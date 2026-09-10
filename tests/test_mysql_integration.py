"""Opt-in checks against the seeded, local MySQL synthetic fixture.

Enable with DB_AGENT_MYSQL_INTEGRATION=1 after configuring the reader account and
seeding tests/fixtures/mysql_business.sql. These tests never create or seed data.
"""

import asyncio
import os
from contextlib import asynccontextmanager

import aiomysql
import pytest

from db_agent.config import load_database_settings
from db_agent.db import DatabaseError, MetadataConnector

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_INTEGRATION") != "1",
    reason="requires DB_AGENT_MYSQL_INTEGRATION=1 and the seeded local MySQL fixture",
)


@pytest.fixture
def database_settings():
    return load_database_settings()


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
