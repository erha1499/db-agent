"""Offline protocol tests for the SAME guarded kernel; real tests are separate."""

import asyncio
from collections import deque

import aiomysql
import pytest
from test_db import driver as driver
from test_db import settings as settings
from test_query_connector import (
    SQL,
    QueryConnection,
)
from test_query_connector import (
    limits as limits,
)

from db_agent.db import DatabaseError, MetadataConnector

CANDIDATE = "SELECT id FROM orders WHERE status = 'paid'"


def pair_connection(**kwargs):
    connection = QueryConnection(**kwargs)
    original, candidate = list(connection.responses), list(QueryConnection().responses)
    # The existing fake handles single-query START only; the hook models the
    # successful protocol OK for the new fixed snapshot statement.
    original_hook = connection.before_query

    async def snapshot(cursor, sql):
        if sql == "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY":
            cursor.connection.server_status = cursor.connection.transaction_status
            cursor.connection.responses.appendleft([])
        if original_hook:
            await original_hook(cursor, sql)

    connection.before_query = snapshot
    connection.responses = deque(
        [[{"isolation_level": "REPEATABLE-READ"}], *original,
         [{"isolation_level": "REPEATABLE-READ"}], *candidate]
    )
    return connection


def run_pair(settings, limits, driver, connection, **kwargs):
    driver[0].append(connection)
    result = asyncio.run(MetadataConnector(settings).compare_checked(
        SQL, CANDIDATE, *limits, **kwargs,
    ))
    assert connection.closed
    return result


def test_pair_shares_connection_snapshot_and_full_checks_for_both(settings, limits, driver):
    connection = pair_connection()
    result = run_pair(settings, limits, driver, connection)
    assert [item["execution_status"] for item in result] == ["completed", "completed"]
    assert len(driver[1]) == 1
    queries = [sql for sql, _ in connection.queries]
    assert queries.count("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY") == 1
    assert queries.count("SELECT @@SESSION.transaction_isolation AS isolation_level") == 2
    assert sum(sql.startswith("EXPLAIN FORMAT=JSON") for sql in queries) == 2
    assert sum("ENGINE AS engine" in sql for sql in queries) == 4
    assert [sql for sql in queries if sql in (SQL, CANDIDATE)] == [SQL, CANDIDATE]
    assert all(item["select_duration_ms"] >= 0 for item in result)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT id FROM secret",
                                 "SELECT * FROM (SELECT id FROM orders) AS a",
                                 "SELECT id FROM orders; SELECT id FROM orders"])
def test_direct_pair_rechecks_both_inputs_without_connecting(settings, limits, driver, side, sql):
    statements = [SQL, CANDIDATE]
    statements[side] = sql
    outcomes = asyncio.run(MetadataConnector(settings).compare_checked(*statements, *limits))
    assert outcomes[side]["decision"] != "ALLOW"
    assert all(item["execution_status"] == "not_started" for item in outcomes)
    assert not driver[1]


@pytest.mark.parametrize("parameter", ["approved", "report", "connection", "snapshot", "check"])
def test_pair_accepts_no_reusable_approval_or_connection(settings, limits, driver, parameter):
    with pytest.raises(TypeError):
        asyncio.run(MetadataConnector(settings).compare_checked(
            SQL, CANDIDATE, *limits, **{parameter: True},
        ))
    assert not driver[1]


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("failure", ["isolation", "transaction", "view", "review", "unknown",
                                     "permission", "timeout", "truncated", "cancelled"])
def test_each_side_failure_stops_and_does_not_claim_snapshot_match(
    settings, limits, driver, side, failure,
):
    connection = pair_connection()
    block = side * 7
    if failure == "isolation":
        connection.responses[block] = [{"isolation_level": "READ-COMMITTED"}]
    elif failure == "transaction":
        async def lose(cursor, sql):
            if sql == (SQL if side == 0 else CANDIDATE):
                pytest.fail("transaction loss must stop SELECT")
            if sql.startswith("SET SESSION max_execution_time") and sum(
                text.startswith("EXPLAIN") for text, _ in connection.queries
            ) == side + 1:
                connection.server_status = 0
        original = connection.before_query

        async def hook(cursor, sql):
            await original(cursor, sql)
            await lose(cursor, sql)
        connection.before_query = hook
    elif failure == "view":
        connection.responses[block + 3] = [{"type": "VIEW", "engine": None}]
    elif failure == "review":
        limits = (limits[0].model_copy(update={"review_scan_rows": 1}), limits[1])
        if side:
            # First plan is permitted; second exceeds the unchanged test threshold.
            import json

            from test_query_connector import PLAN
            plan = {"query_block": {"select_id": 1, "table": {
                **PLAN["query_block"]["table"], "rows_examined_per_scan": 1,
            }}}
            connection.responses[4] = [{"EXPLAIN": json.dumps(plan)}]
    elif failure == "unknown":
        connection.responses[block + 4] = [{"EXPLAIN": '{"future_plan": true}'}]
    elif failure == "permission":
        connection.responses[block + 6] = aiomysql.OperationalError(1142, "secret")
    elif failure == "truncated":
        limits = (limits[0], limits[1].model_copy(update={"max_rows": 1}))
        if side:
            connection.responses[6] = [(1,)]
    else:
        async def stop(cursor, sql):
            if sql == (SQL if side == 0 else CANDIDATE):
                if failure == "cancelled":
                    raise asyncio.CancelledError()
                raise TimeoutError()
        original = connection.before_query

        async def hook(cursor, sql):
            await original(cursor, sql)
            await stop(cursor, sql)
        connection.before_query = hook
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            run_pair(settings, limits, driver, connection)
        assert connection.closed
    else:
        outcomes = run_pair(settings, limits, driver, connection)
        assert outcomes[side]["execution_status"] != "completed"
        if not side:
            assert outcomes[1]["execution_status"] == "not_started"
    if not side:
        assert (CANDIDATE, None) not in connection.queries


def test_second_side_service_veto_cannot_reuse_first_approval(settings, limits, driver):
    connection = pair_connection()
    visits = []

    async def guard(actual):
        assert actual is connection
        visits.append(True)
        if len(visits) == 2:
            raise DatabaseError("PERMISSION_DENIED", "denied")
    outcomes = run_pair(settings, limits, driver, connection, before_select=guard)
    assert visits == [True, True]
    assert outcomes[0]["execution_status"] == "completed"
    assert outcomes[1]["decision"] == "BLOCK"
    assert outcomes[1]["execution_status"] == "not_started"
    assert (CANDIDATE, None) not in connection.queries


def test_pair_total_budget_includes_both_queries(settings, limits, driver):
    async def delay(cursor, sql):
        if sql in (SQL, CANDIDATE):
            await asyncio.sleep(0.03)
    connection = pair_connection(before_query=delay)
    limits = (limits[0], limits[1].model_copy(update={"operation_timeout_seconds": 0.05}))
    outcomes = run_pair(settings, limits, driver, connection)
    assert outcomes[0]["execution_status"] == "completed"
    assert outcomes[1]["error"]["code"] == "TIMEOUT"
    assert outcomes[1]["result"] is None
