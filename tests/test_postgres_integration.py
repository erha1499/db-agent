"""Opt-in reader-only checks against the fixed local PostgreSQL 18.6 fixture.

No tests create data or change database permissions. Literal expected values are
derived from postgres_business.sql, independently of candidate SQL execution.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from db_agent.analysis import SqlAnalysisService
from db_agent.config import AnalysisSettings, PostgreSQLSettings, QuerySettings
from db_agent.db import DatabaseError
from db_agent.postgres import PostgreSQLConnector
from db_agent.query import QueryService

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_POSTGRES_INTEGRATION") != "1",
    reason="requires DB_AGENT_POSTGRES_INTEGRATION=1 and the fixed PostgreSQL fixture",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fixed_default_budgets(monkeypatch):
    # These acceptance checks cannot inherit relaxed shell policy/budget values.
    for key in tuple(os.environ):
        if key.startswith(("DB_AGENT_ANALYSIS_", "DB_AGENT_QUERY_")):
            monkeypatch.delenv(key)


@pytest.fixture
def pg_settings():
    settings = PostgreSQLSettings(_env_file=ROOT / ".env.postgres")
    target = (settings.host, settings.port, settings.database, settings.schema_name, settings.user)
    assert target == (
        "127.0.0.1", 15432, "db_agent_pg", "business", "db_agent_reader",
    ), "tests require the fixed local PostgreSQL reader"
    assert settings.allowed_tables == ("customers", "orders", "order_items")
    return settings


@pytest.fixture
def connector(pg_settings):
    return PostgreSQLConnector(pg_settings)


def execute(connector, sql, **limits):
    return asyncio.run(QueryService(
        connector, AnalysisSettings(_env_file=None), QuerySettings(_env_file=None, **limits),
    ).execute(sql))


def test_real_connection_identity_and_authorized_metadata(connector):
    async def inspect():
        return await connector.check(), await connector.list_tables()

    check, tables = asyncio.run(inspect())
    assert check == {
        "connection_ok": True, "database": "db_agent_pg", "schema": "business",
        "dialect": "postgres", "server_version": "18.6", "readonly_identity_verified": True,
    }
    assert [table["name"] for table in tables["tables"]] == [
        "customers", "order_items", "orders",
    ]
    assert all(table["type"] == "BASE TABLE" for table in tables["tables"])


def test_real_columns_indexes_and_declared_foreign_key(connector):
    details = asyncio.run(connector.describe_table("orders"))
    assert [column["name"] for column in details["columns"]] == [
        "id", "order_no", "customer_id", "status", "total_amount", "created_at", "paid_at",
    ]
    assert [column["position"] for column in details["columns"]] == list(range(1, 8))
    assert [column["type"] for column in details["columns"]][-3:] == [
        "numeric(12,2)", "timestamp(6) without time zone", "timestamp(6) with time zone",
    ]
    index = [part for part in details["indexes"]
             if part["name"] == "idx_orders_customer_created"]
    assert [(part["column"], part["position"], part["unique"]) for part in index] == [
        ("customer_id", 1, False), ("created_at", 2, False),
    ]
    assert details["foreign_keys"] == [{
        "name": "orders_customer_id_fkey", "columns": ["customer_id"],
        "referenced_table": "customers", "referenced_columns": ["id"],
    }]
    assert details["foreign_keys_scope"] == "current_schema_authorized_tables"


def test_real_narrow_scope_does_not_return_foreign_targets(pg_settings):
    connector = PostgreSQLConnector(pg_settings.model_copy(update={"allowed_tables": ("orders",)}))

    async def inspect():
        details = await connector.describe_table("orders")
        listed = await connector.list_tables()
        with pytest.raises(DatabaseError, match="授权"):
            await connector.describe_table("customers")
        return details, listed

    details, listed = asyncio.run(inspect())
    assert details["foreign_keys"] == []
    assert [row["name"] for row in listed["tables"]] == ["orders"]


@pytest.mark.parametrize("sql,expected", [
    ("SELECT COUNT(*) AS n, SUM(total_amount) AS total FROM orders WHERE status='paid'",
     [[3, "130.00"]]),
    ("SELECT COUNT(*) AS n, SUM(total_amount) AS total FROM orders WHERE status='paid' "
     "AND created_at >= '2026-02-01' AND created_at < '2026-03-01'", [[2, "30.00"]]),
    ("SELECT COUNT(*) AS n, SUM(total_amount) AS total FROM orders WHERE status='paid' "
     "AND paid_at >= '2026-02-01+00' AND paid_at < '2026-03-01+00'", [[3, "130.00"]]),
    ("SELECT c.id, COUNT(o.id) AS n FROM customers AS c LEFT JOIN orders AS o "
     "ON c.id=o.customer_id GROUP BY c.id ORDER BY c.id",
     [[1, 2], [2, 2], [3, 1], [4, 0], [5, 1]]),
    ("SELECT SUM(i.quantity*i.unit_price-i.discount_amount) AS revenue "
     "FROM orders AS o INNER JOIN order_items AS i ON o.id=i.order_id WHERE o.status='paid'",
     [["130.00"]]),
    ("SELECT COUNT(*) AS n, SUM(total_amount) AS total FROM orders WHERE id=987654321",
     [[0, None]]),
    ("SELECT id FROM orders WHERE id=987654321", []),
    ("SELECT id,total_amount,created_at,paid_at FROM orders WHERE id=1003",
     [[1003, "50.00", "2026-02-02T10:00:00", None]]),
    ("SELECT id,total_amount,created_at,paid_at FROM orders WHERE id=1001",
     [[1001, "100.00", "2026-01-31T23:59:59", "2026-02-01T00:01:00+00:00"]]),
    ("SELECT COUNT(CASE WHEN status='paid' THEN 1 END) AS n, "
     "SUM(CASE WHEN status='paid' THEN total_amount ELSE 0 END) AS paid_total FROM orders",
     [[3, "130.00"]]),
    ("SELECT SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid_count, "
     "COUNT(CASE WHEN status='paid' THEN total_amount END) AS n FROM orders", [[3, 3]]),
    ("SELECT COUNT(CASE WHEN region IS NULL THEN 1 END) AS unknown_regions, "
     "SUM(CASE WHEN region IS NULL THEN 1 ELSE 0 END) AS n FROM customers", [[1, 1]]),
    ("SELECT c.id,COUNT(CASE WHEN o.status='paid' THEN 1 END) AS n, "
     "SUM(CASE WHEN o.status='paid' THEN o.total_amount END) AS total, "
     "SUM(CASE WHEN o.status='paid' THEN 1 ELSE 0 END) AS integer_count "
     "FROM customers AS c LEFT JOIN orders AS o ON c.id=o.customer_id "
     "GROUP BY c.id ORDER BY c.id",
     [[1, 2, "130.00", 2], [2, 0, None, 0], [3, 1, "0.00", 1],
      [4, 0, None, 0], [5, 0, None, 0]]),
    ("SELECT SUM(CASE WHEN id=987654321 THEN total_amount END) AS n FROM orders", [[None]]),
])
def test_real_business_and_conditional_aggregate_oracles(connector, sql, expected):
    report = execute(connector, sql)
    assert report["status"] == "ok", report
    assert report["decision"] == "ALLOW"
    assert report["execution_status"] == "completed"
    assert report["result"]["rows"] == expected
    assert report["result"]["row_count"] == len(expected)
    assert report["result"]["truncated"] is False
    assert report["result"]["server_statement_status"] == "completed"
    assert report["result"]["result_bytes"] == len(
        json.dumps(report["result"], ensure_ascii=False).encode()
    )


def test_real_duplicate_columns_large_integer_and_decimal_are_exact(connector):
    report = execute(connector, "SELECT o.id,c.id,9007199254740993 AS huge,0.10 AS precise "
                     "FROM orders AS o INNER JOIN customers AS c ON o.customer_id=c.id "
                     "WHERE o.id=1001")
    assert report["status"] == "ok", report
    assert [column["name"] for column in report["result"]["columns"]] == [
        "id", "id", "huge", "precise",
    ]
    assert report["result"]["rows"] == [[1001, 1, "9007199254740993", "0.10"]]


def test_real_row_limit_closes_connection_and_next_query_works(connector):
    async def run():
        service = QueryService(connector, AnalysisSettings(_env_file=None),
                               QuerySettings(_env_file=None, max_rows=2))
        return (await service.execute("SELECT id FROM orders ORDER BY id"),
                await service.execute("SELECT COUNT(*) AS n FROM orders"))

    partial, following = asyncio.run(run())
    assert partial["execution_status"] == "truncated", partial
    assert partial["result"]["rows"] == [[1001], [1002]]
    assert partial["result"]["truncation_reason"] == "row_limit"
    assert partial["result"]["server_statement_status"] == "unknown"
    assert following["execution_status"] == "completed"
    assert following["result"]["rows"] == [[6]]


@pytest.mark.parametrize("limit,sql", [
    (6, "SELECT id FROM orders ORDER BY id"),
    (2, "SELECT id FROM orders ORDER BY id LIMIT 2"),
])
def test_real_exact_row_boundary_is_complete(connector, limit, sql):
    report = execute(connector, sql, max_rows=limit)
    assert report["execution_status"] == "completed", report
    assert report["result"]["row_count"] == limit
    assert report["result"]["truncated"] is False


def test_real_byte_limit_marks_partial_result(connector):
    report = execute(connector, "SELECT id,order_no,customer_id,status,total_amount,"
                     "created_at,paid_at,order_no AS repeated FROM orders ORDER BY id",
                     max_result_bytes=1024)
    assert report["execution_status"] == "truncated", report
    assert report["result"]["truncation_reason"] == "byte_limit"
    assert 0 < report["result"]["row_count"] < 6
    assert report["result"]["result_bytes"] <= 1024


def test_real_column_limit_is_failure_without_result(connector):
    report = execute(connector, "SELECT id,total_amount FROM orders", max_columns=1)
    assert report["decision"] == "ALLOW"
    assert report["execution_status"] == "unknown"
    assert report["result"] is None
    assert report["error"]["code"] == "RESULT_LIMIT"


def test_real_large_scan_is_review_even_with_limit_and_small_scan_is_allowed(pg_settings):
    settings = PostgreSQLSettings(
        _env_file=ROOT / ".env.postgres", allowed_tables=("pg_scan_probe", "orders"),
    )
    connector = PostgreSQLConnector(settings)
    large = execute(connector, "SELECT id FROM pg_scan_probe LIMIT 1")
    assert large["decision"] == "REVIEW", large
    assert large["execution_status"] == "not_started"
    assert large["result"] is None
    assert "PLAN_LARGE_SCAN" in {finding["rule_id"] for finding in large["findings"]}
    small = execute(connector, "SELECT id FROM orders LIMIT 1")
    assert small["decision"] == "ALLOW" and small["execution_status"] == "completed", small


def test_real_explain_does_not_execute_a_runtime_division(connector):
    sql = "SELECT total_amount/(id-id) AS invalid_runtime_value FROM orders WHERE id=1001"

    async def check():
        service = SqlAnalysisService(connector, AnalysisSettings(_env_file=None))
        analysis = await service.analyze(sql)
        result = await QueryService(connector, AnalysisSettings(_env_file=None),
                                    QuerySettings(_env_file=None)).execute(sql)
        return analysis, result

    analysis, result = asyncio.run(check())
    assert analysis["decision"] == "ALLOW", analysis
    assert analysis["evidence_source"] == "postgres_explain_json"
    assert result["decision"] == "ALLOW"
    assert result["execution_status"] == "unknown" and result["result"] is None
    assert result["error"]["code"] == "DATA_ERROR"


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders WHERE id=1001", "SELECT id FROM orders FOR UPDATE",
    "SELECT id FROM orders; SELECT id FROM customers", "SELECT pg_sleep(1) FROM orders",
    "SELECT nextval('some_sequence') FROM orders", "SELECT id INTO some_table FROM orders",
    "SELECT id FROM business.pg_scan_probe", "SELECT relname FROM pg_catalog.pg_class",
    "SELECT id FROM orders UNION SELECT id FROM orders",
    "SELECT id FROM orders WHERE id IN (SELECT order_id FROM order_items)",
    "WITH candidate AS (SELECT id FROM orders) SELECT id FROM candidate",
    "SELECT ROW_NUMBER() OVER (ORDER BY id) FROM orders",
])
def test_live_config_static_denials_never_reach_connection(connector, monkeypatch, sql):
    @asynccontextmanager
    async def forbidden(*args, **kwargs):
        pytest.fail("a statically rejected SQL must not open a database connection")
        yield

    monkeypatch.setattr(connector, "_connection", forbidden)
    report = execute(connector, sql)
    assert report["decision"] in {"BLOCK", "UNKNOWN"}
    assert report["execution_status"] == "not_started"
    assert report["result"] is None


@pytest.mark.parametrize("changed", [{"max_metadata_rows": 1}, {"max_metadata_bytes": 1024}])
def test_real_metadata_budget_does_not_return_partial_schema(pg_settings, changed):
    connector = PostgreSQLConnector(pg_settings.model_copy(update=changed))
    with pytest.raises(DatabaseError) as error:
        asyncio.run(connector.describe_table("orders"))
    assert error.value.code == "RESULT_LIMIT"


def test_real_literal_changes_have_distinct_result_ids(connector):
    first = execute(connector, "SELECT id FROM orders WHERE id=1001")
    second = execute(connector, "SELECT id FROM orders WHERE id=1002")
    assert first["result"]["rows"] == [[1001]] and second["result"]["rows"] == [[1002]]
    assert first["sql_fingerprint"] == second["sql_fingerprint"]
    assert first["query_id"] != second["query_id"]
    assert first["result_id"] != second["result_id"]
