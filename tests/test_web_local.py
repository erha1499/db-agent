"""Default local workspace behavior; isolated service doubles, no live credentials."""

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_web import HEADERS, create
from test_web import configuration as configuration

from db_agent import web
from db_agent.config import ConfigurationError, DatabaseSettings
from db_agent.web_identity import COOKIE, password_hash


def connect(client):
    response = client.get("/api/auth/session")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["authenticated"] is True
    assert value["access_mode"] == "local"
    client.headers["X-DB-Agent-Session"] = value["session_id"]
    return value


def application(tmp_path, **kwargs):
    return web.create_app(store_path=tmp_path / "history.sqlite3", **kwargs)


def test_default_starts_without_users_and_ignores_old_optional_configuration(tmp_path):
    # Old identities and a broken, disabled change target are not startup requirements.
    for relative in ("outputs/web/identities.json", "outputs/changes/target.json"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invalid old optional configuration")
    with TestClient(application(tmp_path), base_url="http://127.0.0.1:8000",
                    headers=HEADERS) as client:
        identity = connect(client)["identity"]
        assert identity["allowed_tables"] == identity["model_tables"] == ["orders"]
        assert identity["model_enabled"] is True
        assert "password" not in json.dumps(identity)
        assert identity["change_targets"] == [] and not identity["change_approve"]
        assert client.get("/api/status").json()["changes_enabled"] is False
        assert client.get("/api/changes").json() == {
            "targets": [], "changes": [], "can_approve": False,
        }
        denied = client.post("/api/changes/preview", json={
            "target": "local_inventory", "item_id": 1, "quantity": 12,
            "request_id": "local-change-probe",
        })
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "CHANGE_PERMISSION_DENIED"
        assert client.post("/api/auth/login", json={
            "username": "local", "password": "anything",
        }).status_code == 404
        assert create(client)


def test_local_sql_workspace_does_not_require_model(tmp_path, monkeypatch):
    def missing():
        raise ConfigurationError("model not configured")

    monkeypatch.setattr(web, "load_settings", missing)
    with TestClient(application(tmp_path), base_url="http://127.0.0.1:8000",
                    headers=HEADERS) as client:
        assert connect(client)["identity"]["model_enabled"] is False
        status = client.get("/api/status").json()
        assert status["database_configured"] and not status["model_configured"]
        assert create(client)


@pytest.mark.parametrize("headers", [
    {"X-DB-Agent-Client": "wrong"},
    {"Origin": "https://example.org"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "example.org"},
])
def test_local_bootstrap_preserves_browser_boundary(tmp_path, headers):
    with TestClient(application(tmp_path), base_url="http://127.0.0.1:8000",
                    headers=HEADERS) as client:
        response = client.get("/api/auth/session", headers=headers)
        assert response.status_code == 403
        assert not client.cookies.get(COOKIE)


def test_local_session_binding_expiry_and_reconnection(tmp_path):
    app = application(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        assert client.get("/api/conversations").status_code == 401
        session = connect(client)
        first = client.cookies.get(COOKIE)
        assert connect(client)["session_id"] == session["session_id"]
        assert client.get("/api/conversations", headers={
            "X-DB-Agent-Session": "forged", "X-User-ID": "local",
        }).status_code == 401
        stored = app.state.service.sessions.values[session["session_id"]]
        app.state.service.sessions.values[stored.key] = replace(
            stored, expires=time.monotonic() - 1,
        )
        assert client.get("/api/conversations").status_code == 401
        assert connect(client)["session_id"] != session["session_id"]
        assert client.cookies.get(COOKIE) != first
        assert create(client)


def test_local_history_persists_but_config_change_does_not_restore_old_scope(tmp_path, monkeypatch):
    app = application(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        connect(client)
        old = create(client)
    with TestClient(application(tmp_path), base_url="http://127.0.0.1:8000",
                    headers=HEADERS) as client:
        connect(client)
        assert client.get(f"/api/conversations/{old}").status_code == 200
        original = web.load_database_settings
        monkeypatch.setattr(web, "load_database_settings", lambda: DatabaseSettings(
            _env_file=None, password="synthetic-db-secret", allowed_tables=[],
        ))
        assert client.get(f"/api/conversations/{old}").status_code == 401
        assert connect(client)["identity"]["allowed_tables"] == []
        assert client.get(f"/api/conversations/{old}").status_code == 404
        monkeypatch.setattr(web, "load_database_settings", original)
        connect(client)
        assert client.get(f"/api/conversations/{old}").status_code == 404


def test_local_workspace_does_not_inherit_password_users_history(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"version": 1, "users": [{
        "username": "local", "display_name": "Old user", "allowed_tables": ["orders"],
        "password_hash": password_hash("synthetic-old-password"),
    }]}))
    path.chmod(0o600)
    with TestClient(application(tmp_path, identity_path=path),
                    base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        assert client.get("/api/auth/session").json()["access_mode"] == "password"
        session = client.post("/api/auth/login", json={
            "username": "local", "password": "synthetic-old-password",
        }).json()
        client.headers["X-DB-Agent-Session"] = session["session_id"]
        old = create(client)
    with TestClient(application(tmp_path), base_url="http://127.0.0.1:8000",
                    headers=HEADERS) as client:
        connect(client)
        assert client.get(f"/api/conversations/{old}").status_code == 404
        assert client.get("/api/conversations").json()["conversations"] == []
