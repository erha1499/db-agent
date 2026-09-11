"""Revocable identity guards on paired execution; real reader probes are opt-in."""

import asyncio
import os

import aiomysql
import pytest
from test_db import driver as driver
from test_db import settings as settings
from test_optimization_connector import CANDIDATE, pair_connection
from test_query_connector import SQL
from test_query_connector import limits as limits

from db_agent.config import (
    load_analysis_settings,
    load_database_settings,
    load_query_settings,
)
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.optimization import OptimizationService


class RevocableIdentity:
    """Synthetic trusted service state, never a SQL/tool argument or driver error."""

    def __init__(self):
        self.active = True
        self.rejected = 0

    def __call__(self):
        if not self.active:
            self.rejected += 1
            raise DatabaseError("PERMISSION_DENIED", "synthetic identity revoked")


def assert_denied(outcomes, side, execution_status):
    denied = outcomes[side]
    assert denied["decision"] == "BLOCK"
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert denied["execution_status"] == execution_status
    assert denied["result"] is None
    if side == 0:
        assert outcomes[1]["execution_status"] == "not_started"
        assert outcomes[1]["result"] is None


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("phase", ["cursor_creation", "before_select"])
def test_pair_rechecks_identity_immediately_before_each_select(
    settings, limits, driver, side, phase,
):
    identity = RevocableIdentity()
    connection = pair_connection()
    original_cursor = connection.cursor
    visits = []

    async def cursor(cursor_type=None):
        result = await original_cursor(cursor_type)
        if (phase == "cursor_creation" and cursor_type is aiomysql.SSCursor
                and len(connection.business_cursors) == side + 1):
            identity.active = False
        return result

    async def before_select(actual):
        assert actual is connection
        visits.append(actual)
        if phase == "before_select" and len(visits) == side + 1:
            identity.active = False
        # Deliberately return normally: the connector must call its identity guard.

    connection.cursor = cursor
    driver[0].append(connection)
    outcomes = asyncio.run(MetadataConnector(
        settings, authorization_check=identity,
    ).compare_checked(SQL, CANDIDATE, *limits, before_select=before_select))

    assert identity.rejected >= 1
    assert_denied(outcomes, side, "not_started")
    assert [sql for sql, _ in connection.queries if sql in (SQL, CANDIDATE)] == [
        SQL, CANDIDATE,
    ][:side]
    assert connection.closed
    if side:
        assert outcomes[0]["execution_status"] == "completed"
        assert outcomes[0]["result"]["rows"] == [[1], [2]]


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("phase", ["last_row", "eof"])
def test_pair_discards_each_result_if_identity_changes_while_reading(
    settings, limits, driver, side, phase,
):
    identity = RevocableIdentity()
    connection = pair_connection()

    async def before_read(cursor):
        target = connection.business_cursors.index(cursor) == side
        boundary = len(cursor.rows) == 1 if phase == "last_row" else not cursor.rows
        if target and boundary:
            identity.active = False

    connection.before_read = before_read
    driver[0].append(connection)
    outcomes = asyncio.run(MetadataConnector(
        settings, authorization_check=identity,
    ).compare_checked(SQL, CANDIDATE, *limits))

    assert identity.rejected >= 1
    assert_denied(outcomes, side, "unknown")
    assert [sql for sql, _ in connection.queries if sql in (SQL, CANDIDATE)] == [
        SQL, CANDIDATE,
    ][:side + 1]
    assert connection.business_cursors[side].reads == 3  # two rows plus EOF
    assert not connection.business_cursors[side].closed  # denied result closes the socket
    assert connection.closed
    if side:
        assert outcomes[0]["execution_status"] == "completed"
        assert connection.business_cursors[0].closed


def test_repeat_cannot_reuse_completed_pair_after_identity_revocation(settings, limits, driver):
    identity = RevocableIdentity()
    connection = pair_connection()
    original_close = connection.close

    def close():
        original_close()
        identity.active = False  # revoke only after the first pair releases its connection

    connection.close = close
    driver[0].append(connection)
    service = OptimizationService(MetadataConnector(
        settings, authorization_check=identity,
    ), *limits)
    report = asyncio.run(service.compare(SQL, CANDIDATE, repeat=2))

    assert report["requested_trials"] == 2
    assert report["completed_trials"] == 1
    assert report["trials"][0]["outcome"] == "observed_equal"
    assert report["outcome"] == "inconclusive"
    assert report["performance"] is None
    assert report["error"]["code"] == "COMPARISON_ERROR"
    assert identity.rejected >= 1
    assert len(driver[1]) == 1  # no connection or SELECT for the second trial
    assert [sql for sql, _ in connection.queries if sql in (SQL, CANDIDATE)] == [SQL, CANDIDATE]
    assert connection.closed


@pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_INTEGRATION") != "1",
    reason="requires DB_AGENT_MYSQL_INTEGRATION=1 and existing public local fixture",
)
@pytest.mark.parametrize("side", [0, 1])
def test_real_pair_honors_revocation_before_each_select(monkeypatch, side):
    settings = load_database_settings()
    assert (settings.host, settings.port, settings.database, settings.user) == (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    )
    assert "orders" in settings.allowed_tables
    analysis, query = load_analysis_settings(), load_query_settings()
    assert (analysis.timeout_seconds, analysis.review_scan_rows,
            analysis.review_join_rows, analysis.review_sort_rows) == (10, 100000, 1000000, 100000)
    assert (query.max_rows, query.max_result_bytes, query.execution_timeout_seconds,
            query.operation_timeout_seconds) == (100, 32768, 5, 15)
    statements = (
        "SELECT id, total_amount FROM orders WHERE id = 1001",
        "SELECT id, total_amount FROM orders WHERE id = 1001 AND status = 'paid'",
    )
    identity = RevocableIdentity()
    visits, dispatched = [], []
    original_execute = aiomysql.SSCursor.execute

    async def observe_execute(cursor, sql, args=None):
        # This is a transparent observation wrapper over the real network driver,
        # not a connection/result replacement or substitute query.
        if sql in statements:
            dispatched.append(sql)
        return await original_execute(cursor, sql, args)

    async def before_select(connection):
        visits.append(connection)
        if len(visits) == side + 1:
            identity.active = False

    monkeypatch.setattr(aiomysql.SSCursor, "execute", observe_execute)
    outcomes = asyncio.run(MetadataConnector(
        settings, authorization_check=identity,
    ).compare_checked(*statements, analysis, query, before_select=before_select))

    assert len(visits) == side + 1
    assert identity.rejected >= 1
    assert_denied(outcomes, side, "not_started")
    assert dispatched == list(statements[:side])
    assert all(connection.closed for connection in visits)
    if side:
        assert outcomes[0]["execution_status"] == "completed"
        # Independently derived from fixture order 1001; no candidate rerun oracle.
        assert outcomes[0]["result"]["rows"] == [[1001, "100.00"]]
