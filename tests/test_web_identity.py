"""Explicit service doubles test trust boundaries; real MySQL is a separate probe."""

import asyncio
import json
import time
from collections import deque
from dataclasses import replace
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from test_agent import stub_server as stub_server
from test_agent import tool_completion
from test_knowledge import document
from test_query_connector import PLAN, QueryConnection
from test_web import create, result, saved_delivery, start, terminal

from db_agent import web
from db_agent.config import AnalysisSettings, ConfigurationError, DatabaseSettings, Settings
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.knowledge import KnowledgeContext
from db_agent.web_identity import COOKIE, password_hash, read_identities

HEADERS = {"X-DB-Agent-Client": "web"}
PASSWORD = "synthetic-local-password"
HASH = password_hash(PASSWORD)


def login(client, username="alice"):
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-DB-Agent-Session"] = response.json()["session_id"]
    return response.json()


def save(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "identities.json"
    data = {"version": 1, "users": [
        {"username": "alice", "display_name": "Alice", "password_hash": HASH,
         "allowed_tables": ["orders", "customers"], "model_enabled": True,
         "model_tables": ["orders"]},
        {"username": "bob", "display_name": "Bob", "password_hash": HASH,
         "allowed_tables": ["customers"], "model_enabled": False, "model_tables": []},
    ]}
    save(path, data)
    monkeypatch.setattr(web, "load_database_settings", lambda: DatabaseSettings(
        _env_file=None, password="synthetic-only-db-secret", allowed_tables=["orders", "customers"],
    ))
    monkeypatch.setattr(web, "load_settings", lambda: Settings(
        _env_file=None, api_key="synthetic-only-model-secret", model="synthetic",
        openai_base_url="http://127.0.0.1:1/v1",
    ))
    app = web.create_app(store_path=tmp_path / "history.sqlite3", identity_path=path,
                         static_dir=tmp_path / "dist")
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        yield app, client, path, data


def test_auth_required_for_all_resources_and_no_self_reported_identity(setup):
    _, client, _, _ = setup
    assert client.get("/api/auth/session").json()["authenticated"] is False
    for method, path, body in [
        ("get", "/api/status", None), ("get", "/api/conversations", None),
        ("post", "/api/conversations", {}), ("get", "/api/schema/tables", None),
        ("get", "/api/schema/tables/orders", None), ("get", "/api/runs/fake", None),
        ("post", "/api/runs/fake/cancel", {}),
        ("get", "/api/conversations/a/runs/b/results/c", None),
        ("post", "/api/conversations/a/runs/b/results/c/export", {"format": "json"}),
    ]:
        response = client.request(method, path, **({"json": body} if body is not None else {}),
                                  headers={"X-User-ID": "alice", "Authorization": "Bearer alice"})
        assert response.status_code == 401, (method, path, response.text)
        assert response.headers["Cache-Control"] == "no-store"
    response = client.post("/api/auth/login", json={
        "username": "alice", "password": PASSWORD, "allowed_tables": ["secret"],
    })
    assert response.status_code == 422


def test_cookie_rotation_failure_logout_expiry_and_private_status(setup):
    app, client, _, _ = setup
    for name in ["alice", "nobody"]:
        response = client.post("/api/auth/login", json={"username": name, "password": "wrong"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "LOGIN_FAILED"
    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api" in cookie
    old = client.cookies.get(COOKIE)
    login(client)
    assert old != client.cookies.get(COOKIE)
    assert app.state.service.sessions.get(old, app.state.service.generation) is None
    payload = client.get("/api/status").text
    assert "secret" not in payload and HASH not in payload and PASSWORD not in payload
    session = next(iter(app.state.service.sessions.values.values()))
    app.state.service.sessions.values[session.key] = replace(session, expires=time.monotonic() - 1)
    assert client.get("/api/status").status_code == 401
    login(client)
    assert client.post("/api/auth/logout", json={}).status_code == 200
    assert not client.cookies.get(COOKIE)
    assert client.get("/api/status").status_code == 401


def test_stale_page_session_header_cannot_write_under_new_cookie(setup):
    _, client, _, _ = setup
    alice = login(client)
    login(client, "bob")
    response = client.post("/api/conversations", json={}, headers={
        "X-DB-Agent-Session": alice["session_id"],
    })
    assert response.status_code == 401
    assert client.get("/api/conversations").json()["conversations"] == []
    assert client.get("/api/status", headers={"X-DB-Agent-Session": ""}).status_code == 401


def test_policy_change_between_middleware_and_handler_prevents_history_write(setup, monkeypatch):
    app, client, path, data = setup
    login(client)
    entered, resume = Event(), Event()
    route = next(route for route in app.routes
                 if getattr(route, "path", None) == "/api/conversations"
                 and "POST" in route.methods)
    original = route.dependant.call

    async def gated(**kwargs):
        entered.set()
        await asyncio.to_thread(resume.wait, 2)
        return await original(**kwargs)

    monkeypatch.setattr(route.dependant, "call", gated)
    responses = []
    thread = Thread(target=lambda: responses.append(client.post("/api/conversations", json={})))
    thread.start()
    try:
        assert entered.wait(2)
        data["users"][0]["allowed_tables"] = ["orders"]
        save(path, data)
        assert client.get("/api/auth/session").json()["authenticated"] is False
    finally:
        resume.set()
        thread.join(timeout=3)
    assert responses[0].status_code == 401
    with app.state.service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert not app.state.service.runtimes


def test_model_contexts_are_identity_specific_and_revoke_stops_next_http(
    setup, monkeypatch, stub_server,
):
    _, client, path, data = setup
    monkeypatch.setattr(web, "load_settings", lambda: Settings(
        _env_file=None, api_key="synthetic-only-model-secret", model="synthetic",
        openai_base_url=stub_server["base_url"],
    ))
    data["users"][1].update(model_enabled=True, model_tables=["customers"])
    save(path, data)
    for username, allowed, excluded in [("alice", "orders", "customers"),
                                        ("bob", "customers", "orders")]:
        login(client, username)
        run = terminal(client, start(client, create(client), "说明可用表").json()["id"])
        assert run["status"] == "completed"
        messages = json.dumps(stub_server["requests"][-1]["body"]["messages"])
        assert allowed in messages and excluded not in messages
        assert "secret" not in messages and '"rows"' not in messages
    login(client)
    stub_server["response"] = tool_completion(("describe_table", {"table": "orders"}))

    async def describe(connector, table):
        data["users"][0]["enabled"] = False
        save(path, data)
        return {"table": table, "columns": [], "indexes": [], "foreign_keys": []}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    start(client, create(client), "查看结构再说明")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if client.get("/api/auth/session").json()["authenticated"] is False:
            break
        time.sleep(0.02)
    assert len(stub_server["requests"]) == 3


def test_session_status_user_removed_after_middleware_returns_logged_out(setup, monkeypatch):
    app, client, path, data = setup
    login(client)
    route = next(route for route in app.routes
                 if getattr(route, "path", None) == "/api/auth/session")
    original = route.dependant.call

    async def removed(**kwargs):
        data["users"] = data["users"][1:]
        save(path, data)
        app.state.service.refresh()
        return await original(**kwargs)

    monkeypatch.setattr(route.dependant, "call", removed)
    response = client.get("/api/auth/session")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_knowledge_confirmation_uses_the_model_visible_foreign_key_view(setup, monkeypatch):
    _, client, _, _ = setup
    login(client)

    async def describe(connector, table):
        connector._authorize()
        return {"table": table, "columns": [{"name": "id", "type": "bigint"}],
                "indexes": [], "foreign_keys": [
                    {"name": "fk_customer", "columns": ["customer_id"],
                     "referenced_table": "customers", "referenced_columns": ["id"]},
                ] if "customers" in connector.authorized_table_candidates else [],
                "foreign_keys_scope": list(connector.authorized_table_candidates)}

    async def agent(prompt, settings, connector, *args, **kwargs):
        context = KnowledgeContext([item["id"]], connector, AnalysisSettings())
        context.validate({"orders": await connector.describe_table("orders")})
        return result()

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    item = client.post("/api/knowledge", json=document()).json()
    response = client.post(f'/api/knowledge/{item["id"]}/confirm', json={"digest": item["digest"]})
    assert response.status_code == 200
    monkeypatch.setattr(web, "run_agent_observed", agent)
    run = terminal(client, start(client, create(client), "核对授权内知识").json()["id"])
    assert run["status"] == "completed"


@pytest.mark.parametrize("phase", ["before", "after"])
def test_explain_guard_vetoes_dispatch_or_discards_received_plan(phase):
    revoked = phase == "before"

    def authorize():
        if revoked:
            raise DatabaseError("PERMISSION_DENIED", "revoked")

    async def after_dispatch(cursor, query):
        nonlocal revoked
        revoked = True

    connector = MetadataConnector(DatabaseSettings(_env_file=None, password="synthetic",
                                  allowed_tables=["orders"]), authorization_check=authorize)
    connection = QueryConnection(before_query=after_dispatch)
    connection.responses = deque([[{"EXPLAIN": json.dumps(PLAN)}]])

    async def probe():
        with pytest.raises(DatabaseError, match="revoked"):
            await connector._read_plan(connection, "SELECT id FROM orders", AnalysisSettings())

    asyncio.run(probe())
    assert len(connection.queries) == (0 if phase == "before" else 1)


def test_knowledge_same_table_users_are_isolated_and_restore_does_not_resurrect(
    setup, monkeypatch,
):
    _, client, path, data = setup
    data["users"][1].update(allowed_tables=["orders", "customers"], model_enabled=True,
                            model_tables=["orders"])
    save(path, data)

    async def describe(connector, table):
        connector._authorize()
        connector.validate_table(table)
        return {"database": "db_agent", "table": table, "columns": [
            {"name": "id", "type": "bigint", "nullable": "NO"}], "indexes": [],
                "foreign_keys": [], "foreign_keys_scope": {"database": "db_agent"}}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    login(client)
    assert client.get("/api/knowledge").json() == {"knowledge": []}
    draft = client.post("/api/knowledge", json=document()).json()
    identifier = draft["id"]
    target = f"/api/knowledge/{identifier}"
    assert "scope" not in draft
    assert client.post(target + "/confirm", json={"digest": "0" * 64}).status_code == 400
    assert client.post(target + "/confirm", json={"digest": draft["digest"]}
                       ).json()["state"] == "confirmed"
    login(client, "bob")
    assert client.get("/api/knowledge").json() == {"knowledge": []}
    assert client.get(target).status_code == 400
    assert client.post(target + "/revoke", json={"reason": "steal"}).status_code == 400
    assert client.post(target + "/confirm", json={"digest": draft["digest"]}
                       ).status_code == 400
    conversation = create(client)
    # Actual agent preparation rejects another user's knowledge before HTTP.
    started = start(client, conversation, f"[[knowledge:{identifier}]] 查询")
    run = terminal(client, started.json()["id"])
    assert run["status"] == "failed" and run["error"]["code"] == "KNOWLEDGE_UNAVAILABLE"
    assert not run["queries"]
    login(client)
    assert client.get(target).json()["state"] == "confirmed"
    original = json.loads(json.dumps(data))
    data["users"][0]["model_tables"] = []
    save(path, data)
    login(client)
    assert client.get(target).status_code == 400
    save(path, original)
    login(client)
    assert client.get(target).status_code == 400


def test_knowledge_outside_model_tables_never_reaches_model(setup, monkeypatch):
    _, client, _, _ = setup
    login(client)

    async def describe(connector, table):
        connector.validate_table(table)
        return {"table": table, "columns": [], "indexes": [], "foreign_keys": []}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    draft = client.post("/api/knowledge", json=document(tables=["customers"])).json()
    target = f'/api/knowledge/{draft["id"]}'
    assert client.post(target + "/confirm", json={"digest": draft["digest"]}).status_code == 200
    run = terminal(client, start(client, create(client),
                                 f'[[knowledge:{draft["id"]}]] 查询').json()["id"])
    assert run["status"] == "failed" and run["error"]["code"] == "PERMISSION_DENIED"
    assert not run["queries"]


def test_identity_history_results_export_and_cancel_isolation(setup, monkeypatch):
    _, client, _, _ = setup
    login(client)
    conversation, run, path = saved_delivery(client, monkeypatch)
    login(client, "bob")
    assert client.get("/api/conversations").json()["conversations"] == []
    assert client.get("/api/status").json()["active_run_id"] is None
    for method, target, body in [
        ("get", f"/api/conversations/{conversation}", None),
        ("patch", f"/api/conversations/{conversation}", {"title": "stolen"}),
        ("delete", f"/api/conversations/{conversation}", None),
        ("post", f"/api/conversations/{conversation}/runs", {
            "prompt": "steal", "request_id": "x" * 32, "mode": "query",
        }),
        ("get", f'/api/runs/{run["id"]}', None),
        ("post", f'/api/runs/{run["id"]}/cancel', {}),
        ("get", path, None),
        ("post", path + "/analysis", {"dimension": 0, "measure": 1}),
        ("post", path + "/export", {"format": "json"}),
    ]:
        response = client.request(method, target, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, response.text
        assert "9007199254740993.01" not in response.text
    login(client)
    assert client.get(path).status_code == 200


def test_tables_query_and_model_range_are_server_owned(setup, monkeypatch):
    _, client, _, _ = setup
    seen = []

    async def agent(prompt, settings, connector, *args, **kwargs):
        seen.append((prompt, connector.authorized_table_candidates, kwargs["previous_requests"]))
        checked = connector.check_sql("SELECT id FROM customers", AnalysisSettings())
        assert checked.decision == "BLOCK"
        return result()

    monkeypatch.setattr(web, "run_agent_observed", agent)
    login(client)
    conversation = create(client)
    assert terminal(client, start(client, conversation).json()["id"])["status"] == "completed"
    assert seen == [("查二月成交额", ("orders",), [])]
    login(client, "bob")
    other = create(client)
    assert start(client, other).status_code == 403
    assert client.get("/api/schema/tables/orders").status_code == 400
    run = terminal(client, start(client, other, "SELECT id FROM orders", "query").json()["id"])
    report = run["queries"][0]["report"]
    assert report["decision"] == "BLOCK" and report["execution_status"] == "not_started"
    assert report["result"] is None and len(seen) == 1
    assert start(client, other, mode="query").status_code == 202


def test_policy_change_rejects_old_cookie_and_never_resurrects_old_history(setup):
    app, client, path, data = setup
    first = login(client)
    conversation = create(client)
    original = json.loads(json.dumps(data))
    data["users"][0]["allowed_tables"] = ["orders"]
    save(path, data)
    assert client.get("/api/status").status_code == 401
    second = login(client)
    assert first["identity"]["authorization_version"] != second["identity"]["authorization_version"]
    assert client.get(f"/api/conversations/{conversation}").status_code == 404
    save(path, original)
    assert client.get("/api/status").status_code == 401
    third = login(client)
    assert third["identity"]["authorization_version"] != first["identity"]["authorization_version"]
    assert client.get(f"/api/conversations/{conversation}").status_code == 404
    assert len(app.state.service.store.list(first["identity"]["authorization_version"])) == 0


@pytest.mark.parametrize("change", ["disable", "invalid", "logout", "expire"])
def test_inflight_revocation_cleans_task_without_another_request(setup, monkeypatch, change):
    app, client, path, data = setup
    entered, cleaned = Event(), Event()

    async def agent(*args, **kwargs):
        entered.set()
        try:
            await asyncio.sleep(20)
        finally:
            cleaned.set()

    monkeypatch.setattr(web, "run_agent_observed", agent)
    login(client)
    conversation = create(client)
    run = start(client, conversation).json()
    assert entered.wait(1)
    runtime = next(iter(app.state.service.runtimes.values()))
    if change == "disable":
        data["users"][0]["enabled"] = False
        save(path, data)
    elif change == "invalid":
        path.write_text("malformed")
    elif change == "logout":
        client.post("/api/auth/logout", json={})
    else:
        session = next(iter(app.state.service.sessions.values.values()))
        app.state.service.sessions.values[session.key] = replace(session, expires=0)
    assert cleaned.wait(2)
    for _ in range(100):
        if not runtime.tasks:
            break
        time.sleep(0.01)
    assert not runtime.tasks
    assert app.state.service.store.get_run(run["id"], runtime.scope)["status"] == "cancelled"


def test_revoke_during_metadata_response_discards_data(setup, monkeypatch):
    _, client, path, data = setup

    async def tables(connector):
        data["users"][0]["enabled"] = False
        save(path, data)
        return {"tables": [{"name": "secret-after-revoke"}]}

    monkeypatch.setattr(MetadataConnector, "list_tables", tables)
    login(client)
    response = client.get("/api/schema/tables")
    assert response.status_code == 401
    assert "secret-after-revoke" not in response.text


def test_connector_checks_authorization_before_any_db_connection():
    def deny():
        raise DatabaseError("PERMISSION_DENIED", "revoked")

    connector = MetadataConnector(DatabaseSettings(_env_file=None, password="synthetic",
                                  allowed_tables=["orders"]), authorization_check=deny)

    async def probe():
        with pytest.raises(DatabaseError, match="revoked"):
            await connector.list_tables()
        with pytest.raises(DatabaseError, match="revoked"):
            await connector.execute_checked("SELECT id FROM orders", AnalysisSettings(), None)
    asyncio.run(probe())


@pytest.mark.parametrize("case", ["broader", "model_broader", "disabled_model", "duplicate",
                                   "unknown_field", "public", "symlink"])
def test_invalid_identity_file_fails_closed(setup, case):
    _, _, path, data = setup
    if case == "broader":
        data["users"][0]["allowed_tables"].append("secret")
    elif case == "model_broader":
        data["users"][0]["model_tables"].append("secret")
    elif case == "disabled_model":
        data["users"][0]["model_enabled"] = False
    elif case == "duplicate":
        data["users"].append(data["users"][0])
    elif case == "unknown_field":
        data["users"][0]["approved"] = True
    save(path, data)
    if case == "public":
        path.chmod(0o644)
    elif case == "symlink":
        target = path.with_suffix(".other")
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ConfigurationError):
        read_identities(path, ("orders", "customers"))


def test_login_rate_limit_is_bounded_and_not_a_user_existence_oracle(setup):
    _, client, _, _ = setup
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "nobody", "password": "x"}
                           ).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}
                       ).status_code == 429


def test_model_denial_precedes_missing_provider_configuration(setup, monkeypatch):
    _, client, _, _ = setup

    def missing():
        raise ConfigurationError("not configured")

    monkeypatch.setattr(web, "load_settings", missing)
    login(client, "bob")
    conversation = create(client)
    assert start(client, conversation).status_code == 403
    assert start(client, conversation, "SELECT id FROM orders", mode="query").status_code == 202
