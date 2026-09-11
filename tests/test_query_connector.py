"""受控 SELECT 的离线驱动边界，不代替真实 MySQL 事务与锁验证。"""

import asyncio
import json
import os
from collections import deque

import aiomysql
import pytest
from pymysql.constants import FIELD_TYPE
from test_db import driver as driver
from test_db import settings as settings

from db_agent.config import AnalysisSettings, QuerySettings
from db_agent.db import MetadataConnector

SQL = "SELECT id FROM orders WHERE status LIKE '%paid%'"
SESSION = {
    "server_version": "8.4.11",
    "database_name": "db_agent",
    "sql_mode": "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
}
FORMAT = {"json_format_version": 1, "end_markers": 0}
TABLE = {"type": "BASE TABLE", "engine": "InnoDB"}
PLAN = {
    "query_block": {
        "select_id": 1,
        "table": {
            "table_name": "orders", "access_type": "ALL", "rows_examined_per_scan": 6,
            "rows_produced_per_join": 6, "filtered": 100,
        },
    },
}
COLUMN = ("id", FIELD_TYPE.LONGLONG, None, None, None, None, False)


@pytest.fixture
def limits(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("DB_AGENT_ANALYSIS_", "DB_AGENT_QUERY_")):
            monkeypatch.delenv(name)
    return AnalysisSettings(_env_file=None), QuerySettings(_env_file=None)


class QueryCursor:
    def __init__(self, connection, cursor_type):
        self.connection = connection
        self.cursor_type = cursor_type
        self.description = connection.description if cursor_type is aiomysql.SSCursor else None
        self.rows = deque()
        self.closed = False
        self.reads = 0

    async def execute(self, query, params):
        self.connection.queries.append((query, params))
        if self.connection.before_query:
            await self.connection.before_query(self, query)
        if query == "START TRANSACTION READ ONLY":
            self.connection.server_status = self.connection.transaction_status
            return
        if query.startswith("SET SESSION"):
            return
        response = self.connection.responses.popleft()
        if isinstance(response, Exception):
            raise response
        self.rows = deque(response)

    async def fetchone(self):
        self.reads += 1
        if self.cursor_type is aiomysql.SSCursor and self.connection.before_read:
            await self.connection.before_read(self)
        return self.rows.popleft() if self.rows else None

    async def close(self):
        self.closed = True
        if self.cursor_type is aiomysql.SSCursor and self.connection.before_close:
            await self.connection.before_close(self)


class QueryConnection:
    def __init__(
        self, *, plan=PLAN, session=SESSION, formats=FORMAT, first_table=TABLE,
        second_table=TABLE, rows=((1,), (2,)), description=(COLUMN,),
        transaction_status=0x2001, before_query=None, before_read=None, before_close=None,
    ):
        self.responses = deque(
            [[session], [formats], [] if first_table is None else [first_table],
             [{"EXPLAIN": json.dumps(plan)}], [] if second_table is None else [second_table],
             rows]
        )
        self.queries = []
        self.cursors = []
        self.closed = False
        self.description = description
        self.transaction_status = transaction_status
        self.server_status = 0
        self.before_query = before_query
        self.before_read = before_read
        self.before_close = before_close

    async def cursor(self, cursor_type=None):
        cursor = QueryCursor(self, cursor_type)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True

    @property
    def business_cursors(self):
        return [cursor for cursor in self.cursors if cursor.cursor_type is aiomysql.SSCursor]


def run_query(settings, limits, driver, connection=None, sql=SQL):
    connection = connection or QueryConnection()
    driver[0].append(connection)
    outcome = asyncio.run(MetadataConnector(settings).execute_checked(sql, *limits))
    return outcome, connection


def assert_not_executed(outcome, connection):
    assert outcome["result"] is None
    assert outcome["execution_status"] == "not_started"
    assert not connection.business_cursors
    assert connection.closed


def test_allow_rechecks_and_executes_original_sql_in_one_readonly_transaction(
    settings, limits, driver,
):
    outcome, connection = run_query(settings, limits, driver)

    assert outcome["decision"] == "ALLOW" and outcome["error"] is None
    assert outcome["execution_status"] == "completed"
    assert outcome["result"]["rows"] == [[1], [2]]
    assert outcome["result"]["server_statement_status"] == "completed"
    assert outcome["assessment"].decision == "ALLOW"
    assert "plan" not in outcome and outcome["server_version"] == "8.4.11"
    assert connection.closed and connection.business_cursors[0].closed
    assert len(driver[1]) == 1
    assert driver[1][0]["cursorclass"] is aiomysql.SSDictCursor
    assert driver[1][0]["user"] == "db_agent_reader"
    queries = connection.queries
    assert queries[-1] == (SQL, None)
    assert ("SET SESSION time_zone = '+00:00'", ()) in queries
    assert ("SET SESSION transaction_isolation = 'READ-COMMITTED'", ()) in queries
    start = queries.index(("START TRANSACTION READ ONLY", ()))
    explain = queries.index(("EXPLAIN FORMAT=JSON " + SQL, None))
    tables = [index for index, (query, _) in enumerate(queries) if "ENGINE AS engine" in query]
    assert start < tables[0] < explain < tables[1] < len(queries) - 1
    assert all(queries[index][1] == ("db_agent", "orders") for index in tables)
    maximums = [params[0] for query, params in queries if "max_execution_time" in query]
    assert maximums[1:] == [10000, 5000] and 0 < maximums[0] <= 15000
    lock_wait = [params[0] for query, params in queries if "lock_wait_timeout" in query]
    assert lock_wait == [10]


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM orders", "SELECT id FROM other.orders", "SELECT id FROM private",
     "SELECT id FROM orders; SELECT id FROM users", "SELECT SLEEP(1) FROM orders",
     "SELECT id FROM orders FOR UPDATE", "EXPLAIN ANALYZE SELECT id FROM orders"],
)
def test_direct_call_rechecks_sql_and_never_connects_on_rejection(settings, limits, driver, sql):
    connector = MetadataConnector(settings)
    assert connector.check_sql(SQL, limits[0]).decision == "ALLOW"
    outcome = asyncio.run(connector.execute_checked(sql, *limits))
    assert outcome["decision"] != "ALLOW"
    assert outcome["execution_status"] == "not_started" and outcome["result"] is None
    assert outcome["assessment"] is None and driver[1] == []


@pytest.mark.parametrize("keyword", ["approved", "report", "check", "assessment"])
def test_no_cached_approval_or_report_argument(settings, limits, driver, keyword):
    with pytest.raises(TypeError):
        asyncio.run(MetadataConnector(settings).execute_checked(SQL, *limits, **{keyword: True}))
    assert driver[1] == []


def test_authorization_is_bound_to_current_connector_target(settings, limits, driver):
    old_connector = MetadataConnector(settings)
    assert old_connector.check_sql(SQL, limits[0]).decision == "ALLOW"
    changed = settings.model_copy(update={"allowed_tables": ("users",)})
    outcome = asyncio.run(MetadataConnector(changed).execute_checked(SQL, *limits))
    assert outcome["decision"] == "BLOCK" and driver[1] == []
    target = settings.model_copy(update={"database": "other"})
    outcome = asyncio.run(
        MetadataConnector(target).execute_checked("SELECT id FROM db_agent.orders", *limits)
    )
    assert outcome["decision"] == "BLOCK" and driver[1] == []


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [({"session": {**SESSION, "database_name": "other"}}, "DATABASE_MISMATCH"),
     ({"session": {**SESSION, "server_version": "8.0.43"}}, "UNSUPPORTED_SERVER"),
     ({"session": {**SESSION, "sql_mode": "HIGH_NOT_PRECEDENCE"}}, "UNSUPPORTED_SQL_MODE"),
     ({"formats": {"json_format_version": 2, "end_markers": 0}}, "UNSUPPORTED_PLAN_FORMAT"),
     ({"first_table": None}, "TABLE_NOT_FOUND"),
     ({"first_table": {"type": "VIEW", "engine": None}}, "UNSUPPORTED_TABLE"),
     ({"first_table": {"type": "BASE TABLE", "engine": "MyISAM"}}, "UNSUPPORTED_ENGINE"),
     ({"first_table": {"type": "BASE TABLE"}}, "UNSUPPORTED_ENGINE")],
)
def test_incomplete_evidence_stops_before_explain(settings, limits, driver, kwargs, code):
    outcome, connection = run_query(settings, limits, driver, QueryConnection(**kwargs))
    assert_not_executed(outcome, connection)
    assert outcome["decision"] == "UNKNOWN" and outcome["error"]["code"] == code
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)


@pytest.mark.parametrize(
    ("table", "code"),
    [(None, "TABLE_NOT_FOUND"), ({"type": "VIEW", "engine": None}, "UNSUPPORTED_TABLE"),
     ({"type": "BASE TABLE", "engine": "MyISAM"}, "UNSUPPORTED_ENGINE")],
)
def test_table_change_after_explain_never_executes(settings, limits, driver, table, code):
    outcome, connection = run_query(
        settings, limits, driver, QueryConnection(second_table=table),
    )
    assert_not_executed(outcome, connection)
    assert outcome["assessment"].decision == "ALLOW" and outcome["decision"] == "UNKNOWN"
    assert outcome["error"]["code"] == code
    assert ("EXPLAIN FORMAT=JSON " + SQL, None) in connection.queries


@pytest.mark.parametrize(
    ("plan", "decision"),
    [({"query_block": {"select_id": 1, "table": {**PLAN["query_block"]["table"],
                                                "rows_examined_per_scan": 100001}}}, "REVIEW"),
     ({"query_block": {"select_id": 1, "table": {"table_name": "orders"}}}, "UNKNOWN"),
     ({"query_block": {"select_id": 1, "future_operation": {}}}, "UNKNOWN")],
)
def test_plan_review_and_unknown_never_run_business_sql(settings, limits, driver, plan, decision):
    outcome, connection = run_query(settings, limits, driver, QueryConnection(plan=plan))
    assert_not_executed(outcome, connection)
    assert outcome["decision"] == decision and outcome["error"] is None


def test_join_rechecks_every_physical_table_and_resolves_plan_aliases(settings, limits, driver):
    sql = "SELECT o.id FROM orders o JOIN users u ON o.user_id = u.id"
    table_plan = PLAN["query_block"]["table"]
    plan = {"query_block": {"select_id": 1, "nested_loop": [
        {"table": {**table_plan, "table_name": "o"}},
        {"table": {**table_plan, "table_name": "u"}},
    ]}}
    connection = QueryConnection()
    connection.responses = deque([
        [SESSION], [FORMAT], [TABLE], [TABLE], [{"EXPLAIN": json.dumps(plan)}],
        [TABLE], [TABLE], [(1,)],
    ])
    outcome, connection = run_query(settings, limits, driver, connection, sql)
    assert outcome["execution_status"] == "completed" and outcome["decision"] == "ALLOW"
    assert outcome["check"].aliases == {"o": "orders", "u": "users"}
    table_requests = [params for query, params in connection.queries if "ENGINE AS engine" in query]
    assert table_requests == [("db_agent", "orders"), ("db_agent", "users")] * 2
    assert connection.queries[-1] == (sql, None)


@pytest.mark.parametrize("status", [0, 1, 8192, None])
def test_start_must_confirm_actual_readonly_transaction(settings, limits, driver, status):
    outcome, connection = run_query(
        settings, limits, driver, QueryConnection(transaction_status=status),
    )
    assert_not_executed(outcome, connection)
    assert outcome["decision"] == "UNKNOWN" and outcome["error"]["code"] == "TRANSACTION_STATE"
    assert not any(query.startswith("EXPLAIN") for query, _ in connection.queries)


def test_transaction_loss_after_explain_is_checked_before_dispatch(settings, limits, driver):
    async def lose_transaction(cursor, query):
        if "max_execution_time" in query and any(
            statement.startswith("EXPLAIN") for statement, _ in cursor.connection.queries
        ):
            cursor.connection.server_status = 0

    outcome, connection = run_query(
        settings, limits, driver, QueryConnection(before_query=lose_transaction),
    )
    assert_not_executed(outcome, connection)
    assert outcome["decision"] == "UNKNOWN" and outcome["error"]["code"] == "TRANSACTION_STATE"


@pytest.mark.parametrize(
    ("number", "code", "decision"),
    [(1142, "PERMISSION_DENIED", "BLOCK"), (3024, "TIMEOUT", "ALLOW"),
     (2013, "CONNECTION_ERROR", "ALLOW"), (1064, "SYNTAX_ERROR", "ALLOW"),
     (1105, "DATABASE_ERROR", "ALLOW")],
)
def test_execution_errors_are_safe_and_do_not_claim_success(
    settings, limits, driver, number, code, decision,
):
    connection = QueryConnection(rows=aiomysql.OperationalError(number, "secret sql and password"))
    outcome, connection = run_query(settings, limits, driver, connection)
    assert outcome["decision"] == decision and outcome["execution_status"] == "unknown"
    assert outcome["result"] is None and outcome["error"]["code"] == code
    assert "secret" not in str(outcome) and connection.closed
    assert not connection.business_cursors[0].closed


def test_permission_error_before_execution_is_block_and_not_started(settings, limits, driver):
    connection = QueryConnection()
    connection.responses[0] = aiomysql.OperationalError(1044, "secret")
    outcome, connection = run_query(settings, limits, driver, connection)
    assert_not_executed(outcome, connection)
    assert outcome["decision"] == "BLOCK" and outcome["error"]["code"] == "PERMISSION_DENIED"
    assert "secret" not in str(outcome)


@pytest.mark.parametrize("reason", ["row_limit", "byte_limit"])
def test_result_truncation_closes_socket_without_draining(settings, limits, driver, reason):
    analysis, query = limits
    if reason == "row_limit":
        query = query.model_copy(update={"max_rows": 1})
        connection = QueryConnection(rows=((1,), (2,), (3,)))
        expected_rows, expected_reads = [[1]], 2
    else:
        query = query.model_copy(update={"max_result_bytes": 1024})
        column = ("value", FIELD_TYPE.VAR_STRING, None, None, None, None, True)
        connection = QueryConnection(rows=(("a",), ("b" * 2000,), ("c",)), description=(column,))
        expected_rows, expected_reads = [["a"]], 2
    outcome, connection = run_query(settings, (analysis, query), driver, connection)
    assert outcome["decision"] == "ALLOW" and outcome["execution_status"] == "truncated"
    assert outcome["error"] is None
    assert outcome["result"]["rows"] == expected_rows
    assert outcome["result"]["truncation_reason"] == reason
    assert outcome["result"]["server_statement_status"] == "unknown"
    assert outcome["result"]["result_bytes"] <= query.max_result_bytes
    cursor = connection.business_cursors[0]
    assert cursor.reads == expected_reads and not cursor.closed and cursor.rows
    assert connection.closed


def test_unsupported_result_type_survives_database_context_safely(settings, limits, driver):
    column = ("private", FIELD_TYPE.BLOB, None, None, None, None, True)
    connection = QueryConnection(rows=((b"secret",),), description=(column,))
    outcome, connection = run_query(settings, limits, driver, connection)
    assert outcome["decision"] == "ALLOW" and outcome["execution_status"] == "unknown"
    assert outcome["result"] is None and outcome["error"]["code"] == "UNSUPPORTED_RESULT_TYPE"
    assert "secret" not in str(outcome) and "private" not in str(outcome)
    assert connection.closed and connection.business_cursors[0].reads == 0
    assert not connection.business_cursors[0].closed


def test_close_failure_does_not_report_completed_result(settings, limits, driver):
    async def fail_close(cursor):
        raise OSError("secret")

    outcome, connection = run_query(
        settings, limits, driver, QueryConnection(before_close=fail_close),
    )
    assert outcome["execution_status"] == "unknown" and outcome["result"] is None
    assert outcome["error"]["code"] == "CONNECTION_ERROR" and connection.closed
    assert "secret" not in str(outcome)


@pytest.mark.parametrize("phase", ["analysis", "dispatch", "read"])
def test_stage_timeouts_cleanup_and_preserve_execution_uncertainty(settings, limits, driver, phase):
    async def before_query(cursor, sql):
        if (phase == "analysis" and sql.startswith("EXPLAIN")) or (
            phase == "dispatch" and cursor.cursor_type is aiomysql.SSCursor
        ):
            await asyncio.Event().wait()

    async def before_read(cursor):
        if phase == "read":
            await asyncio.Event().wait()

    analysis, query = limits
    analysis = analysis.model_copy(update={"timeout_seconds": 0.03})
    query = query.model_copy(update={"execution_timeout_seconds": 0.03})
    outcome, connection = run_query(
        settings, (analysis, query), driver,
        QueryConnection(before_query=before_query, before_read=before_read),
    )
    assert outcome["error"]["code"] == "TIMEOUT" and outcome["result"] is None
    assert connection.closed
    if phase == "analysis":
        assert outcome["decision"] == "UNKNOWN" and outcome["execution_status"] == "not_started"
        assert not connection.business_cursors
    else:
        assert outcome["decision"] == "ALLOW" and outcome["execution_status"] == "unknown"
        assert not connection.business_cursors[0].closed


def test_execution_budget_starts_after_analysis(settings, limits, driver):
    async def slow_analysis(cursor, query):
        if query.startswith("EXPLAIN"):
            await asyncio.sleep(0.04)

    analysis, query = limits
    query = query.model_copy(update={"execution_timeout_seconds": 0.02})
    outcome, connection = run_query(
        settings, (analysis, query), driver, QueryConnection(before_query=slow_analysis),
    )
    assert outcome["execution_status"] == "completed" and connection.closed


def test_total_operation_deadline_still_applies_during_execution(settings, limits, driver):
    async def slow_analysis(cursor, query):
        if query.startswith("EXPLAIN"):
            await asyncio.sleep(0.02)

    async def block_read(cursor):
        await asyncio.Event().wait()

    analysis, query = limits
    query = query.model_copy(update={"operation_timeout_seconds": 0.05})
    outcome, connection = run_query(
        settings, (analysis, query), driver,
        QueryConnection(before_query=slow_analysis, before_read=block_read),
    )
    assert outcome["decision"] == "ALLOW" and outcome["execution_status"] == "unknown"
    assert outcome["error"]["code"] == "TIMEOUT" and outcome["result"] is None
    assert connection.closed and not connection.business_cursors[0].closed


def test_operation_timeout_includes_waiting_for_shared_connector_lock(settings, limits, driver):
    analysis, query = limits
    query = query.model_copy(update={"operation_timeout_seconds": 0.03})

    async def run():
        connector = MetadataConnector(settings)
        async with connector._lock:
            return await connector.execute_checked(SQL, analysis, query)

    outcome = asyncio.run(run())
    assert outcome["decision"] == "UNKNOWN" and outcome["execution_status"] == "not_started"
    assert outcome["error"]["code"] == "TIMEOUT" and driver[1] == []


def test_operation_deadline_limits_connection_attempt(settings, limits, monkeypatch):
    async def connect(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(aiomysql, "connect", connect)
    analysis, query = limits
    query = query.model_copy(update={"operation_timeout_seconds": 0.02})
    outcome = asyncio.run(MetadataConnector(settings).execute_checked(SQL, analysis, query))
    assert outcome["decision"] == "UNKNOWN" and outcome["execution_status"] == "not_started"
    assert outcome["error"]["code"] == "TIMEOUT"


@pytest.mark.parametrize("phase", ["analysis", "dispatch", "read"])
def test_cancellation_propagates_and_never_drains_cursor(settings, limits, driver, phase):
    async def run():
        entered = asyncio.Event()

        async def before_query(cursor, sql):
            if (phase == "analysis" and sql.startswith("EXPLAIN")) or (
                phase == "dispatch" and cursor.cursor_type is aiomysql.SSCursor
            ):
                entered.set()
                await asyncio.Event().wait()

        async def before_read(cursor):
            if phase == "read":
                entered.set()
                await asyncio.Event().wait()

        connection = QueryConnection(before_query=before_query, before_read=before_read)
        driver[0].append(connection)
        task = asyncio.create_task(MetadataConnector(settings).execute_checked(SQL, *limits))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.closed
        assert not any(cursor.closed for cursor in connection.business_cursors)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["revoked", "timeout"])
def test_service_owned_knowledge_veto_after_plan_prevents_select(settings, limits, driver, failure):
    from db_agent.knowledge import KnowledgeError

    connection = QueryConnection()
    driver[0].append(connection)
    visited = []

    async def veto(actual):
        assert actual is connection
        assert any(query.startswith("EXPLAIN FORMAT=JSON") for query, _ in actual.queries)
        assert actual.server_status & 0x2001 == 0x2001
        visited.append(True)
        if failure == "timeout":
            raise TimeoutError()
        raise KnowledgeError("KNOWLEDGE_CHANGED")

    outcome = asyncio.run(MetadataConnector(settings).execute_checked(
        SQL, *limits, before_select=veto,
    ))
    assert visited == [True]
    assert outcome["result"] is None and outcome["decision"] == "UNKNOWN"
    assert outcome["execution_status"] == "not_started"
    assert outcome["error"]["code"] == (
        "TIMEOUT" if failure == "timeout" else "KNOWLEDGE_CHANGED"
    )
    assert (SQL, None) not in connection.queries and connection.closed


def test_service_owned_guard_never_bypasses_original_sql_policy(settings, limits, driver):
    async def guard(connection):
        pytest.fail("forbidden SQL must be rejected before any optional guard")

    outcome = asyncio.run(MetadataConnector(settings).execute_checked(
        "DELETE FROM orders", *limits, before_select=guard,
    ))
    assert outcome["decision"] == "BLOCK"
    assert outcome["execution_status"] == "not_started"
    assert not driver[1]


def test_knowledge_final_check_shares_analysis_deadline(settings, limits, driver):
    connection = QueryConnection()
    driver[0].append(connection)

    async def slow_guard(actual):
        await asyncio.sleep(0.04)

    analysis = limits[0].model_copy(update={"timeout_seconds": 0.01})
    outcome = asyncio.run(MetadataConnector(settings).execute_checked(
        SQL, analysis, limits[1], before_select=slow_guard,
    ))
    assert outcome["decision"] == "UNKNOWN"
    assert outcome["error"]["code"] == "TIMEOUT"
    assert outcome["execution_status"] == "not_started" and outcome["result"] is None
    assert (SQL, None) not in connection.queries and connection.closed


def test_transaction_state_checked_after_final_knowledge_interaction(settings, limits, driver):
    connection = QueryConnection()
    driver[0].append(connection)

    async def state_changed(actual):
        actual.server_status = 0

    outcome = asyncio.run(MetadataConnector(settings).execute_checked(
        SQL, *limits, before_select=state_changed,
    ))
    assert outcome["error"]["code"] == "TRANSACTION_STATE"
    assert outcome["execution_status"] == "not_started"
    assert (SQL, None) not in connection.queries
