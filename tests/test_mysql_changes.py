"""Opt-in real isolated MySQL + trusted Web identities. Fault injections are labeled."""

import asyncio
import os
import sqlite3
from pathlib import Path
from threading import Event, Thread

import aiomysql
import pymysql
import pytest
from fastapi.testclient import TestClient
from test_changes import HEADERS, approve, configured_app, execute, identities, login, preview

from db_agent.changes import ChangeStore, load_change_target
from db_agent.changes_db import ChangeConnector

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_CHANGES_INTEGRATION") != "1",
    reason="explicit independent synthetic MySQL write opt-in required",
)


@pytest.fixture
def real(tmp_path, monkeypatch):
    target = load_change_target(Path("outputs/changes/target.json").resolve())
    assert target is not None, "create the independent changes target first"
    app, path, data = configured_app(tmp_path, monkeypatch, target)
    connection = pymysql.connect(
        host=target.host,
        port=target.port,
        user=target.user,
        password=target.password.get_secret_value(),
        database=target.database,
        autocommit=True,
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        login(client)
        yield app, client, connection, path, data
    connection.close()


def row(db):
    with db.cursor() as cursor:
        cursor.execute("SELECT quantity,version FROM inventory WHERE id=1")
        return cursor.fetchone()


def receipt(db, item):
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT digest,item_id,before_quantity,after_quantity,before_version,after_version "
            "FROM change_receipts WHERE change_id=%s",
            (item["plan"]["id"],),
        )
        return cursor.fetchone()


def prepared(real):
    _, client, db, _, _ = real
    before = row(db)
    item = approve(client, preview(client, (before[0] + 7) % 1000001))
    assert item["plan"]["before"] == {"quantity": before[0], "version": before[1]}
    return item, before


def reconcile(client, item):
    response = client.post(f"/api/changes/{item['plan']['id']}/reconcile", json={})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_success_duplicate_and_explicit_compensation(real):
    _, client, db, _, _ = real
    item, before = prepared(real)
    result = execute(client, item)
    assert result["status"] == "committed"
    after = row(db)
    assert after == (item["plan"]["after"]["quantity"], before[1] + 1)
    assert receipt(db, item) == tuple(ChangeConnector.expected(item))
    assert execute(client, item)["status"] == "committed"
    assert row(db) == after
    assert reconcile(client, item)["status"] == "committed"
    recovery = client.post(
        f"/api/changes/{item['plan']['id']}/recover",
        json={"request_id": "recovery" + item["plan"]["id"]},
    ).json()
    assert execute(client, recovery)["status"] == "preview"
    assert row(db) == after
    approve(client, recovery)
    assert execute(client, recovery)["status"] == "committed"
    assert row(db) == (before[0], before[1] + 2)
    assert receipt(db, recovery) == tuple(ChangeConnector.expected(recovery))


@pytest.mark.parametrize("return_value", [False, True])
def test_real_state_drift_including_aba_rejected(real, return_value):
    _, client, db, _, _ = real
    item, before = prepared(real)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE inventory SET quantity=%s,version=version+1 WHERE id=1",
            (before[0] if return_value else (before[0] + 2) % 1000001,),
        )
    drift = row(db)
    result = execute(client, item)
    assert result["status"] == "rejected"
    assert result["evidence"]["code"] == "CHANGE_DRIFT"
    assert row(db) == drift and receipt(db, item) is None


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM inventory WHERE id=0",
        "INSERT INTO inventory VALUES (999999,1,1)",
        "UPDATE inventory SET id=999999 WHERE id=0",
        "UPDATE change_receipts SET digest='x' WHERE change_id='absent'",
        "DELETE FROM change_receipts WHERE change_id='absent'",
        "CREATE TABLE forbidden(id INT)",
        "SELECT * FROM mysql.user",
        "SELECT * FROM db_agent.orders",
    ],
)
def test_real_minimal_account_rejects_out_of_scope_commands(real, sql):
    _, _, db, _, _ = real
    with db.cursor() as cursor, pytest.raises(pymysql.MySQLError) as error:
        cursor.execute(sql)
    assert error.value.args[0] in {1044, 1142, 1143}


def test_real_receipt_insert_permission_failure_rolls_back_update(real, monkeypatch):
    _, client, db, _, _ = real
    item, before = prepared(real)
    original = ChangeConnector.statement

    async def forbidden(self, connection, sql, args=None):
        if sql.startswith("INSERT INTO change_receipts"):
            # Real server rejection AFTER the actual UPDATE, not a fake exception.
            return await original(self, connection, "INSERT INTO inventory VALUES (999999,1,1)")
        return await original(self, connection, sql, args)

    monkeypatch.setattr(ChangeConnector, "statement", forbidden)
    assert execute(client, item)["status"] == "rolled_back"
    assert row(db) == before and receipt(db, item) is None


@pytest.mark.parametrize("commit_reached_server", [False, True])
def test_real_commit_boundary_driver_fault_and_restart_reconciliation(
    real, monkeypatch, commit_reached_server
):
    app, client, db, _, _ = real
    item, before = prepared(real)
    original = aiomysql.Connection.commit

    async def lost_ack(self):
        if commit_reached_server:
            await original(self)  # Actual MySQL commit; inject loss at the driver boundary.
        self.close()
        raise ConnectionError("synthetic missing commit response")

    with monkeypatch.context() as patch:
        patch.setattr(aiomysql.Connection, "commit", lost_ack)
        assert execute(client, item)["status"] == "unknown"
    assert execute(client, item)["status"] == "unknown"
    app.state.service.changes.store = ChangeStore(app.state.service.store)
    outcome = reconcile(client, item)
    assert outcome["status"] == ("committed" if commit_reached_server else "not_committed")
    assert row(db) == (
        (item["plan"]["after"]["quantity"], before[1] + 1) if commit_reached_server else before
    )
    assert bool(receipt(db, item)) is commit_reached_server
    assert execute(client, item)["status"] == outcome["status"]


def test_real_final_evidence_storage_failure_is_recoverable_without_retry(real, monkeypatch):
    app, client, db, _, _ = real
    item, before = prepared(real)
    original = app.state.service.changes.store.transition

    def fail(value, expected, status, evidence=None):
        if status == "committed":
            raise sqlite3.OperationalError("synthetic final disk failure")
        return original(value, expected, status, evidence)

    with monkeypatch.context() as patch:
        patch.setattr(app.state.service.changes.store, "transition", fail)
        assert client.post(f"/api/changes/{item['plan']['id']}/execute", json={}).status_code == 503
        assert execute(client, item)["status"] == "executing"
    app.state.service.changes.store = ChangeStore(app.state.service.store)
    assert reconcile(client, item)["status"] == "committed"
    assert row(db) == (item["plan"]["after"]["quantity"], before[1] + 1)
    assert receipt(db, item) == tuple(ChangeConnector.expected(item))


def test_real_two_identity_isolation_and_no_self_reported_approval(real):
    _, client, db, _, _ = real
    item, before = prepared(real)
    login(client, "bob")
    for action, body in [
        ("approve", {"digest": item["digest"]}),
        ("execute", {}),
        ("reconcile", {}),
        ("recover", {"request_id": "r" * 32}),
    ]:
        assert (
            client.post(f"/api/changes/{item['plan']['id']}/{action}", json=body).status_code == 400
        )
    assert client.get(f"/api/changes/{item['plan']['id']}").status_code == 400
    assert client.get("/api/changes").json()["changes"] == []
    assert (
        client.post(
            "/api/changes/preview",
            json={
                "target": "local_inventory",
                "item_id": 1,
                "quantity": 0,
                "request_id": "x" * 32,
                "approved": True,
            },
        ).status_code
        == 422
    )
    assert row(db) == before and receipt(db, item) is None


def test_real_execute_and_reconcile_share_dispatch_barrier(real, monkeypatch):
    _, client, db, _, _ = real
    item, before = prepared(real)
    entered, resume = Event(), Event()
    original = ChangeConnector.execute

    async def pause(self, value):
        entered.set()
        await asyncio.to_thread(resume.wait, 3)
        return await original(self, value)

    monkeypatch.setattr(ChangeConnector, "execute", pause)
    results = []
    first = Thread(target=lambda: results.append(execute(client, item)))
    second = Thread(target=lambda: results.append(reconcile(client, item)))
    first.start()
    assert entered.wait(2)
    second.start()
    try:
        assert row(db) == before
    finally:
        resume.set()
        first.join(5)
        second.join(5)
    assert len(results) == 2 and all(r["status"] == "committed" for r in results)
    assert row(db) == (item["plan"]["after"]["quantity"], before[1] + 1)


def test_real_revoke_after_update_before_commit_rolls_back(real, monkeypatch):
    _, client, db, path, data = real
    item, before = prepared(real)
    original = ChangeConnector.statement

    async def revoke(self, connection, sql, args=None):
        result = await original(self, connection, sql, args)
        if sql.startswith("UPDATE inventory"):
            data["users"][0]["change_approve"] = False
            identities(path, data)
        return result

    monkeypatch.setattr(ChangeConnector, "statement", revoke)
    response = client.post(f"/api/changes/{item['plan']['id']}/execute", json={})
    assert response.status_code == 401
    assert row(db) == before and receipt(db, item) is None


def test_real_later_change_prevents_compensation_but_does_not_erase_commit(real):
    _, client, db, _, _ = real
    item, _ = prepared(real)
    assert execute(client, item)["status"] == "committed"
    with db.cursor() as cursor:
        cursor.execute("UPDATE inventory SET version=version+1 WHERE id=1")
    assert reconcile(client, item)["status"] == "committed"
    assert (
        client.post(
            f"/api/changes/{item['plan']['id']}/recover", json={"request_id": "r" * 32}
        ).status_code
        == 400
    )


@pytest.mark.parametrize("drop_after_commit", [False, True])
def test_real_tcp_commit_loss_is_reconciled_without_duplicate_write(
    real, monkeypatch, drop_after_commit
):
    _, client, db, _, _ = real
    item, before = prepared(real)
    original = aiomysql.connect
    seen = Event()
    handlers = set()

    async def packet(reader):
        header = await reader.readexactly(4)
        length = int.from_bytes(header[:3], "little")
        assert length < 65536
        return header + await reader.readexactly(length)

    async def proxy(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", 13316)
        committing = False

        async def requests():
            nonlocal committing
            while True:
                data = await packet(reader)
                if data[4:5] == b"\x03" and data[5:].strip().upper() == b"COMMIT":
                    committing = True
                    if not drop_after_commit:
                        seen.set()
                        return
                upstream_writer.write(data)
                await upstream_writer.drain()

        async def responses():
            while True:
                data = await packet(upstream_reader)
                if committing and drop_after_commit:
                    assert data[4:5] == b"\x00", "server must actually acknowledge COMMIT"
                    seen.set()
                    return  # Discard the actual MySQL OK packet and break TCP.
                writer.write(data)
                await writer.drain()

        tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for child in tasks:
                child.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            upstream_writer.close()
            handlers.discard(task)

    async def start():
        return await asyncio.start_server(proxy, "127.0.0.1", 0)

    server = client.portal.call(start)
    port = server.sockets[0].getsockname()[1]

    async def redirected(**kwargs):
        assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 13316
        return await original(**{**kwargs, "port": port})

    async def stop():
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    try:
        with monkeypatch.context() as patch:
            # Transport-only fault proxy; identity checks still verify actual server_uuid.
            patch.setattr(aiomysql, "connect", redirected)
            assert execute(client, item)["status"] == "unknown"
        assert seen.is_set()
    finally:
        client.portal.call(stop)
    result = reconcile(client, item)
    assert result["status"] == ("committed" if drop_after_commit else "not_committed")
    assert execute(client, item)["status"] == result["status"]
    assert row(db) == (
        (item["plan"]["after"]["quantity"], before[1] + 1) if drop_after_commit else before
    )
    assert bool(receipt(db, item)) is drop_after_commit


def test_real_lock_timeout_stays_unknown_then_barrier_can_confirm_absence(real):
    app, client, db, _, _ = real
    item, before = prepared(real)
    claimed = app.state.service.changes.store.transition(item, "approved", "executing")
    app.state.service.changes.store.transition(claimed, "executing", "unknown")
    db.begin()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT quantity,version FROM inventory WHERE id=1 FOR UPDATE")
        assert reconcile(client, item)["status"] == "unknown"
    finally:
        db.rollback()
    assert reconcile(client, item)["status"] == "not_committed"
    assert row(db) == before and receipt(db, item) is None


def test_real_concurrent_approved_previews_only_one_changes_row(real):
    _, client, db, _, _ = real
    item, before = prepared(real)
    other = approve(client, preview(client, (before[0] + 9) % 1000001))
    results = []
    threads = [
        Thread(target=lambda value=value: results.append(execute(client, value)))
        for value in (item, other)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(result["status"] for result in results) == ["committed", "rejected"]
    assert row(db)[1] == before[1] + 1
    assert sum(receipt(db, value) is not None for value in (item, other)) == 1


def test_real_existing_receipt_requires_reconciliation(real):
    _, client, db, _, _ = real
    item, before = prepared(real)
    with db.cursor() as cursor:
        # Simulate a previously committed transaction whose local claim was restored.
        db.begin()
        cursor.execute(
            "UPDATE inventory SET quantity=%s,version=%s WHERE id=1",
            (item["plan"]["after"]["quantity"], before[1] + 1),
        )
        cursor.execute(
            "INSERT INTO change_receipts VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (item["plan"]["id"], *ChangeConnector.expected(item)),
        )
        db.commit()
    assert execute(client, item)["status"] == "unknown"
    assert reconcile(client, item)["status"] == "committed"
    assert row(db)[1] == before[1] + 1


def test_real_approval_evidence_failure_after_update_rolls_back(real, monkeypatch):
    app, client, db, _, _ = real
    item, before = prepared(real)
    original = ChangeConnector.statement
    original_get = app.state.service.changes.store.get
    broken = False

    def fail_get(*args, **kwargs):
        if broken:
            raise sqlite3.OperationalError("synthetic approval read failure")
        return original_get(*args, **kwargs)

    async def break_disk(self, connection, sql, args=None):
        nonlocal broken
        result = await original(self, connection, sql, args)
        if sql.startswith("UPDATE inventory"):
            broken = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(app.state.service.changes.store, "get", fail_get)
        patch.setattr(ChangeConnector, "statement", break_disk)
        assert client.post(f"/api/changes/{item['plan']['id']}/execute", json={}).status_code == 503
    assert row(db) == before and receipt(db, item) is None


@pytest.mark.parametrize("lose_visibility", [False, True])
def test_real_trigger_guard_detects_admin_schema_change(real, lose_visibility):
    import subprocess
    from uuid import uuid4

    _, client, db, _, _ = real
    root = Path(__file__).resolve().parents[1]
    name = "changes_test_" + uuid4().hex
    item, before = prepared(real)

    def admin(sql):
        # This exact new Compose target only. Root never enters product configuration.
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(root / "infra/changes/compose.yaml"),
                "--env-file",
                str(root / "outputs/changes/compose.env"),
                "exec",
                "-T",
                "mysql",
                "sh",
                "-c",
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot db_agent_changes',
            ],
            input=sql,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, "isolated synthetic administrator operation failed"

    admin(
        f"CREATE TRIGGER {name} BEFORE UPDATE ON inventory "
        "FOR EACH ROW SET NEW.quantity=NEW.quantity"
    )
    try:
        if lose_visibility:
            admin("REVOKE TRIGGER ON inventory FROM 'db_agent_change_inspector'@'localhost'")
        with db.cursor() as cursor:
            cursor.execute("SELECT trigger_count FROM change_schema_guard")
            assert cursor.fetchone()[0] == (-1 if lose_visibility else 1)
        response = client.post(f"/api/changes/{item['plan']['id']}/execute", json={})
        assert response.status_code == 400
        assert row(db) == before and receipt(db, item) is None
    finally:
        if lose_visibility:
            admin("GRANT TRIGGER ON inventory TO 'db_agent_change_inspector'@'localhost'")
        admin(f"DROP TRIGGER {name}")


def test_real_same_target_two_authorized_users_isolate_records_and_requests(real):
    _, client, db, path, data = real
    data["users"][1].update(change_targets=["local_inventory"], change_approve=True)
    identities(path, data)
    client.get("/api/auth/session")
    login(client)
    before = row(db)
    item = approve(client, preview(client, (before[0] + 1) % 1000001, "shared-request-id"))
    login(client, "bob")
    other = approve(client, preview(client, (before[0] + 2) % 1000001, "shared-request-id"))
    assert other["plan"]["id"] != item["plan"]["id"]
    assert [r["plan"]["id"] for r in client.get("/api/changes").json()["changes"]] == [
        other["plan"]["id"]
    ]
    for action, body in [
        ("approve", {"digest": item["digest"]}),
        ("execute", {}),
        ("reconcile", {}),
        ("recover", {"request_id": "r" * 32}),
    ]:
        assert (
            client.post(f"/api/changes/{item['plan']['id']}/{action}", json=body).status_code == 400
        )
    assert client.get(f"/api/changes/{item['plan']['id']}").status_code == 400
    assert execute(client, other)["status"] == "committed"
    login(client)
    assert execute(client, item)["status"] == "rejected"
    assert receipt(db, item) is None and receipt(db, other) is not None


def test_real_logout_watcher_cancels_after_update_and_new_login_can_reconcile(real, monkeypatch):
    app, client, db, _, _ = real
    item, before = prepared(real)
    entered = Event()
    original = ChangeConnector.statement

    async def pause(self, connection, sql, args=None):
        result = await original(self, connection, sql, args)
        if sql.startswith("UPDATE inventory"):
            entered.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(ChangeConnector, "statement", pause)
    outcomes = []

    def submit():
        try:
            outcomes.append(
                client.post(f"/api/changes/{item['plan']['id']}/execute", json={}).status_code
            )
        except BaseException as error:
            outcomes.append(type(error).__name__)

    thread = Thread(target=submit)
    thread.start()
    assert entered.wait(3)
    client.post("/api/auth/logout", json={})
    thread.join(5)
    assert not thread.is_alive() and outcomes
    assert not app.state.service.changes.pending
    login(client)
    saved = client.get(f"/api/changes/{item['plan']['id']}").json()
    assert saved["status"] == "unknown"
    assert reconcile(client, item)["status"] == "not_committed"
    assert row(db) == before and receipt(db, item) is None


def test_real_approval_expiry_during_lock_wait_prevents_update(real):
    import json
    import time

    from db_agent.changes import digest

    app, client, db, _, _ = real
    item, before = prepared(real)
    item["plan"]["expires_at"] = time.time() + 0.4
    item["digest"] = digest(item["plan"])
    item["approval"]["digest"] = item["digest"]
    with app.state.service.store.connection() as store:
        store.execute(
            "UPDATE changes SET plan=?,digest=?,approval=? WHERE id=?",
            (
                json.dumps(item["plan"]),
                item["digest"],
                json.dumps(item["approval"]),
                item["plan"]["id"],
            ),
        )
    db.begin()
    with db.cursor() as cursor:
        cursor.execute("SELECT quantity FROM inventory WHERE id=1 FOR UPDATE")
    results = []
    thread = Thread(target=lambda: results.append(execute(client, item)))
    thread.start()
    time.sleep(0.6)
    db.rollback()
    thread.join(5)
    assert results[0]["status"] == "rolled_back"
    assert row(db) == before and receipt(db, item) is None
