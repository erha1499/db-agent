"""连接器边界单测使用驱动替身，不代表真实 MySQL 集成结果。"""

import asyncio
import json
import os
import traceback
from collections import deque

import aiomysql
import pytest

from db_agent.config import DatabaseSettings
from db_agent.db import DatabaseError, MetadataConnector

TABLE = {"type": "BASE TABLE"}
COLUMN = {"name": "id", "type": "bigint", "nullable": "NO", "position": 1}
INDEX = {
    "name": "PRIMARY",
    "non_unique": 0,
    "column_name": "id",
    "position": 1,
    "type": "BTREE",
}
CHECK = {"connection_ok": 1, "server_version": "8.4-stub", "database_name": "db_agent"}
FOREIGN_KEY = {
    "name": "fk_orders_user", "column_name": "id", "position": 1,
    "referenced_schema": "db_agent", "referenced_table": "users",
    "referenced_column": "id", "referenced_type": "BASE TABLE", "component_count": 1,
}


@pytest.fixture
def settings(monkeypatch):
    for name in list(os.environ):
        if name.startswith("DB_AGENT_MYSQL_"):
            monkeypatch.delenv(name)
    return DatabaseSettings(
        _env_file=None,
        host="127.0.0.1",
        password="synthetic-reader-secret",
        allowed_tables=("orders", "users"),
    )


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = deque()
        self.closed = False

    async def execute(self, query, params):
        self.connection.queries.append((query, params))
        if query.startswith("SET SESSION"):
            return
        if self.connection.before_query:
            await self.connection.before_query()
        response = self.connection.responses.popleft()
        if isinstance(response, Exception):
            raise response
        self.rows = deque(response)

    async def fetchone(self):
        return self.rows.popleft() if self.rows else None

    async def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, responses, before_query=None):
        self.responses = deque(responses)
        self.before_query = before_query
        self.queries = []
        self.cursors = []
        self.closed = False

    async def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


@pytest.fixture
def driver(monkeypatch):
    pending = deque()
    calls = []

    async def connect(**kwargs):
        calls.append(kwargs)
        response = pending.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(aiomysql, "connect", connect)
    return pending, calls


def test_check_uses_reader_and_always_closes_connection(settings, driver):
    pending, calls = driver
    connection = FakeConnection([[CHECK]])
    pending.append(connection)

    result = asyncio.run(MetadataConnector(settings).check())

    assert result == {
        "connection_ok": True,
        "database": "db_agent",
        "server_version": "8.4-stub",
    }
    assert calls[0]["user"] == "db_agent_reader"
    assert calls[0]["password"] == "synthetic-reader-secret"
    assert calls[0]["connect_timeout"] == 3
    assert calls[0]["local_infile"] is False
    assert calls[0]["cursorclass"] is aiomysql.SSDictCursor
    assert connection.queries[0] == ("SET SESSION max_execution_time = %s", (5000,))
    assert connection.closed


@pytest.mark.parametrize(
    ("table", "code"),
    [
        ("private", "PERMISSION_DENIED"),
        ("Orders", "PERMISSION_DENIED"),
        ("mysql.user", "INVALID_TABLE"),
        ("orders; DROP TABLE users", "INVALID_TABLE"),
        ("orders`", "INVALID_TABLE"),
        ("orders\x00", "INVALID_TABLE"),
        ("*", "INVALID_TABLE"),
    ],
)
def test_direct_access_checks_identifiers_and_exact_authorization(settings, driver, table, code):
    _, calls = driver
    connector = MetadataConnector(settings)

    with pytest.raises(DatabaseError) as validation_error:
        connector.validate_table(table)
    with pytest.raises(DatabaseError) as describe_error:
        asyncio.run(connector.describe_table(table))

    assert validation_error.value.code == describe_error.value.code == code
    assert calls == []


def test_empty_allowlist_does_not_discover_tables(settings, driver):
    _, calls = driver
    settings = settings.model_copy(update={"allowed_tables": ()})

    assert asyncio.run(MetadataConnector(settings).list_tables()) == {
        "database": "db_agent",
        "tables": [],
    }
    assert calls == []


def test_list_filters_allowlist_and_base_tables_in_database_query(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[{"name": "orders", "type": "BASE TABLE"}]])
    pending.append(connection)

    result = asyncio.run(MetadataConnector(settings).list_tables())

    query, params = connection.queries[1]
    assert "CAST(TABLE_NAME AS BINARY) IN (%s, %s)" in query
    assert "TABLE_TYPE = 'BASE TABLE'" in query
    assert "orders" not in query and "users" not in query
    assert params == ("db_agent", "orders", "users", 201)
    assert result["tables"] == [{"name": "orders", "type": "BASE TABLE"}]
    assert connection.closed


def test_describe_returns_ordered_column_and_index_metadata(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX], []])
    pending.append(connection)

    result = asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert result["columns"] == [COLUMN]
    assert result["indexes"] == [
        {"name": "PRIMARY", "unique": True, "column": "id", "position": 1, "type": "BTREE"}
    ]
    assert result["foreign_keys"] == []
    assert result["foreign_keys_scope"] == "current_database_authorized_tables"
    for query, params in connection.queries[1:4]:
        assert "orders" not in query
        assert params[:2] == ("db_agent", "orders")
        assert "CAST(TABLE_NAME AS BINARY)" in query
        assert "COMMENT" not in query and "DEFAULT" not in query
    assert connection.closed


@pytest.mark.parametrize("foreign_keys,expected", [
    ([FOREIGN_KEY], [{"name": "fk_orders_user", "columns": ["id"],
                      "referenced_table": "users", "referenced_columns": ["id"]}]),
    ([{**FOREIGN_KEY, "position": 2, "column_name": "user_id",
       "referenced_column": "user_id", "component_count": 2},
      {**FOREIGN_KEY, "component_count": 2}],
     [{"name": "fk_orders_user", "columns": ["id", "user_id"],
       "referenced_table": "users", "referenced_columns": ["id", "user_id"]}]),
    ([{**FOREIGN_KEY, "name": "fk_z"}, {**FOREIGN_KEY, "name": "fk_a"}],
     [{"name": name, "columns": ["id"], "referenced_table": "users",
       "referenced_columns": ["id"]} for name in ("fk_a", "fk_z")]),
], ids=["single", "composite_positions", "constraint_order"])
def test_foreign_keys_keep_complete_ordered_column_pairs(settings, driver, foreign_keys, expected):
    pending, calls = driver
    columns = [COLUMN, {**COLUMN, "name": "user_id", "position": 2}]
    connection = FakeConnection([[TABLE], columns, [INDEX], foreign_keys])
    pending.append(connection)

    result = asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert result["foreign_keys"] == expected
    assert result["foreign_keys_scope"] == "current_database_authorized_tables"
    assert len(calls) == 1 and connection.closed
    assert all(cursor.closed for cursor in connection.cursors)


def test_foreign_key_query_filters_targets_before_reading_constraint_fields(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX], []])
    pending.append(connection)

    asyncio.run(MetadataConnector(settings).describe_table("orders"))

    query, params = connection.queries[-1]
    assert "FROM information_schema.KEY_COLUMN_USAGE AS k" in query
    assert "JOIN information_schema.TABLES AS t" in query
    assert "CAST(k.TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)" in query
    assert "CAST(k.TABLE_NAME AS BINARY) = CAST(%s AS BINARY)" in query
    assert "CAST(k.REFERENCED_TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)" in query
    assert "CAST(k.REFERENCED_TABLE_NAME AS BINARY) IN (%s, %s)" in query
    assert "CAST(t.TABLE_SCHEMA AS BINARY) = CAST(k.REFERENCED_TABLE_SCHEMA AS BINARY)" in query
    assert "CAST(t.TABLE_NAME AS BINARY) = CAST(k.REFERENCED_TABLE_NAME AS BINARY)" in query
    assert "t.TABLE_TYPE = 'BASE TABLE'" in query
    assert "COUNT(*) OVER" in query and "ORDINAL_POSITION" in query
    assert "COMMENT" not in query and "DEFAULT" not in query
    assert all(value not in query for value in ("db_agent", "orders", "users"))
    assert params == ("db_agent", "orders", "db_agent", "orders", "users", 199)


@pytest.mark.parametrize("override", [
    {"referenced_schema": "private_database"}, {"referenced_schema": "DB_AGENT"},
    {"referenced_table": "private_table"}, {"referenced_table": "Users"},
    {"referenced_type": "VIEW"}, {"name": "private-invalid-name"},
    {"column_name": "private_column"}, {"referenced_column": "private.invalid"},
    {"position": True}, {"position": 0}, {"component_count": True},
    {"component_count": 2},
])
def test_invalid_foreign_key_evidence_fails_with_a_fixed_error(
    settings, driver, override,
):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX], [{**FOREIGN_KEY, **override}]])
    pending.append(connection)

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert error.value.code == "INVALID_METADATA"
    assert "private" not in "".join(traceback.format_exception(error.value))
    assert connection.closed


@pytest.mark.parametrize("second", [
    {"position": 1}, {"position": 3}, {"referenced_table": "orders"},
    {"component_count": 3}, {"referenced_column": "id"}, {"column_name": "id"},
])
def test_foreign_key_components_cannot_be_missing_duplicated_or_mixed(settings, driver, second):
    pending, _ = driver
    first = {**FOREIGN_KEY, "component_count": 2}
    next_component = {**first, "position": 2, "column_name": "user_id",
                      "referenced_column": "user_id", **second}
    connection = FakeConnection([
        [TABLE], [COLUMN, {**COLUMN, "name": "user_id", "position": 2}], [INDEX],
        [first, next_component],
    ])
    pending.append(connection)

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert error.value.code == "INVALID_METADATA" and connection.closed


def test_foreign_key_rows_share_column_and_index_budget_without_partial_results(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX], [FOREIGN_KEY]])
    pending.append(connection)
    limited = settings.model_copy(update={"max_metadata_rows": 2})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(limited).describe_table("orders"))

    assert error.value.code == "RESULT_LIMIT"
    assert connection.queries[-1][1][-1] == 1
    assert connection.closed and not connection.cursors[-1].closed


def test_foreign_key_raw_rows_share_metadata_byte_budget(settings, driver):
    pending, _ = driver
    foreign_keys = [{**FOREIGN_KEY, "name": "fk_" + str(index) + "x" * 50} for index in range(4)]
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX], foreign_keys])
    pending.append(connection)
    limited = settings.model_copy(update={"max_metadata_bytes": 1024})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(limited).describe_table("orders"))

    assert error.value.code == "RESULT_LIMIT"
    assert connection.closed and not connection.cursors[-1].closed


def test_foreign_key_scope_and_grouped_envelope_share_final_byte_budget(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX], []])
    pending.append(connection)
    previous = {"database": "db_agent", "table": "orders", "columns": [COLUMN],
                "indexes": [{"name": "PRIMARY", "unique": True, "column": "id",
                             "position": 1, "type": "BTREE"}]}
    limited = settings.model_copy(update={"max_metadata_bytes": len(json.dumps(previous).encode())})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(limited).describe_table("orders"))

    assert error.value.code == "RESULT_LIMIT"
    assert "KEY_COLUMN_USAGE" in connection.queries[-1][0]
    assert connection.closed and connection.cursors[-1].closed


def test_foreign_key_driver_error_is_sanitized_without_draining_cursor(settings, driver):
    pending, _ = driver
    connection = FakeConnection([
        [TABLE], [COLUMN], [INDEX], aiomysql.OperationalError(2013, "private-target-constraint"),
    ])
    pending.append(connection)

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert error.value.code == "CONNECTION_ERROR"
    assert "private-target" not in "".join(traceback.format_exception(error.value))
    assert connection.closed and not connection.cursors[-1].closed


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
def test_foreign_key_wait_uses_existing_deadline_and_cancellation_cleanup(settings, driver, cancel):
    pending, calls = driver

    async def scenario():
        started = asyncio.Event()

        async def pause_last_query():
            if "KEY_COLUMN_USAGE" in connection.queries[-1][0]:
                started.set()
                await asyncio.Event().wait()

        connection = FakeConnection([[TABLE], [COLUMN], [INDEX], []], pause_last_query)
        pending.append(connection)
        limited = settings.model_copy(update={"metadata_timeout_seconds": 0.05})
        task = asyncio.create_task(MetadataConnector(limited).describe_table("orders"))
        await asyncio.wait_for(started.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(DatabaseError) as error:
                await task
            assert error.value.code == "TIMEOUT"
        assert len(calls) == 1 and connection.closed and not connection.cursors[-1].closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tables", "code"),
    [([], "TABLE_NOT_FOUND"), ([{"type": "VIEW"}], "UNSUPPORTED_TABLE")],
)
def test_missing_tables_and_views_do_not_reach_column_queries(settings, driver, tables, code):
    pending, _ = driver
    connection = FakeConnection([tables])
    pending.append(connection)

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert error.value.code == code
    assert len(connection.queries) == 2
    assert connection.closed


def test_row_budget_covers_columns_and_indexes_together(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [COLUMN], [INDEX]])
    pending.append(connection)
    settings = settings.model_copy(update={"max_metadata_rows": 1})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert error.value.code == "RESULT_LIMIT"
    assert connection.queries[-1][1][-1] == 1
    assert connection.closed
    assert not connection.cursors[-1].closed  # 不调用会排空未读取结果的流式 cursor.close。


def test_list_does_not_silently_truncate_rows(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[{"name": "orders"}, {"name": "users"}]])
    pending.append(connection)
    settings = settings.model_copy(update={"max_metadata_rows": 1})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).list_tables())

    assert error.value.code == "RESULT_LIMIT"
    assert connection.queries[-1][1][-1] == 2
    assert connection.closed


def test_utf8_byte_budget_rejects_entire_result(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[TABLE], [{**COLUMN, "type": "字" * 400}]])
    pending.append(connection)
    settings = settings.model_copy(update={"max_metadata_bytes": 1024})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert error.value.code == "RESULT_LIMIT"
    assert connection.closed


def test_result_envelope_is_included_in_byte_budget(settings, driver):
    pending, _ = driver
    connection = FakeConnection([[{"name": "orders", "type": "BASE TABLE"}]])
    pending.append(connection)
    settings = settings.model_copy(update={"max_metadata_bytes": 60})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).list_tables())

    assert error.value.code == "RESULT_LIMIT"
    assert connection.closed


def test_byte_budget_includes_tool_json_serialization_whitespace(settings, driver):
    pending, _ = driver
    row = {"name": "orders", "type": "BASE TABLE"}
    connection = FakeConnection([[row]])
    pending.append(connection)
    result = {"database": "db_agent", "tables": [row]}
    compact_size = len(json.dumps(result, separators=(",", ":")).encode())
    assert len(json.dumps(result).encode()) > compact_size
    settings = settings.model_copy(update={"max_metadata_bytes": compact_size})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).list_tables())

    assert error.value.code == "RESULT_LIMIT"
    assert connection.closed


@pytest.mark.parametrize(
    ("number", "code"),
    [(1045, "PERMISSION_DENIED"), (2003, "CONNECTION_ERROR"), (3024, "TIMEOUT"),
     (1064, "SYNTAX_ERROR"), (1054, "SQL_REFERENCE_ERROR")],
)
def test_driver_errors_are_sanitized_and_close_connection(settings, driver, number, code):
    pending, _ = driver
    connection = FakeConnection([aiomysql.OperationalError(number, "raw-secret-host-password")])
    pending.append(connection)

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).check())

    assert error.value.code == code
    assert "raw-secret" not in "".join(traceback.format_exception(error.value))
    assert connection.closed


def test_connect_failure_does_not_leak_driver_message(settings, driver):
    pending, calls = driver
    pending.append(aiomysql.OperationalError(2003, "raw-secret-host-password"))

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).check())

    assert error.value.code == "CONNECTION_ERROR"
    assert "raw-secret" not in "".join(traceback.format_exception(error.value))
    assert len(calls) == 1


def test_timeout_closes_connection_without_draining_cursor(settings, driver):
    pending, _ = driver

    async def wait_forever():
        await asyncio.Event().wait()

    connection = FakeConnection([[CHECK]], before_query=wait_forever)
    pending.append(connection)
    settings = settings.model_copy(update={"metadata_timeout_seconds": 0.01})

    with pytest.raises(DatabaseError) as error:
        asyncio.run(MetadataConnector(settings).check())

    assert error.value.code == "TIMEOUT"
    assert connection.closed
    assert not connection.cursors[-1].closed


def test_external_cancellation_propagates_after_closing_connection(settings, driver):
    pending, _ = driver

    async def scenario():
        started = asyncio.Event()

        async def wait_forever():
            started.set()
            await asyncio.Event().wait()

        connection = FakeConnection([[CHECK]], before_query=wait_forever)
        pending.append(connection)
        task = asyncio.create_task(MetadataConnector(settings).check())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.closed

    asyncio.run(scenario())


def test_connector_serializes_requests(settings, driver):
    pending, calls = driver

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def pause():
            started.set()
            await release.wait()

        first_connection = FakeConnection([[CHECK]], before_query=pause)
        second_connection = FakeConnection([[CHECK]])
        pending.extend([first_connection, second_connection])
        connector = MetadataConnector(settings)
        first = asyncio.create_task(connector.check())
        await started.wait()
        second = asyncio.create_task(connector.check())
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert len(calls) == 2
        assert first_connection.closed and second_connection.closed

    asyncio.run(scenario())


def test_time_budget_includes_wait_for_connector_lock(settings, driver):
    _, calls = driver
    settings = settings.model_copy(update={"metadata_timeout_seconds": 0.01})

    async def scenario():
        connector = MetadataConnector(settings)
        async with connector._lock:
            with pytest.raises(DatabaseError) as error:
                await connector.check()
        assert error.value.code == "TIMEOUT"
        assert calls == []

    asyncio.run(scenario())


def test_real_driver_closes_socket_when_mysql_handshake_times_out(settings):
    """本地 TCP 替身故意不发送 MySQL 握手，验证驱动在取消时关闭已建 socket。"""

    async def scenario():
        disconnected = asyncio.Event()

        async def accept(reader, writer):
            try:
                if await reader.read(1) == b"":
                    disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
            local_settings = settings.model_copy(
                update={
                    "port": server.sockets[0].getsockname()[1],
                    "metadata_timeout_seconds": 0.05,
                }
            )
            with pytest.raises(DatabaseError) as error:
                await MetadataConnector(local_settings).check()
            assert error.value.code == "TIMEOUT"
            await asyncio.wait_for(disconnected.wait(), timeout=1)

    asyncio.run(scenario())
