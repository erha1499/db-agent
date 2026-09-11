"""Opt-in PostgreSQL service/Web tests against the fixed 18.6 synthetic fixture.

Every successful SQL/metadata operation below reaches the real reader connector.
Only model HTTP responses are explicit protocol doubles; these are not natural
language accuracy measurements. SQLite and identity files live under tmp_path.
Source changes test rejection of old scopes without connecting to invented targets.
No DDL, grants, fixture changes, real model credentials, or business writes occur.
"""

import asyncio
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_agent import completion, tool_completion
from test_agent import stub_server as stub_server
from test_web import create, start, terminal
from test_web_identity import HASH, HEADERS, login, save

from db_agent import postgres, web
from db_agent.config import (
    AnalysisSettings,
    DatabaseSettings,
    PostgreSQLSettings,
    QuerySettings,
    Settings,
)
from db_agent.conversations import source_scope
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.knowledge import KnowledgeContext, KnowledgeError, KnowledgeStore
from db_agent.optimization import OptimizationService
from db_agent.postgres import PostgreSQLConnector
from db_agent.query import QueryService

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_POSTGRES_INTEGRATION") != "1",
    reason="requires DB_AGENT_POSTGRES_INTEGRATION=1 and the fixed PostgreSQL reader fixture",
)
ROOT = Path(__file__).resolve().parents[1]
TOTAL = "SELECT SUM(total_amount) AS total FROM orders WHERE status='paid'"
CASE_TOTAL = (
    "SELECT SUM(CASE WHEN status='paid' THEN total_amount ELSE 0 END) AS total FROM orders"
)


@pytest.fixture
def pg_settings():
    settings = PostgreSQLSettings(_env_file=ROOT / ".env.postgres")
    actual_target = (
        settings.host, settings.port, settings.database, settings.schema_name, settings.user,
    )
    assert actual_target == (
        "127.0.0.1", 15432, "db_agent_pg", "business", "db_agent_reader",
    ), "tests require the fixed local PostgreSQL reader"
    assert settings.allowed_tables == ("customers", "orders", "order_items")
    return settings


@pytest.fixture
def limits():
    # Acceptance budgets are explicit and cannot be enlarged by shell settings.
    return (
        AnalysisSettings(
            _env_file=None, max_sql_bytes=16384, max_ast_nodes=512, max_ast_depth=32,
            max_tables=8, max_plan_bytes=65536, max_plan_nodes=256, timeout_seconds=10,
            review_scan_rows=100000, review_join_rows=1000000, review_sort_rows=100000,
        ),
        QuerySettings(
            _env_file=None, max_rows=100, max_result_bytes=32768, max_columns=64,
            execution_timeout_seconds=5, operation_timeout_seconds=15,
        ),
    )


def draft(**changes):
    return {
        "kind": "metric", "title": "合成支付成交额",
        "definition": "status='paid'的订单按paid_at归属日期汇总total_amount。",
        "source": "tests/fixtures/postgres_business.sql", "source_version": "postgres-fixture-v1",
        "invalidation_condition": "状态或时间口径变化时由维护者撤销。",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "tables": ["orders"], **changes,
    }


def confirmed(store, connector, analysis, **changes):
    item = store.create(json.dumps(draft(**changes)).encode(), connector, analysis)
    return asyncio.run(store.confirm(item["id"], item["digest"], connector, analysis))


def test_compare_real_complete_results_in_one_readonly_snapshot_per_pair(pg_settings, limits):
    connector = PostgreSQLConnector(pg_settings)
    snapshots = []

    async def observe(connection):
        # Trusted test-only session probe; no candidate SQL is substituted.
        rows = await connector._fetch(connection, """
            SELECT pg_catalog.pg_current_snapshot()::text AS snapshot,
                   pg_catalog.pg_backend_pid() AS pid,
                   current_setting('transaction_isolation') AS isolation,
                   current_setting('transaction_read_only') AS readonly
        """)
        snapshots.append((id(connection), rows[0]))

    report = asyncio.run(OptimizationService(
        connector, *limits, before_select=observe,
    ).compare(TOTAL, CASE_TOTAL, repeat=3))
    assert report["outcome"] == "observed_equal", report["error"]
    assert report["completed_trials"] == report["requested_trials"] == 3
    assert report["general_equivalence_proven"] is False
    assert report["independent_oracle_checked"] is False
    assert report["source_scope"] == connector.knowledge_scope
    assert [trial["execution_order"] for trial in report["trials"]] == [
        ["original", "candidate"], ["candidate", "original"], ["original", "candidate"],
    ]
    assert len(snapshots) == 6
    for index, trial in enumerate(report["trials"]):
        assert snapshots[2 * index] == snapshots[2 * index + 1]
        observed = snapshots[2 * index][1]
        assert observed["readonly"] == "on" and observed["isolation"] == "repeatable read"
        assert "postgres" in trial["snapshot"] and "innodb" not in trial["snapshot"]
        for side in ("original", "candidate"):
            evidence = trial[side]
            assert evidence["decision"] == "ALLOW" and evidence["execution_status"] == "completed"
            assert evidence["server_version"] == "18.6"
            assert evidence["result"]["rows"] == [["130.00"]]
            assert evidence["result"]["truncated"] is False
            assert evidence["plan_summary"]["dialect"] == "postgres"
    samples = report["performance"]["select_duration_ms_samples"]
    assert all(len(values) == 3 and all(value >= 0 for value in values)
               for values in samples.values())
    assert report["performance"]["general_speedup_proven"] is False


def test_compare_real_difference_keeps_independent_expected_totals(pg_settings, limits):
    report = asyncio.run(OptimizationService(PostgreSQLConnector(pg_settings), *limits).compare(
        TOTAL, "SELECT SUM(total_amount) AS total FROM orders",
    ))
    assert report["outcome"] == "different" and report["reason"] == "rows_differ"
    assert report["completed_trials"] == 1
    assert report["trials"][0]["original"]["result"]["rows"] == [["130.00"]]
    assert report["trials"][0]["candidate"]["result"]["rows"] == [["230.00"]]
    assert report["performance"] is None


def test_compare_real_truncation_stops_before_other_side(pg_settings, limits):
    analysis, query = limits
    query = query.model_copy(update={"max_rows": 2})
    statement = "SELECT id FROM orders ORDER BY id"
    report = asyncio.run(OptimizationService(
        PostgreSQLConnector(pg_settings), analysis, query,
    ).compare(statement, statement, repeat=3))
    assert report["outcome"] == "inconclusive" and report["completed_trials"] == 0
    assert report["requested_trials"] == 3 and len(report["trials"]) == 1
    trial = report["trials"][0]
    assert trial["original"]["result"]["rows"] == [[1001], [1002]]
    assert trial["original"]["execution_status"] == "truncated"
    assert trial["original"]["result"]["server_statement_status"] == "unknown"
    assert trial["candidate"]["execution_status"] == "not_started"
    assert trial["candidate"]["result"] is None and report["performance"] is None


@pytest.mark.parametrize("knowledge_kind", ["metric", "sql_template", "relationship"])
def test_real_knowledge_confirmation_and_revocation_lifecycle(
    pg_settings, limits, tmp_path, knowledge_kind,
):
    connector = PostgreSQLConnector(pg_settings)
    store = KnowledgeStore(tmp_path / "knowledge" / "items.sqlite3")
    analysis = limits[0]
    changes = {"kind": knowledge_kind}
    if knowledge_kind == "sql_template":
        changes["sql"] = TOTAL
    elif knowledge_kind == "relationship":
        changes.update(tables=["orders", "customers"], relationship={
            "table": "orders", "columns": ["customer_id"],
            "referenced_table": "customers", "referenced_columns": ["id"],
        })
    item = store.create(json.dumps(draft(**changes)).encode(), connector, analysis)
    with pytest.raises(KnowledgeError):
        KnowledgeContext([item["id"]], connector, analysis, store)
    with pytest.raises(KnowledgeError):
        asyncio.run(store.confirm(item["id"], "0" * 64, connector, analysis))
    receipt = asyncio.run(store.confirm(item["id"], item["digest"], connector, analysis))
    assert receipt["state"] == "confirmed" and receipt["digest"] == item["digest"]
    fresh_store = KnowledgeStore(store.path)
    assert fresh_store.get(item["id"], connector.knowledge_scope) == receipt
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    context = KnowledgeContext([item["id"]], connector, analysis, fresh_store)

    async def read_schemas():
        return {table: await connector.describe_table(table) for table in context.tables}

    context.validate(asyncio.run(read_schemas()))
    assert context.evidence[0]["source_version"] == "postgres-fixture-v1"
    with pytest.raises(KnowledgeError):
        asyncio.run(store.confirm(item["id"], item["digest"], connector, analysis))
    revoked = store.revoke(item["id"], connector.knowledge_scope, "synthetic definition changed")
    assert revoked["state"] == "revoked"
    with pytest.raises(KnowledgeError):
        context.validate_lifecycle()
    with pytest.raises(KnowledgeError):
        KnowledgeContext([item["id"]], connector, analysis, fresh_store)


@pytest.mark.parametrize("change", [
    {"schema_name": "other_business"}, {"user": "another_reader"},
    {"allowed_tables": ("orders",)},
])
def test_real_confirmed_pg_knowledge_never_reuses_changed_source_scope(
    pg_settings, limits, tmp_path, change,
):
    connector = PostgreSQLConnector(pg_settings)
    store = KnowledgeStore(tmp_path / "knowledge" / "items.sqlite3")
    item = confirmed(store, connector, limits[0])
    other = PostgreSQLConnector(pg_settings.model_copy(update=change))
    assert other.knowledge_scope != connector.knowledge_scope
    assert store.list(other.knowledge_scope) == []
    with pytest.raises(KnowledgeError):
        KnowledgeContext([item["id"]], other, limits[0], store)
    with pytest.raises(KnowledgeError):
        store.revoke(item["id"], other.knowledge_scope, "wrong scope")


def test_source_kind_and_schema_are_independently_bound_and_mysql_knowledge_is_rejected(
    pg_settings, limits, tmp_path,
):
    fields = {key: getattr(pg_settings, key) for key in (
        "kind", "schema_name", "host", "port", "database", "user", "allowed_tables",
    )}
    baseline = source_scope(SimpleNamespace(**fields))
    for change in ({"kind": "mysql"}, {"schema_name": "other_business"},
                   {"user": "another_reader"}, {"allowed_tables": ("orders",)}):
        assert source_scope(SimpleNamespace(**{**fields, **change})) != baseline
    mysql = MetadataConnector(DatabaseSettings(
        _env_file=None, host=pg_settings.host, port=pg_settings.port,
        database=pg_settings.database, user=pg_settings.user, password="synthetic-not-used",
        allowed_tables=pg_settings.allowed_tables,
    ))
    postgres_connector = PostgreSQLConnector(pg_settings)
    store = KnowledgeStore(tmp_path / "knowledge" / "items.sqlite3")
    # A real persisted MySQL-scoped draft requires no MySQL network access. Its
    # source boundary rejects retrieval even before confirmation is considered.
    old_mysql = store.create(json.dumps(draft()).encode(), mysql, limits[0])
    assert store.list(postgres_connector.knowledge_scope) == []
    with pytest.raises(KnowledgeError):
        store.get(old_mysql["id"], postgres_connector.knowledge_scope)
    current = confirmed(store, postgres_connector, limits[0])
    with pytest.raises(KnowledgeError):
        KnowledgeContext([current["id"]], mysql, limits[0], store)


def test_real_query_rechecks_knowledge_lifecycle_before_dispatch(pg_settings, limits, tmp_path):
    connector = PostgreSQLConnector(pg_settings)
    store = KnowledgeStore(tmp_path / "knowledge" / "items.sqlite3")
    item = confirmed(store, connector, limits[0])
    context = KnowledgeContext([item["id"]], connector, limits[0], store)
    observed = []

    async def recheck(connection):
        schemas = {table: await connector.describe_for_query(connection, table)
                   for table in context.tables}
        observed.append(schemas)
        context.validate(schemas)

    service = QueryService(connector, *limits, before_select=recheck)
    first = asyncio.run(service.execute(TOTAL))
    assert first["execution_status"] == "completed" and first["result"]["rows"] == [["130.00"]]
    store.revoke(item["id"], connector.knowledge_scope, "synthetic revocation")
    second = asyncio.run(service.execute(TOTAL))
    assert second["result"] is None and second["execution_status"] == "not_started"
    assert second["error"]["code"].startswith("KNOWLEDGE_")
    assert len(observed) == 2  # Both executions reached the real in-transaction schema recheck.


@pytest.mark.parametrize("phase", ["before_dispatch", "after_rows"])
def test_real_query_authorization_veto_discards_or_prevents_results(
    pg_settings, limits, monkeypatch, phase,
):
    revoked = False
    observed = []

    def authorize():
        if revoked:
            raise DatabaseError("PERMISSION_DENIED", "synthetic-revocation")

    connector = PostgreSQLConnector(pg_settings, authorization_check=authorize)
    original = postgres.read_postgres_result

    async def read_real(cursor, query_limits):
        nonlocal revoked
        result = await original(cursor, query_limits)
        observed.append(result)
        if phase == "after_rows":
            revoked = True
        return result

    async def before_select(connection):
        nonlocal revoked
        if phase == "before_dispatch":
            revoked = True

    monkeypatch.setattr(postgres, "read_postgres_result", read_real)
    report = asyncio.run(QueryService(
        connector, *limits, before_select=before_select,
    ).execute(TOTAL))
    assert report["status"] == "error" and report["decision"] == "BLOCK"
    assert report["error"]["code"] == "PERMISSION_DENIED"
    assert report["result"] is None and "result_id" not in report
    expected_status = "not_started" if phase == "before_dispatch" else "unknown"
    assert report["execution_status"] == expected_status
    assert len(observed) == (0 if phase == "before_dispatch" else 1)
    if observed:
        assert observed[0]["rows"] == [["130.00"]]


@pytest.fixture
def identity_web(pg_settings, limits, tmp_path, monkeypatch, stub_server):
    monkeypatch.chdir(tmp_path)
    identities_path = tmp_path / "identities.json"
    identities = {"version": 1, "users": [
        {"username": "alice", "display_name": "Alice", "password_hash": HASH,
         "allowed_tables": ["orders", "customers"], "model_enabled": True,
         "model_tables": ["orders"]},
        {"username": "bob", "display_name": "Bob", "password_hash": HASH,
         "allowed_tables": ["customers"], "model_enabled": True, "model_tables": ["customers"]},
    ]}
    save(identities_path, identities)
    source = {"settings": pg_settings}
    monkeypatch.setattr(web, "load_database_settings", lambda: source["settings"])
    monkeypatch.setattr(web, "load_analysis_settings", lambda: limits[0])
    monkeypatch.setattr(web, "load_query_settings", lambda: limits[1])
    monkeypatch.setattr(web, "load_settings", lambda: Settings(
        _env_file=None, api_key="synthetic-http-only-secret", model="synthetic-http-protocol",
        openai_base_url=stub_server["base_url"], max_model_calls=4, max_tool_calls=6,
        max_output_tokens=1024, run_timeout_seconds=60,
    ))
    app = web.create_app(
        store_path=tmp_path / "history.sqlite3", identity_path=identities_path,
        static_dir=tmp_path / "dist",
    )
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        yield app, client, identities_path, identities, source, stub_server


def saved_real_query(client):
    conversation = create(client)
    started = start(client, conversation, TOTAL, "query")
    assert started.status_code == 202, started.text
    run = terminal(client, started.json()["id"])
    assert run["status"] == "completed", run["error"]
    report = run["queries"][0]["report"]
    assert report["execution_status"] == "completed" and report["result"]["rows"] == [["130.00"]]
    result_path = (
        f'/api/conversations/{conversation}/runs/{run["id"]}/results/{report["result_id"]}'
    )
    assert client.get(result_path).json()["report"]["result"]["rows"] == [["130.00"]]
    return conversation, run, result_path


def saved_real_knowledge(client):
    response = client.post("/api/knowledge", json=draft(kind="sql_template", sql=TOTAL))
    assert response.status_code == 201, response.text
    item = response.json()
    target = f'/api/knowledge/{item["id"]}'
    response = client.post(target + "/confirm", json={"digest": item["digest"]})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "confirmed"
    return target


def test_web_real_metadata_uses_identity_and_distinct_model_connector_scopes(identity_web):
    app, client, _, _, _, stub = identity_web
    for username, allowed, model_table in [
        ("alice", ["customers", "orders"], "orders"), ("bob", ["customers"], "customers"),
    ]:
        login(client, username)
        response = client.get("/api/schema/tables")
        assert response.status_code == 200, response.text
        assert [row["name"] for row in response.json()["tables"]] == allowed
        response = client.get(f"/api/schema/tables/{model_table}")
        assert response.status_code == 200 and response.json()["dialect"] == "postgres"
        runtime = app.state.service.runtimes[f"{app.state.service.generation}:{username}"]
        assert type(runtime.connector) is type(runtime.model_connector) is PostgreSQLConnector
        assert list(runtime.connector._settings.allowed_tables) == allowed
        assert runtime.model_connector._settings.allowed_tables == (model_table,)
        # Only these two HTTP responses are doubles. describe_table itself is real.
        stub["responses"] = [tool_completion(("describe_table", {"table": model_table})),
                             completion("synthetic HTTP protocol: metadata received")]
        before = len(stub["requests"])
        run = terminal(client, start(client, create(client), "说明授权表结构").json()["id"])
        assert run["status"] == "completed", run["error"]
        assert len(stub["requests"]) == before + 2
        messages = stub["requests"][-1]["body"]["messages"]
        tool_messages = [message for message in messages if message["role"] == "tool"]
        assert len(tool_messages) == 1
        metadata_text = tool_messages[0]["content"]
        assert model_table in metadata_text and "postgres" in metadata_text
        assert "Customer Alpha" not in metadata_text and "SYN-O1001" not in metadata_text
        assert "synthetic-http-only-secret" not in json.dumps(messages)
    assert client.get("/api/schema/tables/orders").status_code == 400
    rejected = terminal(client, start(
        client, create(client), "SELECT id FROM orders", "query",
    ).json()["id"])["queries"][0]["report"]
    assert rejected["decision"] == "BLOCK" and rejected["execution_status"] == "not_started"
    assert rejected["result"] is None


def test_web_real_results_and_knowledge_are_isolated_between_logins(identity_web):
    _, client, _, _, _, stub = identity_web
    login(client)
    conversation, run, result_path = saved_real_query(client)
    knowledge_path = saved_real_knowledge(client)
    login(client, "bob")
    assert client.get("/api/conversations").json()["conversations"] == []
    for path in (f"/api/conversations/{conversation}", f'/api/runs/{run["id"]}', result_path):
        assert client.get(path).status_code == 404
    assert client.post(result_path + "/export", json={"format": "json"}).status_code == 404
    assert client.get(knowledge_path).status_code == 400
    assert client.get("/api/knowledge").json()["knowledge"] == []
    assert client.post(knowledge_path + "/revoke", json={"reason": "wrong user"}).status_code == 400
    login(client)
    assert client.get(result_path).status_code == 200
    assert client.get(knowledge_path).json()["state"] == "confirmed"
    assert stub["requests"] == []  # SQL mode and knowledge management do not call a model.


@pytest.mark.parametrize("change", ["identity", "kind", "schema", "account", "allowlist"])
def test_web_generation_changes_never_resurrect_pg_history_knowledge_or_result_ids(
    identity_web, change,
):
    app, client, identity_path, identities, source, stub = identity_web
    original_settings = source["settings"]
    original_identities = copy.deepcopy(identities)
    first_login = login(client)
    conversation, run, result_path = saved_real_query(client)
    knowledge_path = saved_real_knowledge(client)
    original_generation = app.state.service.generation
    if change == "identity":
        identities["users"][0]["allowed_tables"] = ["orders"]
        save(identity_path, identities)
    elif change == "kind":
        source["settings"] = DatabaseSettings(
            _env_file=None, host=original_settings.host, port=original_settings.port,
            database=original_settings.database, user=original_settings.user,
            password="synthetic-unused-other-source",
            allowed_tables=original_settings.allowed_tables,
        )
    else:
        field, value = {
            "schema": ("schema_name", "other_business"), "account": ("user", "another_reader"),
            "allowlist": ("allowed_tables", ("customers", "orders")),
        }[change]
        source["settings"] = original_settings.model_copy(update={field: value})
    assert client.get("/api/status").status_code == 401
    second_login = login(client)
    assert first_login["identity"]["authorization_version"] != (
        second_login["identity"]["authorization_version"]
    )
    assert app.state.service.generation != original_generation
    changed_generation = app.state.service.generation
    for path in (f"/api/conversations/{conversation}", f'/api/runs/{run["id"]}', result_path):
        assert client.get(path).status_code == 404
    assert client.get(knowledge_path).status_code == 400
    assert client.post(result_path + "/export", json={"format": "json"}).status_code == 404
    # Restoring byte-for-byte source/identity config must create another generation.
    source["settings"] = original_settings
    save(identity_path, original_identities)
    assert client.get("/api/status").status_code == 401
    login(client)
    assert app.state.service.generation not in {original_generation, changed_generation}
    assert client.get(f"/api/conversations/{conversation}").status_code == 404
    assert client.get(result_path).status_code == 404
    assert client.get(knowledge_path).status_code == 400
    assert stub["requests"] == []


def test_postgres_numeric_comparison_preserves_more_than_mysql_chart_precision(pg_settings, limits):
    # PostgreSQL numeric accepts this literal; the independent exact decimal has
    # 101 digits, wider than MySQL DECIMAL. No arithmetic/chart budget applies.
    value = "1" * 101 + ".0"
    query = f"SELECT {value} AS amount FROM orders WHERE id=1001"
    service = OptimizationService(PostgreSQLConnector(pg_settings), *limits)
    report = asyncio.run(service.compare(query, query))
    assert report["outcome"] == "observed_equal", report
    assert report["trials"][0]["original"]["result"]["rows"] == [[value]]


def test_web_real_postgres_never_inherits_explicit_mysql_change_grant(identity_web, monkeypatch):
    from db_agent.changes import ChangeTarget
    from db_agent.changes_db import ChangeConnector

    app, client, path, identities, _, _ = identity_web
    # The independent MySQL target is configured at the application boundary; this
    # test must reject it before ANY MySQL dispatch, even with explicit user grants.
    target = ChangeTarget(password="synthetic-never-dispatched", server_uuid="a" * 36,
                          schema_digest="b" * 64)
    monkeypatch.setattr(web, "load_change_target", lambda: target)
    identities["users"][0].update(change_targets=["local_inventory"], change_approve=True)
    save(path, identities)
    calls = []
    def forbidden(self, *args, **kwargs):
        calls.append(True)
        raise AssertionError("PostgreSQL Web must not construct a MySQL writer")
    monkeypatch.setattr(ChangeConnector, "__init__", forbidden)
    client.get("/api/auth/session")
    login(client)
    saved_real_query(client)  # Actual PostgreSQL query + independent 130.00 fixture oracle.
    assert client.get("/api/changes").json() == {
        "targets": [], "changes": [], "can_approve": False,
    }
    identifier = "a" * 32
    for endpoint, payload in [
        ("preview", {"target": "local_inventory", "item_id": 1, "quantity": 0,
                     "request_id": "x" * 32}),
        (identifier + "/approve", {"digest": "b" * 64}),
        (identifier + "/execute", {}), (identifier + "/reconcile", {}),
        (identifier + "/recover", {"request_id": "r" * 32}),
    ]:
        response = client.post("/api/changes/" + endpoint, json=payload)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CHANGE_PERMISSION_DENIED"
    assert client.get("/api/changes/" + identifier).status_code == 400
    assert not calls and not app.state.service.changes.pending
