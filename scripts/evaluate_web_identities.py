"""Explicit loopback HTTP + MySQL identity acceptance; --model adds real model calls."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from db_agent.config import (
    load_analysis_settings,
    load_database_settings,
    load_query_settings,
    load_settings,
)
from db_agent.db import MetadataConnector
from db_agent.web_identity import password_hash

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-DB-Agent-Client": "web", "Content-Type": "application/json"}
ALICE_SQL = (
    "SELECT COUNT(*) AS n, SUM(total_amount) AS amount FROM orders "
    "WHERE status = 'paid' AND paid_at >= '2026-02-01' AND paid_at < '2026-03-01'"
)
BOB_SQL = "SELECT id, display_name FROM customers WHERE id = 1"
ALICE_ROWS = [[3, "130.00"]]
BOB_ROWS = [[1, "Customer Alpha"]]
# Explicit, exposed fixture oracle: paid orders 1001/1002/1004, amounts 100+30+0.
# No expected values are obtained by rerunning a candidate or supplied to the model.
WORKER = """
import socket, sys, uvicorn
from pathlib import Path
from db_agent.web import create_app
app = create_app(store_path=Path(sys.argv[1]), identity_path=Path(sys.argv[2]))
server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
server.run(sockets=[socket.socket(fileno=int(sys.argv[3]))])
"""
BASE_CHECKS = (
    "database_target", "anonymous_rejected", "alice_login", "bob_login",
    "alice_metadata", "bob_metadata", "alice_query", "bob_query", "bob_orders_blocked",
    "alice_delivery", "bob_delivery", "bob_cannot_access_alice", "alice_cannot_access_bob",
    "alice_knowledge", "bob_knowledge", "knowledge_cross_identity", "relogin_persistence",
    "browser", "model_alice", "model_bob", "model_knowledge", "model_foreign_knowledge",
    "model_outside_context", "model_revoked_knowledge", "model_inflight_cancel",
    "model_inflight_policy_change", "model_disabled",
    "permission_generation", "restored_policy_stays_invalid", "same_policy_users_isolated",
    "source_unchanged",
)
EXPECTED_BUDGETS = {
    "analysis": {"review_scan_rows": 100000, "review_join_rows": 1000000,
                 "review_sort_rows": 100000, "timeout_seconds": 10},
    "query": {"max_rows": 100, "max_result_bytes": 32768,
              "execution_timeout_seconds": 5, "operation_timeout_seconds": 15},
    "model": {"max_model_calls": 4, "max_tool_calls": 6,
              "run_timeout_seconds": 60, "max_output_tokens": 1024},
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def source_hashes():
    paths = [*ROOT.joinpath("src/db_agent").glob("*.py"),
             *ROOT.joinpath("frontend/src").glob("*"),
             *ROOT.joinpath("frontend/scripts").glob("*.mjs"),
             Path(__file__), ROOT / "tests/fixtures/mysql_business.sql", ROOT / "uv.lock"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


def public_budgets(model_selected):
    limits = {"analysis": load_analysis_settings().model_dump(mode="json"),
              "query": load_query_settings().model_dump(mode="json")}
    if model_selected:
        # Explicit field inclusion cannot accidentally serialize provider credentials.
        limits["model"] = load_settings().model_dump(mode="json", include={
            "request_timeout_seconds", "run_timeout_seconds", "max_output_tokens",
            "max_model_calls", "max_tool_calls",
        })
    return limits


def check_budgets(limits):
    mismatches = [f"{section}.{key}: expected {expected}, got {limits[section].get(key)}"
                  for section, values in EXPECTED_BUDGETS.items() if section in limits
                  for key, expected in values.items() if limits[section].get(key) != expected]
    require(not mismatches,
            "acceptance requires original default budgets; " + "; ".join(mismatches))


def save_private(path, value):
    # Replace only this probe's explicitly owned config, never the project .env.
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def request(client, method, path, expected=200, body=None):
    response = client.request(method, "/api" + path,
                              **({"json": body} if body is not None else {}))
    require(response.status_code == expected,
            f"{method} {path}: expected HTTP {expected}, got {response.status_code}")
    return response


def login(client, name, password):
    response = request(client, "POST", "/auth/login",
                       body={"username": name, "password": password})
    data = response.json()
    require(data["authenticated"] is True and data["identity"]["username"] == name,
            "login did not return the requested authenticated principal")
    require("HttpOnly" in response.headers["set-cookie"]
            and "SameSite=strict" in response.headers["set-cookie"], "cookie boundary missing")
    client.headers["X-DB-Agent-Session"] = data["session_id"]
    session = request(client, "GET", "/auth/session").json()
    require(session["session_id"] == data["session_id"], "session binding mismatch")
    # Keep session/cookie/password out of acceptance output, including failures.
    return data["identity"]


def start_run(client, prompt, mode="query"):
    conversation = request(client, "POST", "/conversations", 201, {}).json()["id"]
    return request(client, "POST", f"/conversations/{conversation}/runs", 202, {
        "prompt": prompt, "mode": mode, "request_id": uuid4().hex,
    }).json()


def terminal_run(client, run, timeout=80):
    deadline = time.monotonic() + timeout
    while run["status"] in {"running", "cancelling"} and time.monotonic() < deadline:
        time.sleep(0.1)
        run = request(client, "GET", f'/runs/{run["id"]}').json()
    require(run["status"] not in {"running", "cancelling"}, "run exceeded polling deadline")
    return run


def run_query(client, prompt, mode="query"):
    return terminal_run(client, start_run(client, prompt, mode))


def cancelled(run):
    require(run["status"] == "cancelled" and run.get("finished_at")
            and (run.get("error") or {}).get("code") == "CANCELLED",
            "task did not reach a persisted cancelled terminal state")


def read_probe_run(history, identifier):
    # Administrative observation of this probe's own store, never a user API or
    # an old-cookie bypass. mode=ro cannot create or change the database.
    with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT status,payload,context_eligible FROM runs WHERE id=?",
                         (identifier,)).fetchone()
        require(row is not None, "probe history is missing the submitted run")
        run = json.loads(row[1])
        require(run["id"] == identifier and run["status"] == row[0],
                "persisted run identifiers/status disagree")
        count = db.execute("SELECT COUNT(*) FROM runs WHERE conversation_id=?",
                           (run["conversation_id"],)).fetchone()[0]
    return run, {"rows_in_conversation": count, "context_eligible": row[2]}


def successful(run, expected, *, knowledge=None):
    require(run["status"] == "completed" and run["error"] is None,
            "run did not complete successfully")
    require(run.get("missing_query_reports", 0) == 0 and len(run["queries"]) == 1,
            "expected exactly one complete query report")
    report = run["queries"][0]["report"]
    require(report["status"] == "ok" and report["decision"] == "ALLOW"
            and report["execution_status"] == "completed", "query execution was not complete")
    require(report["result"]["truncated"] is False
            and report["result"]["rows"] == expected, "independent fixture oracle mismatch")
    if knowledge is not None:
        require({item["id"]: item["digest"] for item in report.get("business_knowledge", [])}
                == knowledge, "confirmed knowledge provenance mismatch")
    return report


def result_path(run):
    return (f'/conversations/{run["conversation_id"]}/runs/{run["id"]}/results/'
            f'{run["queries"][0]["report"]["result_id"]}')


def draft(table, title):
    return {
        "kind": "metric", "title": title, "tables": [table],
        "definition": (
            "支付成交额为status='paid'订单total_amount之和，笔数为COUNT(*)；"
            "按paid_at统计日期，时间区间左闭右开，不含取消和已退款订单。"
            if table == "orders" else "客户数量为customers表的COUNT(*)，不添加其他筛选条件。"
        ),
        "source": "tests/fixtures/mysql_business.sql", "source_version": "public-small-fixture-v1",
        "invalidation_condition": "源数据结构、状态或指标语义变化时人工撤销。",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }


def confirm(client, document):
    item = request(client, "POST", "/knowledge", 201, document).json()
    shown = request(client, "GET", f'/knowledge/{item["id"]}').json()
    require(shown["digest"] == item["digest"] and shown["state"] == "draft",
            "draft readback does not match")
    confirmed = request(client, "POST", f'/knowledge/{item["id"]}/confirm',
                        body={"digest": item["digest"]}).json()
    require(confirmed["state"] == "confirmed", "knowledge not confirmed")
    return confirmed


class Acceptance:
    def __init__(self, output, frozen, budgets, model_selected):
        self.output, self.frozen = output, frozen
        self.budgets, self.model_selected = budgets, model_selected
        self.checks = {name: {"name": name, "status": "not_run"} for name in BASE_CHECKS}
        self.runs = {}

    def check(self, name, function, dependencies=()):
        item = self.checks[name]
        if any(self.checks[key]["status"] != "passed" for key in dependencies):
            item.update(status="not_run", reason="prerequisite failed", dependencies=dependencies)
            return
        try:
            require(source_hashes() == self.frozen, "source changed during acceptance")
            require(public_budgets(self.model_selected) == self.budgets,
                    "configured budgets changed during acceptance")
            item["observed"] = function()
            item["status"] = "passed"
        except Exception as exc:
            item.update(status="failed", error_type=type(exc).__name__, error=(
                str(exc) if isinstance(exc, AssertionError) else "probe operation failed"
            ))
        print(json.dumps({"check": name, "status": item["status"]}), flush=True)

    def query(self, name, client, sql, rows, mode="query", knowledge=None):
        # Store actual run even when oracle validation fails; never retry a model failure.
        run = run_query(client, sql, mode)
        self.runs[name] = run
        report = successful(run, rows, knowledge=knowledge)
        return {"run_id": run["id"], "rows": report["result"]["rows"],
                "decision": report["decision"], "server_version": report["server_version"]}


def exercise(probe, clients, passwords, policy, policy_path, args, base_url):
    alice, bob, anonymous = (clients[name] for name in ("alice", "bob", "anonymous"))
    identities, knowledge = {}, {}

    def log_in(name):
        identities[name] = login(clients[name], name, passwords[name])
        return identities[name]

    def anonymous_rejected():
        require(request(anonymous, "GET", "/auth/session").json()["authenticated"] is False,
                "anonymous principal was authenticated")
        statuses = []
        for method, target, body in [
            ("GET", "/status", None), ("GET", "/conversations", None),
            ("GET", "/schema/tables", None), ("GET", "/schema/tables/orders", None),
            ("GET", "/runs/" + "0" * 32, None),
            ("POST", "/runs/" + "0" * 32 + "/cancel", {}),
            ("GET", "/knowledge", None),
        ]:
            statuses.append(request(anonymous, method, target, 401, body).status_code)
        return {"http_statuses": statuses}

    def metadata(client, tables):
        data = request(client, "GET", "/schema/tables").json()
        require({table["name"] for table in data["tables"]} == tables,
                "table list does not equal identity policy")
        for table in tables:
            require(request(client, "GET", f"/schema/tables/{table}").json()["columns"],
                    "authorized schema missing")
        if tables == {"customers"}:
            rejected = request(client, "GET", "/schema/tables/orders", 400).json()
            require(rejected["error"]["code"] == "PERMISSION_DENIED", "metadata not denied")
        return data

    def blocked_orders():
        run = run_query(bob, "SELECT id FROM orders LIMIT 1")
        probe.runs["bob_orders_blocked"] = run
        report = run["queries"][0]["report"]
        require(report["decision"] == "BLOCK" and report["execution_status"] == "not_started"
                and report["result"] is None, "unauthorized query was not blocked")
        return report

    def delivery(name):
        client, run = clients[name], probe.runs[name + "_query"]
        path = result_path(run)
        snapshot = request(client, "GET", path).json()
        require(snapshot["report"] == run["queries"][0]["report"], "saved report changed")
        selection = {"dimension": 0, "measure": 1} if name == "alice" else {
            "dimension": 1, "measure": 0,
        }
        analysis = request(client, "POST", path + "/analysis", body=selection).json()
        require(analysis["analysis"] is not None, "analysis missing")
        for format_name in ("json", "html"):
            response = request(client, "POST", path + "/export", body={"format": format_name})
            require("attachment;" in response.headers["content-disposition"], "not an attachment")
            (probe.output / f"{name}-result.{format_name}").write_bytes(response.content)
            if format_name == "json":
                require(response.json() == snapshot, "export changed saved snapshot")
            else:
                require(("130.00" if name == "alice" else "Customer Alpha") in response.text,
                        "HTML does not include the actual result")
        return {"result_id": snapshot["result_id"], "exports": ["json", "html"]}

    def isolate(viewer, owner):
        run = probe.runs[owner + "_query"]
        path, cid = result_path(run), run["conversation_id"]
        targets = [
            ("GET", f"/conversations/{cid}", None),
            ("PATCH", f"/conversations/{cid}", {"title": "cross identity must fail"}),
            ("DELETE", f"/conversations/{cid}", None),
            ("POST", f"/conversations/{cid}/runs", {
                "prompt": "SELECT id FROM customers LIMIT 1", "mode": "query",
                "request_id": uuid4().hex,
            }),
            ("GET", f'/runs/{run["id"]}', None),
            ("POST", f'/runs/{run["id"]}/cancel', {}), ("GET", path, None),
            ("POST", path + "/analysis", {"dimension": 0, "measure": 1}),
            ("POST", path + "/export", {"format": "json"}),
            ("POST", path + "/export", {"format": "html"}),
        ]
        for method, target, body in targets:
            request(clients[viewer], method, target, 404, body)
        listed = request(clients[viewer], "GET", "/conversations").json()["conversations"]
        require(cid not in {item["id"] for item in listed}, "foreign conversation listed")
        require(request(clients[viewer], "GET", "/status").json()["active_run_id"] is None,
                "completed foreign run exposed as active")
        return {"denied_endpoints": len(targets), "http_status": 404}

    def create_knowledge(name):
        knowledge[name] = confirm(clients[name], draft("orders" if name == "alice" else
                                                      "customers", name + " metric"))
        if name == "alice":
            knowledge["alice_customers"] = confirm(alice, draft("customers", "local only metric"))
        return knowledge[name]

    def knowledge_denied(client, item):
        unknown = request(client, "GET", "/knowledge/" + "0" * 32, 400).json()
        target = f'/knowledge/{item["id"]}'
        require(request(client, "GET", target, 400).json() == unknown,
                "foreign knowledge has a different error from unknown knowledge")
        request(client, "POST", target + "/confirm", 400, {"digest": item["digest"]})
        request(client, "POST", target + "/revoke", 400, {"reason": "isolation probe"})
        listed = request(client, "GET", "/knowledge").json()["knowledge"]
        require(item["id"] not in {entry["id"] for entry in listed}, "foreign knowledge listed")

    def knowledge_isolation():
        knowledge_denied(bob, knowledge["alice"])
        knowledge_denied(bob, knowledge["alice_customers"])
        knowledge_denied(alice, knowledge["bob"])
        return {"cross_identity_items_denied": 3}

    def relogin():
        for name in ("alice", "bob"):
            before = identities[name]["authorization_version"]
            log_in(name)
            require(identities[name]["authorization_version"] == before, "login changed policy")
            request(clients[name], "GET", result_path(probe.runs[name + "_query"]))
            item = request(clients[name], "GET", f'/knowledge/{knowledge[name]["id"]}').json()
            require(item["state"] == "confirmed", "knowledge not persisted through relogin")
        return {"principals": 2, "history_and_knowledge_retained": True}

    def browser():
        script = ROOT / "frontend/scripts/evaluate-identities.mjs"
        require(script.is_file() and (ROOT / "frontend/dist/index.html").is_file(),
                "browser probe requires its script and npm run build")
        process = subprocess.run(["node", str(script)], input=json.dumps({
            "base_url": base_url, "credentials": passwords, "output_dir": str(probe.output),
        }), text=True, capture_output=True, timeout=240, check=False, cwd=ROOT / "frontend")
        summary = json.loads(process.stdout)
        # Preserve individual browser failures; a nonzero exit never becomes a pass.
        probe.checks["browser"]["observed"] = summary
        require(process.returncode == 0, "real browser probe failed; do not infer API success")
        require(summary["planned"] > 0 and summary["passed"] == summary["planned"],
                "real browser checks did not all pass")
        return summary

    def refused_reference(name, client, item, code, revoke=False):
        if revoke:
            request(client, "POST", f'/knowledge/{item["id"]}/revoke',
                    body={"reason": "fixed synthetic acceptance lifecycle"})
        run = run_query(client, f'[[knowledge:{item["id"]}]] 统计客户数量。', "chat")
        probe.runs[name] = run
        require(run["status"] == "failed" and run["error"]["code"] == code
                and not run["queries"], "knowledge reference was not rejected before query")
        require(not any(event["event"].startswith("model_") for event in run["events"]),
                "rejected knowledge reached a model stage")
        return {"error": run["error"], "queries": run["queries"]}

    def live_progress(name):
        run = start_run(alice, "统计orders中status为paid且paid_at在2026年2月的订单，"
                        "只返回COUNT(*) AS n和SUM(total_amount) AS amount。", "chat")
        probe.runs[name] = run
        progress = request(alice, "GET", f'/runs/{run["id"]}').json()
        probe.runs[name] = progress
        observed = {
            "run_id": run["id"], "owner_progress": progress,
            "server_statement_status": "unknown",
            "limitation": "Web task stopped; provider and SQL server cancellation remain unknown.",
        }
        probe.checks[name]["observed"] = observed
        require(progress["status"] == "running", "task completed before in-flight observation")
        require(any(event["event"] == "run_started" for event in progress["events"]),
                "owner cannot observe actual run progress")
        require(request(alice, "GET", "/status").json()["active_run_id"] == run["id"],
                "owner cannot observe its running task")
        return progress, observed

    def inflight_cancel():
        name = "model_inflight_cancel"
        try:
            run, observed = live_progress(name)
            require(request(bob, "GET", "/status").json()["active_run_id"] is None,
                    "foreign active run leaked through status")
            request(bob, "GET", f'/runs/{run["id"]}', 404)
            request(bob, "POST", f'/runs/{run["id"]}/cancel', 404, {})
            progress = request(alice, "GET", f'/runs/{run["id"]}').json()
            probe.runs[name] = progress
            observed["after_foreign_cancel"] = progress
            require(progress["status"] == "running",
                    "task was not still running after foreign cancel rejection")
            acknowledgment = request(alice, "POST", f'/runs/{run["id"]}/cancel', body={}).json()
            observed["owner_cancel_ack_status"] = acknowledgment["status"]
            require(acknowledgment["status"] in {"cancelling", "cancelled"},
                    "owner cancellation was not accepted while task was active")
            terminal = terminal_run(alice, acknowledgment, timeout=10)
            probe.runs[name] = terminal
            cancelled(terminal)
            deadline = time.monotonic() + 5
            while True:
                active = request(alice, "GET", "/status").json()["active_run_id"]
                if active is None or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            require(active is None, "cancelled task still occupies the owner's registry")
            persisted, counts = read_probe_run(probe.output / "history.sqlite3", run["id"])
            cancelled(persisted)
            require(counts == {"rows_in_conversation": 1, "context_eligible": 0},
                    "cancelled request was replayed or became trusted conversation context")
            observed.update(foreign_run_http=404, foreign_cancel_http=404,
                            registry_active_run_id=active, persisted=counts)
            return observed
        finally:
            # If an assertion failed while it was still active, stop only this
            # probe-created task so later checks can proceed. Failure stays failed.
            run = probe.runs.get(name)
            if run and run["status"] in {"running", "cancelling"}:
                try:
                    acknowledgment = request(alice, "POST", f'/runs/{run["id"]}/cancel', body={})
                    probe.runs[name] = terminal_run(alice, acknowledgment.json(), timeout=10)
                    probe.checks[name].setdefault("observed", {})["cleanup_cancelled"] = (
                        probe.runs[name]["status"] == "cancelled"
                    )
                except Exception:
                    probe.checks[name]["cleanup_error"] = "could not confirm probe task stopped"

    def inflight_policy_change():
        name = "model_inflight_policy_change"
        run, observed = live_progress(name)
        old_generation = identities["alice"]["authorization_version"]
        changed = copy.deepcopy(policy)
        changed["users"][1].update(model_enabled=False, model_tables=[])
        save_private(policy_path, changed)
        # Do not make an HTTP request after changing policy until background
        # cancellation is observed. This specifically exercises the watcher.
        deadline = time.monotonic() + 10
        while True:
            persisted, counts = read_probe_run(probe.output / "history.sqlite3", run["id"])
            probe.runs[name] = persisted
            if persisted["status"] not in {"running", "cancelling"}:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("background policy watcher did not stop the task")
            time.sleep(0.1)
        cancelled(persisted)
        require(counts == {"rows_in_conversation": 1, "context_eligible": 0},
                "revoked request was replayed or became trusted conversation context")
        request(alice, "GET", "/status", 401)
        request(alice, "GET", f'/runs/{run["id"]}', 401)
        log_in("alice")
        require(identities["alice"]["authorization_version"] != old_generation,
                "authorization generation did not change")
        request(alice, "GET", f'/runs/{run["id"]}', 404)
        request(alice, "GET", f'/conversations/{run["conversation_id"]}', 404)
        require(request(alice, "GET", "/status").json()["active_run_id"] is None,
                "revoked run reappeared after login")
        observed.update(background_cancel_without_http_trigger=True, old_cookie_http=401,
                        new_session_old_run_http=404, persisted=counts)
        return observed

    def model_disabled():
        changed = copy.deepcopy(policy)
        changed["users"][1].update(model_enabled=False, model_tables=[])
        save_private(policy_path, changed)
        request(bob, "GET", "/status", 401)
        log_in("bob")
        conversation = request(bob, "POST", "/conversations", 201, {}).json()["id"]
        request(bob, "POST", f"/conversations/{conversation}/runs", 403, {
            "prompt": "统计客户数量", "mode": "chat", "request_id": uuid4().hex,
        })
        return {"model_disabled_http_status": 403}

    def generation_changed():
        old = identities["alice"]["authorization_version"]
        changed = copy.deepcopy(policy)
        changed["users"][0].update(allowed_tables=["customers"], model_tables=["customers"])
        save_private(policy_path, changed)
        request(alice, "GET", "/status", 401)
        log_in("alice")
        require(identities["alice"]["authorization_version"] != old, "generation not advanced")
        request(alice, "GET", result_path(probe.runs["alice_query"]), 404)
        request(alice, "GET", "/conversations/" + probe.runs["alice_query"]["conversation_id"], 404)
        knowledge_denied(alice, knowledge["alice"])
        request(alice, "GET", "/schema/tables/orders", 400)
        return {"old_cookie": 401, "old_result": 404, "old_knowledge": 400}

    def restored():
        old = identities["alice"]["authorization_version"]
        save_private(policy_path, policy)
        request(alice, "GET", "/status", 401)
        log_in("alice")
        require(identities["alice"]["authorization_version"] != old, "generation was restored")
        request(alice, "GET", result_path(probe.runs["alice_query"]), 404)
        knowledge_denied(alice, knowledge["alice"])
        return {"old_result": 404, "old_knowledge": 400}

    def same_policy():
        changed = copy.deepcopy(policy)
        changed["users"][1].update(allowed_tables=["orders", "customers"], model_tables=["orders"])
        save_private(policy_path, changed)
        log_in("alice")
        log_in("bob")
        item = confirm(alice, draft("customers", "equal policy identity isolation"))
        knowledge_denied(bob, item)
        run = run_query(alice, BOB_SQL)
        probe.runs["same_policy_alice"] = run
        successful(run, BOB_ROWS)
        request(bob, "GET", result_path(run), 404)
        return {"same_account_and_allowlists": True,
                "foreign_result": 404, "foreign_knowledge": 400}

    probe.check("anonymous_rejected", anonymous_rejected)
    probe.check("alice_login", lambda: log_in("alice"))
    probe.check("bob_login", lambda: log_in("bob"))
    probe.check("alice_metadata", lambda: metadata(alice, {"orders", "customers"}),
                ("alice_login",))
    probe.check("bob_metadata", lambda: metadata(bob, {"customers"}), ("bob_login",))
    for name, sql, rows in (("alice", ALICE_SQL, ALICE_ROWS), ("bob", BOB_SQL, BOB_ROWS)):
        probe.check(name + "_query", lambda n=name, s=sql, r=rows:
                    probe.query(n + "_query", clients[n], s, r), (name + "_metadata",))
        probe.check(name + "_delivery", lambda n=name: delivery(n), (name + "_query",))
        probe.check(name + "_knowledge", lambda n=name: create_knowledge(n), (name + "_metadata",))
    probe.check("bob_orders_blocked", blocked_orders, ("bob_login",))
    probe.check("bob_cannot_access_alice", lambda: isolate("bob", "alice"), ("alice_query",))
    probe.check("alice_cannot_access_bob", lambda: isolate("alice", "bob"), ("bob_query",))
    probe.check("knowledge_cross_identity", knowledge_isolation,
                ("alice_knowledge", "bob_knowledge"))
    probe.check("relogin_persistence", relogin,
                ("alice_query", "bob_query", "alice_knowledge", "bob_knowledge"))
    baseline_logins_finished_at = time.monotonic()
    if args.browser:
        probe.check("browser", browser, ("alice_login", "bob_login"))
    else:
        probe.checks["browser"].update(status="not_selected", reason="requires --browser")
    if args.model:
        probe.check("model_alice", lambda: probe.query("model_alice", alice,
                    "统计orders中status为paid且paid_at在2026年2月的订单，"
                    "只返回COUNT(*) AS n和SUM(total_amount) AS amount。", ALICE_ROWS, "chat"),
                    ("alice_metadata",))
        probe.check("model_bob", lambda: probe.query("model_bob", bob,
                    "查询customers表中id等于1的客户，只返回id和display_name。", BOB_ROWS, "chat"),
                    ("bob_metadata",))
        probe.check("model_knowledge", lambda: probe.query("model_knowledge", alice,
                    f'[[knowledge:{knowledge["alice"]["id"]}]] 统计2026年2月支付成交额，'
                    '只返回笔数n和金额amount。', ALICE_ROWS, "chat",
                    {knowledge["alice"]["id"]: knowledge["alice"]["digest"]}),
                    ("alice_knowledge",))
        probe.check("model_foreign_knowledge", lambda: refused_reference(
                    "model_foreign_knowledge", bob, knowledge["alice"], "KNOWLEDGE_UNAVAILABLE"),
                    ("alice_knowledge", "bob_login"))
        probe.check("model_outside_context", lambda: refused_reference(
                    "model_outside_context", alice, knowledge["alice_customers"],
                    "PERMISSION_DENIED"),
                    ("alice_knowledge",))
        probe.check("model_revoked_knowledge", lambda: refused_reference(
                    "model_revoked_knowledge", bob, knowledge["bob"],
                    "KNOWLEDGE_UNAVAILABLE", True),
                    ("bob_knowledge",))
        probe.check("model_inflight_cancel", inflight_cancel, ("alice_login", "bob_login"))
    else:
        for name in BASE_CHECKS:
            if name.startswith("model_") and name != "model_disabled":
                probe.checks[name].update(status="not_selected", reason="requires --model")
    if args.browser:
        # API + browser legitimately log in up to thirteen times. Respect the
        # ten/minute limit; let the first four expire before up to six lifecycle
        # logins. This schedules requests, does not retry a failed assertion.
        remaining = max(0, 62 - (time.monotonic() - baseline_logins_finished_at))
        print(json.dumps({"phase": "login_rate_limit_cooldown", "seconds": round(remaining)}),
              flush=True)
        deadline = time.monotonic() + remaining
        while time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))
    if args.model:
        probe.check("model_inflight_policy_change", inflight_policy_change,
                    ("alice_login", "model_inflight_cancel"))
    probe.check("model_disabled", model_disabled, ("bob_login",))
    probe.check("permission_generation", generation_changed, ("alice_query", "alice_knowledge"))
    probe.check("restored_policy_stays_invalid", restored, ("permission_generation",))
    probe.check("same_policy_users_isolated", same_policy, ("restored_policy_stays_invalid",))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run real local HTTP/MySQL checks")
    parser.add_argument("--model", action="store_true", help="add explicit real model acceptance")
    parser.add_argument("--browser", action="store_true", help="add real Chromium UI acceptance")
    args = parser.parse_args()
    if not args.run:
        parser.error("pass --run; --model and --browser are separately opt-in")
    if Path.cwd().resolve() != ROOT:
        parser.error("run from this checkout root; no other configuration is searched")
    try:
        settings = load_database_settings()
        budgets = public_budgets(args.model)
    except Exception:
        parser.error("explicit local configuration is missing or invalid; values are not printed")
    if ((settings.host, settings.port, settings.database, settings.user)
            != ("127.0.0.1", 13306, "db_agent", "db_agent_reader")
            or not {"orders", "customers"} <= set(settings.allowed_tables)):
        parser.error("requires 127.0.0.1:13306/db_agent reader and orders/customers allowlist")
    try:
        check_budgets(budgets)
    except AssertionError as exc:
        # These are numeric budgets only; no model or database credential is serialized.
        print(json.dumps({"configured_budgets": budgets, "configuration_rejected": True}))
        parser.error(str(exc))
    output = ROOT / "outputs/web-identity" / uuid4().hex
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    probe = Acceptance(output, source_hashes(), budgets, args.model)
    passwords = {name: secrets.token_urlsafe(24) for name in ("alice", "bob")}
    policy = {"version": 1, "users": [
        {"username": name, "display_name": name.title(), "password_hash": password_hash(password),
         "allowed_tables": ["orders", "customers"] if name == "alice" else ["customers"],
         "model_enabled": True, "model_tables": ["orders"] if name == "alice" else ["customers"]}
        for name, password in passwords.items()
    ]}
    policy_path = output / "identities.json"
    save_private(policy_path, policy)
    started = datetime.now(UTC).isoformat()
    probe.check("database_target", lambda: asyncio.run(MetadataConnector(settings).check()))
    process = None
    with ExitStack() as stack:
        try:
            require(probe.checks["database_target"]["status"] == "passed",
                    "local database unavailable")
            listener = stack.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            log = stack.enter_context((output / "server.log").open("w"))
            process = subprocess.Popen(
                [sys.executable, "-c", WORKER, str(output / "history.sqlite3"),
                 str(policy_path), str(listener.fileno())], cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT, pass_fds=(listener.fileno(),),
            )
            clients = {name: stack.enter_context(httpx.Client(
                base_url=base_url, headers=HEADERS, trust_env=False, timeout=20,
            )) for name in ("alice", "bob", "anonymous")}
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if clients["anonymous"].get("/api/auth/session").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("probe server did not become ready; see private server.log")
            exercise(probe, clients, passwords, policy, policy_path, args, base_url)
        except Exception as exc:
            probe.checks["server"] = {"name": "server", "status": "failed",
                                      "error_type": type(exc).__name__}
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            probe.check("source_unchanged",
                        lambda: {"source_unchanged": source_hashes() == probe.frozen})
    selected = [item for item in probe.checks.values() if item["status"] != "not_selected"]
    passed = sum(item["status"] == "passed" for item in selected)
    report = {
        "version": "web-identities-v2", "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(), "evidence": "real loopback HTTP and MySQL",
        "role": "exposed fixed synthetic acceptance; no production or unbiased accuracy claim",
        "model_selected": args.model, "browser_selected": args.browser,
        "configured_budgets": budgets, "expected_default_budgets": EXPECTED_BUDGETS,
        "source_sha256": probe.frozen, "source_unchanged": source_hashes() == probe.frozen,
        "fixture": "tests/fixtures/mysql_business.sql",
        "database": {"host": settings.host, "port": settings.port,
                     "database": settings.database, "user": settings.user},
        "oracle": {"alice": ALICE_ROWS, "bob": BOB_ROWS}, "checks": list(probe.checks.values()),
        "runs": probe.runs, "passed": passed, "planned": len(selected),
        "failed": sum(item["status"] == "failed" for item in selected),
        "not_run": sum(item["status"] == "not_run" for item in selected),
        "server_stopped": process is None or process.poll() is not None,
        "limitations": ["No database writes, root, permission edits, or query cancellation claim.",
                        "Knowledge lives in the project store with this history's unique scope.",
                        "Only this probe's server is stopped; private evidence is retained."],
    }
    save_private(output / "report.json", report)
    for path in output.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    print(json.dumps({"report": str(output / "report.json"), "passed": passed,
                      "planned": len(selected), "failed": report["failed"],
                      "not_run": report["not_run"]}), flush=True)
    return 0 if passed == len(selected) and report["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
