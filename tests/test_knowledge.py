"""Persistence/lifecycle contracts using synthetic metadata; no model/MySQL evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_agent import isolated_environment as isolated_environment
from test_agent_semantics import metadata as metadata

from db_agent.config import AnalysisSettings, load_database_settings
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.knowledge import (
    KnowledgeContext,
    KnowledgeError,
    KnowledgeStore,
    digest,
    references,
)


def document(**updates):
    return {
        "kind": "metric", "title": "支付成交额",
        "definition": "paid订单按paid_at统计total_amount。",
        "source": "tests/fixtures/mysql_business.sql", "source_version": "fixture-v1",
        "invalidation_condition": "支付状态或统计口径变化时由维护者撤销并创建新版本。",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "tables": ["orders"], **updates,
    }


def create(store, connector, **updates):
    return store.create(json.dumps(document(**updates)).encode(), connector, AnalysisSettings())


def confirm(store, connector, item):
    return asyncio.run(store.confirm(item["id"], item["digest"], connector, AnalysisSettings()))


@pytest.fixture
def setup(isolated_environment, metadata):
    return KnowledgeStore(), MetadataConnector(load_database_settings())


def test_immutable_confirmation_persists_to_fresh_store_with_source_and_scope(setup):
    store, connector = setup
    item = create(store, connector)
    with pytest.raises(KnowledgeError):
        KnowledgeContext([item["id"]], connector, AnalysisSettings())
    with pytest.raises(KnowledgeError):
        asyncio.run(store.confirm(item["id"], "0" * 64, connector, AnalysisSettings()))
    confirmed = confirm(store, connector, item)
    fresh = KnowledgeStore()
    assert fresh.get(item["id"], connector.knowledge_scope) == confirmed
    assert confirmed["state"] == "confirmed" and confirmed["schema_hashes"]["orders"]
    assert confirmed["payload"]["source_version"] == "fixture-v1"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.list(connector.knowledge_scope) == [confirmed]
    with pytest.raises(KnowledgeError):
        confirm(store, connector, item)


@pytest.mark.parametrize("change", [
    {"host": "another-host"}, {"port": 13307}, {"database": "another_db"},
    {"user": "another_reader"}, {"allowed_tables": ("orders",)},
])
def test_source_account_and_allowlist_changes_never_reuse(setup, change):
    store, connector = setup
    item = confirm(store, connector, create(store, connector))
    other = MetadataConnector(load_database_settings().model_copy(update=change))
    assert store.list(other.knowledge_scope) == []
    with pytest.raises(KnowledgeError):
        KnowledgeContext([item["id"]], other, AnalysisSettings())
    with pytest.raises(KnowledgeError):
        store.revoke(item["id"], other.knowledge_scope, "wrong scope")


@pytest.mark.parametrize("updates", [
    {"approved": True}, {"rows": [[1, "private row"]]}, {"result_id": "old-result"},
    {"source": ""}, {"source_version": ""}, {"definition": " "},
    {"expires_at": "2099-01-01"}, {"expires_at": "2020-01-01T00:00:00Z"},
    {"tables": ["orders", "orders"]}, {"kind": "sql_template"},
    {"sql": "SELECT id FROM orders"}, {"kind": "relationship"},
    {"kind": "sql_template", "sql": "DELETE FROM orders"},
    {"kind": "sql_template", "sql": "SELECT id FROM customers"},
    {"kind": "sql_template", "sql": "SELECT id FROM orders; SELECT id FROM orders"},
    {"kind": "sql_template", "sql": "SELECT SLEEP(2) FROM orders"},
])
def test_invalid_unconfirmed_or_authorizing_inputs_rejected(setup, updates):
    store, connector = setup
    with pytest.raises(KnowledgeError):
        create(store, connector, **updates)


def test_forbidden_table_fails_current_policy(setup):
    store, connector = setup
    with pytest.raises(DatabaseError) as exc:
        create(store, connector, tables=["secrets"])
    assert exc.value.code == "PERMISSION_DENIED"


def test_template_checks_real_columns_at_confirmation(setup):
    store, connector = setup
    good = create(store, connector, kind="sql_template", sql="SELECT id FROM orders ORDER BY id")
    confirm(store, connector, good)
    bad = create(store, connector, kind="sql_template", sql="SELECT missing FROM orders")
    with pytest.raises(KnowledgeError):
        confirm(store, connector, bad)
    assert store.get(bad["id"], connector.knowledge_scope)["state"] == "draft"


def test_relationship_requires_complete_current_declared_column_pairs(setup, monkeypatch):
    store, connector = setup
    original = MetadataConnector.describe_table

    async def describe(self, table):
        data = await original(self, table)
        data["foreign_keys"] = [dict(
            columns=["customer_id", "id"], referenced_table="customers",
            referenced_columns=["id", "name"],
        )] if table == "orders" else []
        return data

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    relationship = dict(table="orders", columns=["customer_id"], referenced_table="customers",
                        referenced_columns=["id"])
    item = create(store, connector, kind="relationship", tables=["orders", "customers"],
                  relationship=relationship)
    with pytest.raises(KnowledgeError):
        confirm(store, connector, item)
    relationship.update(columns=["customer_id", "id"], referenced_columns=["id", "name"])
    good = create(store, connector, kind="relationship", tables=["orders", "customers"],
                  relationship=relationship)
    assert confirm(store, connector, good)["state"] == "confirmed"


@pytest.mark.parametrize("failure", ["revoke", "expiry", "schema", "corrupt", "missing"])
def test_loaded_context_revalidates_before_later_use(setup, failure):
    store, connector = setup
    item = confirm(store, connector, create(store, connector))
    context = KnowledgeContext([item["id"]], connector, AnalysisSettings())
    schemas = {"orders": asyncio.run(connector.describe_table("orders"))}
    context.validate(schemas)
    if failure == "revoke":
        store.revoke(item["id"], connector.knowledge_scope, "metric definition changed")
    elif failure == "schema":
        schemas["orders"]["columns"][0]["type"] = "varchar(80)"
    elif failure == "missing":
        store.path.unlink()
    else:
        with store.connection() as db:
            if failure == "expiry":
                payload = item["payload"] | {"expires_at": "2020-01-01T00:00:00Z"}
                db.execute("UPDATE knowledge SET payload=?,digest=? WHERE id=?",
                           (json.dumps(payload), digest(payload), item["id"]))
            else:
                db.execute("UPDATE knowledge SET payload='broken' WHERE id=?", (item["id"],))
    with pytest.raises(KnowledgeError):
        context.validate(schemas)


def test_store_failure_never_returns_success_or_silently_ignores_reference(setup):
    store, connector = setup
    store.path.parent.mkdir(parents=True)
    store.path.write_text("corrupt SQLite")
    with pytest.raises(KnowledgeError) as exc:
        create(store, connector)
    assert exc.value.code == "KNOWLEDGE_STORAGE"


def test_no_implicit_search_no_file_path_and_bounded_references():
    identifier = "a" * 32
    assert references("支付成交额") == []
    assert references(f"[[knowledge:{identifier}]] [[knowledge:{identifier}]]") == [identifier]
    for bad in ("[[knowledge:/tmp/secret]]", "[[knowledge:abc]]"):
        with pytest.raises(KnowledgeError):
            references(bad)
    with pytest.raises(KnowledgeError):
        references(" ".join(f"[[knowledge:{x * 32}]]" for x in "abcd"))
    assert not Path("outputs/knowledge").exists()


def test_cli_management_does_not_require_model_and_retains_explicit_receipt(
    setup, monkeypatch, capsys,
):
    import io

    from db_agent.cli import main

    _, connector = setup
    for name in ("DB_AGENT_API_KEY", "DB_AGENT_MODEL", "DB_AGENT_OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(document()).encode())))
    assert main(["knowledge", "create", "--stdin"]) == 0
    draft = json.loads(capsys.readouterr().out)
    assert main(["knowledge", "show", draft["id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["digest"] == draft["digest"] and shown["state"] == "draft"
    assert main(["knowledge", "confirm", draft["id"], "--digest", "bad"]) == 1
    assert "KNOWLEDGE_INVALID" in capsys.readouterr().err
    assert main(["knowledge", "confirm", draft["id"], "--digest", draft["digest"]]) == 0
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed["state"] == "confirmed"
    assert main(["knowledge", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == draft["id"]
    assert main(["knowledge", "revoke", draft["id"], "--reason", "业务口径变化"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "revoked"


def test_expiry_during_confirmation_does_not_confirm(setup, monkeypatch):
    from datetime import datetime as RealDatetime

    from db_agent import knowledge

    store, connector = setup
    item = create(store, connector)
    original = MetadataConnector.describe_table

    class ExpiredClock:
        @staticmethod
        def now(tz):
            return RealDatetime.now(tz) + timedelta(days=31)

    async def describe(self, table):
        schema = await original(self, table)
        monkeypatch.setattr(knowledge, "datetime", ExpiredClock)
        return schema

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    # parse_time uses datetime.fromisoformat as well.
    ExpiredClock.fromisoformat = RealDatetime.fromisoformat
    with pytest.raises(KnowledgeError):
        confirm(store, connector, item)
    assert store.get(item["id"], connector.knowledge_scope)["state"] == "draft"
