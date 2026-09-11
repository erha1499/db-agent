"""Offline trust/state tests with a clearly identified in-memory database double."""

import json
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from db_agent import web
from db_agent.changes import ChangeTarget, digest
from db_agent.config import ConfigurationError, DatabaseSettings
from db_agent.web_identity import password_hash

PASSWORD = "synthetic-changes-user-password"
HASH = password_hash(PASSWORD)
HEADERS = {"X-DB-Agent-Client": "web"}


def login(client, username="alice"):
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200
    client.headers["X-DB-Agent-Session"] = response.json()["session_id"]


def identities(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def configured_app(tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "users.json"
    data = {
        "version": 1,
        "users": [
            {
                "username": "alice",
                "display_name": "Alice",
                "password_hash": HASH,
                "allowed_tables": [],
                "change_targets": ["local_inventory"],
                "change_approve": True,
            },
            {"username": "bob", "display_name": "Bob", "password_hash": HASH, "allowed_tables": []},
        ],
    }
    identities(path, data)
    monkeypatch.setattr(web, "load_change_target", lambda: target)
    monkeypatch.setattr(
        web,
        "load_database_settings",
        lambda: DatabaseSettings(
            _env_file=None,
            password="synthetic-unused-read-secret",
            allowed_tables=[],
        ),
    )

    def unconfigured():
        raise ConfigurationError("no model for changes")

    monkeypatch.setattr(web, "load_settings", unconfigured)
    return (
        web.create_app(
            store_path=tmp_path / "history.sqlite3",
            identity_path=path,
            static_dir=tmp_path / "dist",
        ),
        path,
        data,
    )


class DatabaseDouble:
    def __init__(self):
        self.current = {"quantity": 10, "version": 1}
        self.executions = 0

    def __call__(self, guard):
        self.guard = guard
        return self

    async def read(self, item_id):
        self.guard()
        return dict(self.current)

    async def execute(self, item):
        self.guard()
        self.executions += 1
        if self.current != item["plan"]["before"]:
            return {"outcome": "rejected", "code": "CHANGE_DRIFT", "current": self.current}
        self.current = dict(item["plan"]["after"])
        return {
            "outcome": "committed",
            "code": "DOUBLE_ONLY",
            "current": self.current,
            "receipt_verified": True,
        }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    target = ChangeTarget(password="synthetic-secret", server_uuid="a" * 36, schema_digest="b" * 64)
    app, path, data = configured_app(tmp_path, monkeypatch, target)
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        double = DatabaseDouble()
        monkeypatch.setattr(app.state.service.changes, "connector", double)
        login(client)
        yield app, client, path, data, double


def preview(client, quantity=12, request_id=None):
    from uuid import uuid4

    response = client.post(
        "/api/changes/preview",
        json={
            "target": "local_inventory",
            "item_id": 1,
            "quantity": quantity,
            "request_id": request_id or uuid4().hex,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, item):
    response = client.post(
        f"/api/changes/{item['plan']['id']}/approve", json={"digest": item["digest"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def execute(client, item):
    response = client.post(f"/api/changes/{item['plan']['id']}/execute", json={})
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_approval_is_bound_and_duplicate_execute_never_dispatches(setup):
    _, client, _, _, double = setup
    item = preview(client)
    assert execute(client, item)["status"] == "preview"
    assert double.executions == 0
    assert (
        client.post(
            f"/api/changes/{item['plan']['id']}/approve", json={"digest": "0" * 64}
        ).status_code
        == 422
    )
    approve(client, item)
    assert execute(client, item)["status"] == "committed"
    assert execute(client, item)["status"] == "committed"
    assert double.executions == 1
    assert double.current == {"quantity": 12, "version": 2}


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"role": "admin"},
        {"sql": "UPDATE inventory SET quantity=0"},
        {"target": "production"},
        {"quantity": -1},
        {"quantity": 1000001},
        {"quantity": True},
        {"quantity": "1"},
        {"item_id": 0},
        {"item_id": 1000000001},
    ],
)
def test_no_arbitrary_sql_targets_roles_or_unbounded_inputs(setup, extra):
    _, client, _, _, double = setup
    body = {"target": "local_inventory", "item_id": 1, "quantity": 12, "request_id": "x" * 32}
    body.update(extra)
    assert client.post("/api/changes/preview", json=body).status_code == 422
    assert double.executions == 0


def test_request_reuse_and_recovery_require_new_approval(setup):
    _, client, _, _, double = setup
    item = preview(client, request_id="x" * 32)
    assert preview(client, request_id="x" * 32) == item
    response = client.post(
        "/api/changes/preview",
        json={"target": "local_inventory", "item_id": 1, "quantity": 13, "request_id": "x" * 32},
    )
    assert response.status_code == 422
    approve(client, item)
    execute(client, item)
    recovery = client.post(
        f"/api/changes/{item['plan']['id']}/recover", json={"request_id": "r" * 32}
    ).json()
    assert recovery["status"] == "preview"
    assert recovery["plan"]["recovery_of"] == item["plan"]["id"]
    assert execute(client, recovery)["status"] == "preview"
    approve(client, recovery)
    assert execute(client, recovery)["status"] == "committed"
    assert double.current == {"quantity": 10, "version": 3}


@pytest.mark.parametrize("operation", ["get", "approve", "execute", "reconcile", "recover"])
def test_other_identity_cannot_read_or_act_on_change(setup, operation):
    _, client, _, _, double = setup
    item = preview(client)
    login(client, "bob")
    path = f"/api/changes/{item['plan']['id']}"
    response = (
        client.get(path)
        if operation == "get"
        else client.post(
            path + "/" + operation,
            json={"digest": item["digest"]}
            if operation == "approve"
            else {"request_id": "r" * 32}
            if operation == "recover"
            else {},
        )
    )
    assert response.status_code == 400
    assert client.get("/api/changes").json()["targets"] == []
    assert double.executions == 0


def test_permission_change_invalidates_old_generation_and_restore_does_not_revive(setup):
    _, client, path, data, double = setup
    item = preview(client)
    approve(client, item)
    data["users"][0]["change_approve"] = False
    identities(path, data)
    assert client.post(f"/api/changes/{item['plan']['id']}/execute", json={}).status_code == 401
    login(client)
    assert client.get(f"/api/changes/{item['plan']['id']}").status_code == 400
    data["users"][0]["change_approve"] = True
    identities(path, data)
    client.get("/api/auth/session")
    login(client)
    assert client.get("/api/changes").json()["changes"] == []
    assert double.executions == 0


def test_preview_without_approval_permission_cannot_self_promote(setup):
    _, client, path, data, double = setup
    data["users"][0]["change_approve"] = False
    identities(path, data)
    client.get("/api/auth/session")
    login(client)
    item = preview(client)
    for action, body in [("approve", {"digest": item["digest"]}), ("execute", {})]:
        assert (
            client.post(f"/api/changes/{item['plan']['id']}/{action}", json=body).status_code == 400
        )
    assert double.executions == 0


@pytest.mark.parametrize("stage", ["approved", "executing"])
def test_approval_and_claim_persistence_failure_prevents_database_dispatch(
    setup, monkeypatch, stage
):
    app, client, _, _, double = setup
    item = preview(client)
    if stage == "executing":
        approve(client, item)
    original = app.state.service.changes.store.transition

    def failed(value, expected, status, evidence=None):
        if status == stage:
            raise sqlite3.OperationalError("synthetic disk failure")
        return original(value, expected, status, evidence)

    monkeypatch.setattr(app.state.service.changes.store, "transition", failed)
    response = client.post(
        f"/api/changes/{item['plan']['id']}/" + ("approve" if stage == "approved" else "execute"),
        json={"digest": item["digest"]} if stage == "approved" else {},
    )
    assert response.status_code == 503
    assert double.executions == 0


def test_final_storage_failure_retains_claim_without_redispatch(setup, monkeypatch):
    app, client, _, _, double = setup
    item = approve(client, preview(client))
    original = app.state.service.changes.store.transition

    def failed(value, expected, status, evidence=None):
        if status == "committed":
            raise sqlite3.OperationalError("synthetic disk failure")
        return original(value, expected, status, evidence)

    monkeypatch.setattr(app.state.service.changes.store, "transition", failed)
    assert client.post(f"/api/changes/{item['plan']['id']}/execute", json={}).status_code == 503
    assert execute(client, item)["status"] == "executing"
    assert double.executions == 1


def test_expiry_and_tampered_plan_fail_closed(setup):
    app, client, _, _, double = setup
    item = approve(client, preview(client))
    p = item["plan"]
    p["expires_at"] = 0.0
    with app.state.service.store.connection() as db:
        db.execute(
            "UPDATE changes SET plan=?,digest=? WHERE id=?", (json.dumps(p), digest(p), p["id"])
        )
    assert client.post(f"/api/changes/{p['id']}/execute", json={}).status_code == 400
    with app.state.service.store.connection() as db:
        db.execute("UPDATE changes SET digest=? WHERE id=?", ("0" * 64, p["id"]))
    assert client.get(f"/api/changes/{p['id']}").status_code == 422
    assert double.executions == 0


def test_sqlite_transaction_commit_failure_does_not_persist_claim(setup, monkeypatch):
    app, client, _, _, double = setup
    item = approve(client, preview(client))
    original = app.state.service.store.connection

    @contextmanager
    def failed_commit():
        with original() as db:
            yield db
            raise sqlite3.OperationalError("failure before sqlite commit")

    # Transition is isolated to demonstrate actual rollback of its CAS transaction.
    with monkeypatch.context() as patch:
        patch.setattr(app.state.service.store, "connection", failed_commit)
        with pytest.raises(sqlite3.Error):
            app.state.service.changes.store.transition(item, "approved", "executing")
    assert client.get(f"/api/changes/{item['plan']['id']}").json()["status"] == "approved"
    assert double.executions == 0


def test_confirmed_commit_never_becomes_not_committed_after_failed_reconciliation(
    setup, monkeypatch
):
    app, client, _, _, double = setup
    item = approve(client, preview(client))
    assert execute(client, item)["status"] == "committed"

    async def unavailable(value):
        return {"outcome": "unknown", "code": "RECONCILIATION_UNAVAILABLE"}

    monkeypatch.setattr(double, "reconcile", unavailable, raising=False)
    result = client.post(f"/api/changes/{item['plan']['id']}/reconcile", json={}).json()
    assert result["status"] == "committed" and result["evidence"]["outcome"] == "unknown"
    assert (
        client.post(
            f"/api/changes/{item['plan']['id']}/recover", json={"request_id": "r" * 32}
        ).status_code
        == 400
    )

    async def absent(value):
        return {"outcome": "not_committed", "code": "ABSENT_AFTER_BARRIER"}

    monkeypatch.setattr(double, "reconcile", absent)
    assert (
        client.post(f"/api/changes/{item['plan']['id']}/reconcile", json={}).json()["status"]
        == "committed"
    )


def test_forged_status_without_durable_approval_cannot_reach_database(setup):
    app, client, _, _, double = setup
    item = preview(client)
    with app.state.service.store.connection() as db:
        db.execute("UPDATE changes SET status='approved' WHERE id=?", (item["plan"]["id"],))
    assert client.post(f"/api/changes/{item['plan']['id']}/execute", json={}).status_code == 400
    assert double.executions == 0


def test_recovery_rechecks_original_evidence_at_actual_execution(setup):
    app, client, _, _, double = setup
    item = approve(client, preview(client))
    execute(client, item)
    recovery = client.post(
        f"/api/changes/{item['plan']['id']}/recover", json={"request_id": "r" * 32}
    ).json()
    approve(client, recovery)
    with app.state.service.store.connection() as db:
        db.execute(
            "UPDATE changes SET evidence=? WHERE id=?",
            (json.dumps({"outcome": "unknown"}), item["plan"]["id"]),
        )
    assert client.post(f"/api/changes/{recovery['plan']['id']}/execute", json={}).status_code == 400
    assert double.executions == 1


def test_second_source_never_inherits_mysql_changes(setup, monkeypatch):
    app, client, _, _, double = setup
    database = app.state.service.db_settings.model_copy(update={"kind": "postgresql"})
    monkeypatch.setattr(web, "load_database_settings", lambda: database)
    client.get("/api/auth/session")
    login(client)
    assert client.get("/api/changes").json()["targets"] == []
    response = client.post(
        "/api/changes/preview",
        json={
            "target": "local_inventory",
            "item_id": 1,
            "quantity": 1,
            "request_id": "x" * 32,
        },
    )
    assert response.status_code == 400
    assert double.executions == 0
