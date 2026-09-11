"""Real Chromium -> authenticated Web -> independent MySQL, no model or HTTP doubles."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pymysql

from db_agent.changes import load_change_target
from db_agent.web_identity import password_hash

ROOT = Path(__file__).resolve().parents[1]
WORKER = """
import json,socket,sys,uvicorn
from pathlib import Path
from db_agent import web
from db_agent.config import DatabaseSettings
settings=DatabaseSettings(_env_file=None,**json.loads(Path(sys.argv[4]).read_text()))
web.load_database_settings=lambda: settings
app=web.create_app(store_path=Path(sys.argv[1]),identity_path=Path(sys.argv[2]))
server=uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False))
server.run(sockets=[socket.socket(fileno=int(sys.argv[3]))])
"""


def save(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="真实页面与独立合成MySQL受控变更验收")
    parser.add_argument("--run", action="store_true", required=True)
    parser.parse_args()
    target = load_change_target(ROOT / "outputs/changes/target.json")
    if target is None:
        parser.error("先显式创建独立受控变更目标")
    reader = ROOT / "outputs/changes/reader.json"
    output = ROOT / "outputs/changes/acceptance" / uuid4().hex
    output.mkdir(parents=True, mode=0o700)
    passwords = {user: secrets.token_urlsafe(24) for user in ("alice", "bob")}
    users = {
        "version": 1,
        "users": [
            {
                "username": user,
                "display_name": user.title(),
                "password_hash": password_hash(password),
                "allowed_tables": ["inventory"],
                "change_targets": ["local_inventory"] if user == "alice" else [],
                "change_approve": user == "alice",
                "model_enabled": False,
            }
            for user, password in passwords.items()
        ],
    }
    save(output / "users.json", users)
    database = pymysql.connect(
        host=target.host,
        port=target.port,
        database=target.database,
        user=target.user,
        password=target.password.get_secret_value(),
        autocommit=True,
        connect_timeout=3,
        read_timeout=5,
    )
    process = None
    report = {
        "version": "changes-browser-v1",
        "status": "failed",
        "model_calls": 0,
        "target": target.public(),
        "fault_injection": False,
    }
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT quantity,version FROM inventory WHERE id=1")
            before = cursor.fetchone()
        with socket.socket() as listener, (output / "server.log").open("w") as log:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    WORKER,
                    str(output / "history.sqlite3"),
                    str(output / "users.json"),
                    str(listener.fileno()),
                    str(reader),
                ],
                cwd=ROOT,
                pass_fds=(listener.fileno(),),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            with httpx.Client(trust_env=False, timeout=2) as client:
                for _ in range(100):
                    try:
                        if (
                            client.get(
                                base_url + "/api/auth/session", headers={"X-DB-Agent-Client": "web"}
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("real web server unavailable")
            result = subprocess.run(
                ["node", str(ROOT / "frontend/scripts/evaluate-changes.mjs")],
                input=json.dumps(
                    {
                        "base_url": base_url,
                        "credentials": passwords,
                        "output_dir": str(output),
                        "before": list(before),
                    }
                ),
                text=True,
                capture_output=True,
                cwd=ROOT / "frontend",
                timeout=120,
            )
            browser = json.loads(result.stdout)
            report["browser"] = browser
            assert result.returncode == 0 and browser["passed"] == browser["planned"]
        with database.cursor() as cursor:
            cursor.execute("SELECT quantity,version FROM inventory WHERE id=1")
            after = cursor.fetchone()
            assert after == (before[0], before[1] + 2)
            receipts = []
            for identifier in browser["change_ids"]:
                cursor.execute(
                    "SELECT change_id,item_id,before_quantity,after_quantity,"
                    "before_version,after_version FROM change_receipts WHERE change_id=%s",
                    (identifier,),
                )
                receipts.append(cursor.fetchone())
            assert len(receipts) == 2 and all(receipts)
            assert receipts[0][1:] == (
                1,
                before[0],
                (before[0] + 3) % 1000001,
                before[1],
                before[1] + 1,
            )
            assert receipts[1][1:] == (
                1,
                (before[0] + 3) % 1000001,
                before[0],
                before[1] + 1,
                before[1] + 2,
            )
        report.update(status="passed", before=before, after=after, receipts=receipts)
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        database.close()
        report["sources"] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                *(ROOT / "src/db_agent").glob("*.py"),
                ROOT / "frontend/src/ChangesPanel.tsx",
            ]
        }
        save(output / "report.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output / "report.json"),
                "browser": report.get("browser", {}),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
