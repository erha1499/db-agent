"""Opt-in, read-only assertions against the public, seeded MySQL business fixture."""

import asyncio
import json
import os

import pytest

from db_agent.config import AnalysisSettings, QuerySettings, load_database_settings
from db_agent.db import MetadataConnector
from db_agent.query import QueryService

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_INTEGRATION") != "1",
    reason="requires DB_AGENT_MYSQL_INTEGRATION=1 and the seeded local MySQL fixture",
)


@pytest.fixture
def connector():
    settings = load_database_settings()
    assert (settings.host, settings.port, settings.database) == ("127.0.0.1", 13306, "db_agent")
    return MetadataConnector(settings)


def execute(connector, sql, **limits):
    return asyncio.run(QueryService(
        connector, AnalysisSettings(_env_file=None), QuerySettings(_env_file=None, **limits),
    ).execute(sql))


@pytest.mark.parametrize(
    ("sql", "rows"),
    [
        (
            "SELECT COUNT(*) AS order_count, SUM(total_amount) AS total "
            "FROM orders WHERE status = 'paid'",
            [[3, "130.00"]],
        ),
        (
            "SELECT COUNT(*) AS order_count, SUM(total_amount) AS total FROM orders "
            "WHERE status = 'paid' AND created_at >= '2026-02-01' AND created_at < '2026-03-01'",
            [[2, "30.00"]],
        ),
        (
            "SELECT COUNT(*) AS order_count, SUM(total_amount) AS total FROM orders "
            "WHERE status = 'paid' AND paid_at >= '2026-02-01' AND paid_at < '2026-03-01'",
            [[3, "130.00"]],
        ),
        (
            "SELECT c.id, COUNT(o.id) AS order_count FROM customers AS c "
            "LEFT JOIN orders AS o ON c.id = o.customer_id GROUP BY c.id ORDER BY c.id",
            [[1, 2], [2, 2], [3, 1], [4, 0], [5, 1]],
        ),
        (
            "SELECT SUM(i.quantity * i.unit_price - i.discount_amount) AS revenue "
            "FROM orders AS o INNER JOIN order_items AS i ON o.id = i.order_id "
            "WHERE o.status = 'paid'",
            [["130.00"]],
        ),
        (
            "SELECT id, total_amount, created_at, paid_at FROM orders WHERE id = 1003",
            [[1003, "50.00", "2026-02-02T10:00:00", None]],
        ),
        (
            "SELECT COUNT(*) AS n, SUM(total_amount) AS total FROM orders WHERE id = 987654321",
            [[0, None]],
        ),
        ("SELECT id FROM orders WHERE id = 987654321", []),
        ("SELECT id FROM orders WHERE status LIKE '%paid%' ORDER BY id", [[1001], [1002], [1004]]),
    ],
)
def test_real_business_results_have_independent_expected_values(connector, sql, rows):
    # Expected values are hand-derived from the fixed fixture, not from another
    # execution of the candidate SQL or an LLM's assessment of the result.
    report = execute(connector, sql)
    assert report["status"] == "ok", report["error"]
    assert report["decision"] == "ALLOW"
    assert report["execution_status"] == "completed"
    assert report["result"]["rows"] == rows
    assert report["result"]["row_count"] == len(rows)
    assert report["result"]["truncated"] is False
    assert report["result"]["server_statement_status"] == "completed"
    assert report["result"]["result_bytes"] == len(
        json.dumps(report["result"], ensure_ascii=False).encode()
    )
    assert report["result_id"] and report["session_time_zone"] == "+00:00"


def test_real_duplicate_column_labels_preserve_both_values(connector):
    report = execute(
        connector, "SELECT o.id, c.id FROM orders AS o "
        "INNER JOIN customers AS c ON o.customer_id = c.id WHERE o.id = 1001",
    )
    assert report["status"] == "ok", report["error"]
    assert [column["name"] for column in report["result"]["columns"]] == ["id", "id"]
    assert report["result"]["rows"] == [[1001, 1]]


def test_real_row_limit_marks_partial_results_and_next_connection_works(connector):
    async def run():
        service = QueryService(
            connector, AnalysisSettings(_env_file=None), QuerySettings(_env_file=None, max_rows=2),
        )
        partial = await service.execute("SELECT id FROM orders ORDER BY id")
        following = await service.execute("SELECT COUNT(*) AS n FROM orders")
        return partial, following

    partial, following = asyncio.run(run())
    assert partial["status"] == "ok", partial["error"]
    assert partial["execution_status"] == "truncated"
    assert partial["result"]["rows"] == [[1001], [1002]]
    assert partial["result"]["truncation_reason"] == "row_limit"
    assert partial["result"]["server_statement_status"] == "unknown"
    assert following["execution_status"] == "completed"
    assert following["result"]["rows"] == [[6]]


def test_real_exact_row_boundary_and_explicit_sql_limit_are_complete(connector):
    for sql, limit in (
        ("SELECT id FROM orders ORDER BY id", 6),
        ("SELECT id FROM orders ORDER BY id LIMIT 2", 2),
    ):
        report = execute(connector, sql, max_rows=limit)
        assert report["status"] == "ok", report["error"]
        assert report["result"]["row_count"] == limit
        assert report["result"]["truncated"] is False


def test_real_byte_limit_returns_a_bounded_partial_result(connector):
    report = execute(
        connector,
        "SELECT id, order_no, customer_id, status, total_amount, created_at, paid_at, "
        "order_no AS repeated_order_no FROM orders ORDER BY id",
        max_result_bytes=1024,
    )
    assert report["status"] == "ok", report["error"]
    assert report["execution_status"] == "truncated"
    assert report["result"]["truncation_reason"] == "byte_limit"
    assert 0 < report["result"]["row_count"] < 6
    assert report["result"]["rows"][0][0] == 1001
    assert report["result"]["result_bytes"] <= 1024


def test_real_result_column_limit_is_not_reported_as_query_success(connector):
    report = execute(connector, "SELECT id, total_amount FROM orders", max_columns=1)
    assert report["decision"] == "ALLOW"
    assert report["status"] == "error"
    assert report["result"] is None
    assert report["error"]["code"] == "RESULT_LIMIT"


def test_real_scan_review_does_not_return_business_rows(connector):
    report = asyncio.run(QueryService(
        connector, AnalysisSettings(_env_file=None, review_scan_rows=1),
        QuerySettings(_env_file=None),
    ).execute("SELECT id, total_amount FROM orders LIMIT 1"))
    assert report["decision"] == "REVIEW"
    assert report["status"] == "rejected"
    assert report["execution_status"] == "not_started"
    assert report["result"] is None


def test_real_unknown_column_has_no_result_or_raw_error(connector):
    report = execute(connector, "SELECT private_missing_column_83e2 FROM orders")
    assert report["decision"] == "UNKNOWN"
    assert report["status"] == "error"
    assert report["execution_status"] == "not_started"
    assert report["result"] is None
    assert report["error"]["code"] == "SQL_REFERENCE_ERROR"
    assert "private_missing_column_83e2" not in json.dumps(report)


def test_real_literal_changes_do_not_reuse_results_or_authorization(connector):
    async def run():
        service = QueryService(
            connector, AnalysisSettings(_env_file=None), QuerySettings(_env_file=None)
        )
        return [await service.execute(f"SELECT id FROM orders WHERE id = {value}")
                for value in (1001, 1002)]

    first, second = asyncio.run(run())
    assert first["result"]["rows"] == [[1001]]
    assert second["result"]["rows"] == [[1002]]
    # The literal-redacted fingerprint is a log grouping key, never an execution credential.
    assert first["sql_fingerprint"] == second["sql_fingerprint"]
    assert first["query_id"] != second["query_id"]
    assert first["result_id"] != second["result_id"]


@pytest.mark.parametrize("revoke", [False, True])
def test_real_knowledge_metadata_and_lifecycle_on_select_connection(connector, tmp_path, revoke):
    from datetime import UTC, datetime, timedelta

    from db_agent.knowledge import KnowledgeContext, KnowledgeStore

    store = KnowledgeStore(tmp_path / "knowledge" / "store.sqlite3")
    limits = AnalysisSettings(_env_file=None)
    raw = json.dumps({
        "kind": "metric", "title": "synthetic paid order count",
        "definition": "Count orders whose status is paid.",
        "source": "tests/fixtures/mysql_business.sql", "source_version": "fixture-v1",
        "invalidation_condition": "Revoke if status semantics change.",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "tables": ["orders"],
    }).encode()

    async def exercise():
        draft = store.create(raw, connector, limits)
        item = await store.confirm(draft["id"], draft["digest"], connector, limits)
        context = KnowledgeContext([item["id"]], connector, limits, store)
        current = {"orders": await connector.describe_table("orders")}
        context.validate(current)

        async def guard(connection):
            fresh = {"orders": await connector.describe_for_query(connection, "orders")}
            if revoke:
                store.revoke(item["id"], connector.knowledge_scope, "synthetic lifecycle veto")
            context.validate(fresh)

        return await QueryService(
            connector, limits, QuerySettings(_env_file=None), before_select=guard,
        ).execute("SELECT COUNT(*) AS n FROM orders WHERE status = 'paid'")

    report = asyncio.run(exercise())
    if revoke:
        assert report["status"] == "error" and report["result"] is None
        assert report["execution_status"] == "not_started"
        assert report["error"]["code"] == "KNOWLEDGE_UNAVAILABLE"
    else:
        assert report["status"] == "ok" and report["execution_status"] == "completed"
        assert report["result"]["rows"] == [[3]]
