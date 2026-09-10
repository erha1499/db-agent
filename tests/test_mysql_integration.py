"""Opt-in checks against the seeded, local MySQL synthetic fixture.

Enable with DB_AGENT_MYSQL_INTEGRATION=1 after configuring the reader account and
seeding tests/fixtures/mysql_business.sql. These tests never create or seed data.
"""

import asyncio
import os
from contextlib import asynccontextmanager

import aiomysql
import pytest

from db_agent.analysis import SqlAnalysisService
from db_agent.config import AnalysisSettings, load_database_settings
from db_agent.db import DatabaseError, MetadataConnector

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_INTEGRATION") != "1",
    reason="requires DB_AGENT_MYSQL_INTEGRATION=1 and the seeded local MySQL fixture",
)


@pytest.fixture
def database_settings():
    settings = load_database_settings()
    # This fixture only tests the original public tables, even after ecommerce is loaded.
    original = ("customers", "orders", "order_items")
    assert set(original).issubset(settings.allowed_tables)
    return settings.model_copy(update={"allowed_tables": original})


def test_connection_and_authorized_fixture_tables(database_settings):
    async def check_metadata():
        connector = MetadataConnector(database_settings)
        check = await connector.check()
        tables = await connector.list_tables()
        return check, tables

    check, tables = asyncio.run(check_metadata())

    assert check["connection_ok"] is True
    assert check["database"] == database_settings.database
    assert check["server_version"]
    assert tables["database"] == database_settings.database
    assert [table["name"] for table in tables["tables"]] == [
        "customers",
        "order_items",
        "orders",
    ]
    assert all(table["type"] == "BASE TABLE" for table in tables["tables"])


def test_orders_columns_and_composite_index_order(database_settings):
    details = asyncio.run(MetadataConnector(database_settings).describe_table("orders"))

    assert details["table"] == "orders"
    assert [column["name"] for column in details["columns"]] == [
        "id",
        "order_no",
        "customer_id",
        "status",
        "total_amount",
        "created_at",
        "paid_at",
    ]
    assert [column["position"] for column in details["columns"]] == list(range(1, 8))
    composite_index = [
        index for index in details["indexes"] if index["name"] == "idx_orders_customer_created"
    ]
    assert [(index["column"], index["position"]) for index in composite_index] == [
        ("customer_id", 1),
        ("created_at", 2),
    ]
    assert all(index["unique"] is False for index in composite_index)


def test_narrow_allowlist_filters_listing_and_blocks_other_tables(database_settings):
    async def inspect_narrow_scope():
        settings = database_settings.model_copy(update={"allowed_tables": ("orders",)})
        connector = MetadataConnector(settings)
        listed = await connector.list_tables()
        with pytest.raises(DatabaseError) as exc:
            await connector.describe_table("customers")
        return listed, exc.value.code

    listed, code = asyncio.run(inspect_narrow_scope())

    assert [table["name"] for table in listed["tables"]] == ["orders"]
    assert code == "PERMISSION_DENIED"


@pytest.mark.parametrize("table", ["mysql.user", "orders; DROP TABLE orders", "orders` OR 1=1"])
def test_qualified_and_injected_table_names_are_rejected(database_settings, table):
    with pytest.raises(DatabaseError) as exc:
        asyncio.run(MetadataConnector(database_settings).describe_table(table))

    assert exc.value.code == "INVALID_TABLE"


def test_explicitly_allowed_missing_table_reports_not_found(database_settings):
    missing_table = "integration_missing_table_7b2a6f"
    settings = database_settings.model_copy(update={"allowed_tables": (missing_table,)})

    with pytest.raises(DatabaseError) as exc:
        asyncio.run(MetadataConnector(settings).describe_table(missing_table))

    assert exc.value.code == "TABLE_NOT_FOUND"


def test_real_metadata_stops_at_the_result_row_limit(database_settings):
    settings = database_settings.model_copy(
        update={"allowed_tables": ("orders",), "max_metadata_rows": 1}
    )

    with pytest.raises(DatabaseError) as exc:
        asyncio.run(MetadataConnector(settings).describe_table("orders"))

    assert exc.value.code == "RESULT_LIMIT"


@asynccontextmanager
async def _reader_connection(settings):
    try:
        connection = await aiomysql.connect(
            host=settings.host,
            port=settings.port,
            db=settings.database,
            user=settings.user,
            password=settings.password.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            autocommit=False,
            charset="utf8mb4",
            local_infile=False,
            echo=False,
        )
    except (aiomysql.Error, OSError):
        raise AssertionError("integration reader connection failed") from None
    try:
        async with asyncio.timeout(settings.metadata_timeout_seconds):
            yield connection
    finally:
        connection.close()


async def _denied_statement_error_number(settings, query):
    async with _reader_connection(settings) as connection:
        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                try:
                    await cursor.execute(query)
                except aiomysql.Error as exc:
                    # Only retain the errno; driver messages may identify the account or host.
                    return exc.args[0] if exc.args else None
                return None
        finally:
            await connection.rollback()


def test_reader_database_privileges_deny_system_table_select(database_settings):
    # LIMIT 0 avoids reading system rows even if the account was misconfigured.
    code = asyncio.run(
        _denied_statement_error_number(database_settings, "SELECT 1 FROM mysql.user LIMIT 0")
    )

    assert code == 1142, "reader must not have SELECT permission on mysql.user"


def test_reader_database_privileges_deny_insert(database_settings):
    # The constant false predicate produces zero rows even with excessive privileges.
    # START TRANSACTION and ROLLBACK also protect this fixed permission probe.
    query = """
        INSERT INTO orders (
            id, order_no, customer_id, status, total_amount, created_at, paid_at
        )
        SELECT 18446744073709551000, 'SYN-PERMISSION-PROBE', 1, 'pending', 0,
               '2026-01-01 00:00:00', NULL
        WHERE FALSE
    """
    code = asyncio.run(_denied_statement_error_number(database_settings, query))

    assert code == 1142, "reader must not have INSERT permission on orders"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, customer_id FROM orders ORDER BY created_at LIMIT 10",
        "SELECT o.id, c.id FROM orders AS o INNER JOIN customers AS c ON o.customer_id = c.id",
        "SELECT c.id, COUNT(o.id) FROM customers AS c LEFT JOIN orders AS o "
        "ON c.id = o.customer_id GROUP BY c.id ORDER BY c.id",
        "SELECT customer_id, SUM(total_amount) FROM orders GROUP BY customer_id",
        "SELECT id FROM orders WHERE status LIKE '%paid%'",
        "SELECT COUNT(*) FROM orders",
        "SELECT id FROM orders WHERE 1 = 0",
    ],
)
def test_real_supported_sql_produces_plan_evidence(database_settings, sql):
    report = asyncio.run(
        SqlAnalysisService(
            MetadataConnector(database_settings),
            AnalysisSettings(_env_file=None),
        ).analyze(sql)
    )
    assert report["decision"] == "ALLOW", report["findings"]
    assert report["evidence_source"] == "mysql_explain_json"
    assert report["server_version"].startswith("8.4.")
    assert report["plan_summary"] is not None


def test_real_small_scan_can_trigger_configured_review_without_running_query(database_settings):
    # Lower the policy threshold on the six-row fixture; this is not a large-load test.
    limits = AnalysisSettings(_env_file=None, review_scan_rows=1)
    report = asyncio.run(
        SqlAnalysisService(MetadataConnector(database_settings), limits).analyze(
            "SELECT id, total_amount FROM orders LIMIT 1"
        )
    )
    assert report["decision"] == "REVIEW"
    assert "PLAN_LARGE_SCAN" in {finding["rule_id"] for finding in report["findings"]}
    tables = report["plan_summary"]["tables"]
    assert tables[0]["rows_examined_per_scan"] > limits.review_scan_rows


def test_real_secondary_index_can_cover_primary_key_projection(database_settings):
    report = asyncio.run(SqlAnalysisService(
        MetadataConnector(database_settings), AnalysisSettings(_env_file=None),
    ).analyze("SELECT id, customer_id FROM orders ORDER BY created_at LIMIT 10"))
    table = report["plan_summary"]["tables"][0]
    assert table["using_index"] is True
    assert table["key"] == "idx_orders_customer_created"
    assert "PLAN_COVERING_INDEX" in {finding["rule_id"] for finding in report["findings"]}


def test_real_invalid_column_is_unknown_and_raw_error_is_hidden(database_settings):
    import json

    report = asyncio.run(
        SqlAnalysisService(
            MetadataConnector(database_settings),
            AnalysisSettings(_env_file=None),
        ).analyze("SELECT private_missing_column_83e2 FROM orders")
    )
    assert report["decision"] == "UNKNOWN"
    assert report["plan_summary"] is None
    assert "SQL_REFERENCE_ERROR" in {finding["rule_id"] for finding in report["findings"]}
    assert "private_missing_column_83e2" not in json.dumps(report)


def test_real_literal_change_gets_fresh_plan_and_distinct_report(database_settings):
    async def assess():
        service = SqlAnalysisService(
            MetadataConnector(database_settings),
            AnalysisSettings(
                _env_file=None,
            ),
        )
        return [
            await service.analyze(sql)
            for sql in (
                "SELECT id FROM orders WHERE id = 1001",
                "SELECT id FROM orders WHERE id = 987654321",
            )
        ]

    first, second = asyncio.run(assess())
    assert first["report_id"] != second["report_id"]
    assert first["sql_fingerprint"] == second["sql_fingerprint"]
    assert first["evidence_source"] == second["evidence_source"] == "mysql_explain_json"
    assert first["plan_summary"] != second["plan_summary"]
