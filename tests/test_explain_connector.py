"""普通 EXPLAIN 连接器的离线边界测试，不代表真实 MySQL 计划。"""

import asyncio
import json
import os
import traceback

import aiomysql
import pytest
from test_db import FakeConnection
from test_db import driver as driver
from test_db import settings as settings

from db_agent.config import AnalysisSettings
from db_agent.db import DatabaseError, MetadataConnector

SESSION = {
    "server_version": "8.4.11",
    "database_name": "db_agent",
    "sql_mode": "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
}
FORMAT = {"json_format_version": 1, "end_markers": 0}
TABLE = {"type": "BASE TABLE"}
PLAN = {"query_block": {"select_id": 1, "table": {"table_name": "orders"}}}
SQL = "SELECT id FROM orders WHERE status LIKE '%paid%'"


@pytest.fixture
def limits(monkeypatch):
    for name in list(os.environ):
        if name.startswith("DB_AGENT_ANALYSIS_"):
            monkeypatch.delenv(name)
    return AnalysisSettings(_env_file=None)


def connection_for(plan=PLAN, *, session=SESSION, formats=FORMAT, tables=None):
    return FakeConnection(
        [[session], [formats], [TABLE] if tables is None else tables,
         [{"EXPLAIN": json.dumps(plan, ensure_ascii=False)}]]
    )


def test_fresh_check_and_fixed_explain_prefix_preserve_percent_literals(settings, limits, driver):
    pending, calls = driver
    connection = connection_for()
    pending.append(connection)
    connector = MetadataConnector(settings)

    result = asyncio.run(connector.explain_checked(SQL, limits))

    assert result["check"].decision == "ALLOW"
    assert result["plan"] == PLAN
    assert result["server_version"] == "8.4.11"
    assert connector.database == "db_agent"
    assert connection.queries[-1] == ("EXPLAIN FORMAT=JSON " + SQL, None)
    assert ("SET SESSION explain_json_format_version = 1", ()) in connection.queries
    assert ("SET SESSION end_markers_in_json = OFF", ()) in connection.queries
    assert 0 < connection.queries[0][1][0] <= 10000
    assert calls[0]["user"] == "db_agent_reader" and connection.closed


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders", "SELECT id FROM private", "SELECT id FROM other.orders",
        "SELECT id FROM orders; SELECT id FROM users", "EXPLAIN ANALYZE SELECT id FROM orders",
    ],
)
def test_direct_explain_rejects_sql_without_connecting(settings, limits, driver, sql):
    _, calls = driver
    connector = MetadataConnector(settings)
    assert connector.check_sql("SELECT id FROM orders", limits).decision == "ALLOW"

    result = asyncio.run(connector.explain_checked(sql, limits))

    assert result["check"].decision != "ALLOW"
    assert result["plan"] is None and result["server_version"] is None
    assert calls == []


def test_sql_check_uses_copied_scope_and_never_connects(settings, limits, driver):
    _, calls = driver
    connector = MetadataConnector(settings)
    settings.allowed_tables = ("private",)
    assert connector.check_sql("SELECT id FROM orders", limits).decision == "ALLOW"
    assert connector.check_sql("SELECT id FROM private", limits).decision != "ALLOW"
    assert calls == []


@pytest.mark.parametrize(
    ("session", "code"),
    [
        ({**SESSION, "database_name": "other"}, "DATABASE_MISMATCH"),
        ({**SESSION, "server_version": "8.0.43"}, "UNSUPPORTED_SERVER"),
        ({**SESSION, "server_version": "9.0.1"}, "UNSUPPORTED_SERVER"),
        ({**SESSION, "server_version": "8.4"}, "UNSUPPORTED_SERVER"),
        ({**SESSION, "server_version": "8.4.11-MariaDB"}, "UNSUPPORTED_SERVER"),
        ({**SESSION, "sql_mode": None}, "UNSUPPORTED_SQL_MODE"),
    ],
)
def test_target_and_server_contract_fail_before_explain(settings, limits, driver, session, code):
    pending, _ = driver
    connection = connection_for(session=session)
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == code and connection.closed
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)


@pytest.mark.parametrize(
    "mode",
    ["ANSI_QUOTES", "NO_BACKSLASH_ESCAPES", "PIPES_AS_CONCAT", "IGNORE_SPACE",
     "HIGH_NOT_PRECEDENCE", "FUTURE_MODE"],
)
def test_unsupported_sql_modes_stop_before_plan(settings, limits, driver, mode):
    pending, _ = driver
    connection = connection_for(session={**SESSION, "sql_mode": "STRICT_TRANS_TABLES," + mode})
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "UNSUPPORTED_SQL_MODE" and connection.closed
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)


@pytest.mark.parametrize(
    "formats", [{"json_format_version": 2, "end_markers": 0},
                {"json_format_version": 1, "end_markers": 1}, {}],
)
def test_json_session_settings_must_be_verified(settings, limits, driver, formats):
    pending, _ = driver
    connection = connection_for(formats=formats)
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "UNSUPPORTED_PLAN_FORMAT" and connection.closed
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)


@pytest.mark.parametrize(
    ("tables", "code"), [([], "TABLE_NOT_FOUND"), ([{"type": "VIEW"}], "UNSUPPORTED_TABLE")],
)
def test_views_and_missing_tables_do_not_reach_explain(settings, limits, driver, tables, code):
    pending, _ = driver
    connection = connection_for(tables=tables)
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == code and connection.closed
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)
    assert connection.queries[-1][1] == ("db_agent", "orders")


def test_every_referenced_table_is_checked_before_plan(settings, limits, driver):
    pending, _ = driver
    connection = FakeConnection([[SESSION], [FORMAT], [TABLE], [{"type": "VIEW"}]])
    pending.append(connection)
    sql = "SELECT o.id FROM orders o JOIN users u ON o.id = u.id"
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(sql, limits))
    assert error.value.code == "UNSUPPORTED_TABLE" and connection.closed
    checked_tables = [params[1] for query, params in connection.queries if "TABLE_TYPE" in query]
    assert set(checked_tables) == {"orders", "users"}
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)


def test_plan_has_its_own_byte_budget(settings, limits, driver):
    pending, _ = driver
    plan = {"query_block": {"attached_condition": "x" * 2048}}
    connection = connection_for(plan)
    pending.append(connection)
    settings = settings.model_copy(update={"max_metadata_bytes": 1024})
    result = asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert result["plan"] == plan and connection.closed


def test_plan_byte_budget_counts_utf8_and_closes_without_drain(settings, limits, driver):
    pending, _ = driver
    connection = connection_for({"query_block": {"attached_condition": "字" * 400}})
    pending.append(connection)
    limits = limits.model_copy(update={"max_plan_bytes": 1024})
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "RESULT_LIMIT" and connection.closed
    assert not connection.cursors[-1].closed


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ([{"EXPLAIN": "{private-invalid-json"}], "INVALID_PLAN"),
        ([{"EXPLAIN": '{"query_block":{"value":NaN}}'}], "INVALID_PLAN"),
        ([{"EXPLAIN": '{"operation":"version 2"}'}], "UNSUPPORTED_PLAN_FORMAT"),
        ([{"EXPLAIN": "[]"}], "UNSUPPORTED_PLAN_FORMAT"),
        ([{"EXPLAIN": 1}], "INVALID_PLAN"), ([], "INVALID_PLAN"),
        ([{"unknown": "private-value"}], "INVALID_PLAN"),
        ([{"EXPLAIN": json.dumps(PLAN)}, {"EXPLAIN": json.dumps(PLAN)}], "INVALID_PLAN"),
    ],
)
def test_invalid_plans_are_rejected_without_raw_content(settings, limits, driver, response, code):
    pending, _ = driver
    connection = FakeConnection([[SESSION], [FORMAT], [TABLE], response])
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == code and connection.closed
    assert "private" not in "".join(traceback.format_exception(error.value))


def test_plan_structure_budget(settings, limits, driver):
    pending, _ = driver
    connection = connection_for()
    pending.append(connection)
    limits = limits.model_copy(update={"max_plan_nodes": 1})
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "RESULT_LIMIT" and connection.closed


def test_deep_json_is_rejected_by_decoder_or_structure_budget(settings, limits, driver):
    pending, _ = driver
    raw = '{"query_block":{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}}"
    connection = FakeConnection([[SESSION], [FORMAT], [TABLE], [{"EXPLAIN": raw}]])
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code in {"INVALID_PLAN", "RESULT_LIMIT"} and connection.closed


def test_json_decoder_recursion_failure_is_sanitized(settings, limits, driver, monkeypatch):
    pending, _ = driver
    connection = connection_for()
    pending.append(connection)

    def fail_decode(*args, **kwargs):
        raise RecursionError("private deeply nested plan")

    monkeypatch.setattr("db_agent.db.json.loads", fail_decode)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "INVALID_PLAN" and connection.closed
    assert "private" not in "".join(traceback.format_exception(error.value))


def test_analysis_timeout_includes_waiting_for_connector_lock(settings, limits, driver):
    _, calls = driver
    limits = limits.model_copy(update={"timeout_seconds": 0.01})

    async def scenario():
        connector = MetadataConnector(settings)
        async with connector._lock:
            with pytest.raises(DatabaseError) as error:
                await connector.explain_checked(SQL, limits)
        assert error.value.code == "TIMEOUT" and calls == []

    asyncio.run(scenario())


def test_analysis_uses_analysis_deadline_and_cleans_on_timeout(settings, limits, driver):
    pending, _ = driver

    async def wait_forever():
        await asyncio.Event().wait()

    connection = FakeConnection([[SESSION]], before_query=wait_forever)
    pending.append(connection)
    limits = limits.model_copy(update={"timeout_seconds": 0.02})
    settings = settings.model_copy(update={"metadata_timeout_seconds": 10})
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "TIMEOUT" and connection.closed
    assert "已取消" not in error.value.message
    assert not connection.cursors[-1].closed


def test_external_cancel_closes_analysis_connection(settings, limits, driver):
    pending, _ = driver

    async def scenario():
        started = asyncio.Event()

        async def pause():
            started.set()
            await asyncio.Event().wait()

        connection = FakeConnection([[SESSION]], before_query=pause)
        pending.append(connection)
        task = asyncio.create_task(MetadataConnector(settings).explain_checked(SQL, limits))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.closed

    asyncio.run(scenario())


def test_driver_failure_cannot_leak_query_or_credentials(settings, limits, driver):
    pending, _ = driver
    connection = FakeConnection(
        [[SESSION], [FORMAT], [TABLE], aiomysql.OperationalError(1064, "private SQL password")]
    )
    pending.append(connection)
    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).explain_checked(SQL, limits))
    assert error.value.code == "SYNTAX_ERROR" and connection.closed
    assert "private" not in "".join(traceback.format_exception(error.value))
