"""Local Web conversation history; separate from redacted run logs and authorization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat()


def source_scope(settings) -> str:
    payload = [
        settings.kind,
        getattr(settings, "schema_name", None),
        settings.host,
        settings.port,
        settings.database,
        settings.user,
        sorted(settings.allowed_tables),
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, scope TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    mode TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, context_eligible INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(conversation_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS runs_conversation ON runs(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS conversations_updated ON conversations(updated_at);
            """)
            # A previous process cannot still own tasks in this store. Do not replay them.
            for row in db.execute(
                "SELECT id, payload FROM runs WHERE status IN ('running','cancelling')"
            ):
                payload = json.loads(row["payload"])
                payload.update(
                    status="interrupted",
                    error={
                        "code": "PROCESS_RESTARTED",
                        "message": "服务已重启，本次运行中断；未确认数据库语句最终状态。",
                    },
                )
                db.execute(
                    "UPDATE runs SET status=?, payload=? WHERE id=?",
                    ("interrupted", json.dumps(payload, ensure_ascii=False), row["id"]),
                )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, scope: str) -> dict:
        stamp = now()
        result = dict(id=uuid4().hex, title="新建对话", created_at=stamp, updated_at=stamp)
        with self.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM conversations WHERE scope=?", (scope,)
            ).fetchone()[0]
            if count >= 100:
                raise ValueError("当前数据源范围已达到 100 个会话，请删除不再需要的历史后新建。")
            db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)", (*result.values(), scope)
            )
        return result

    def list(self, scope: str) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT c.id, c.title, c.created_at, c.updated_at, "
                    "(SELECT id FROM runs WHERE conversation_id=c.id AND status='running' LIMIT 1) "
                    "AS active_run_id FROM conversations c WHERE scope=? ORDER BY updated_at DESC",
                    (scope,),
                )
            ]

    def get(self, conversation_id: str, scope: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT id,title,created_at,updated_at FROM conversations WHERE id=? AND scope=?",
                (conversation_id, scope),
            ).fetchone()
            if row is None:
                return None
            runs = [
                json.loads(item[0])
                for item in db.execute(
                    "SELECT payload FROM runs WHERE conversation_id=? ORDER BY created_at, rowid",
                    (conversation_id,),
                )
            ]
            return {**dict(row), "runs": runs}

    def rename(self, conversation_id: str, title: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, now(), conversation_id),
            )

    def delete(self, conversation_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def get_run(self, run_id: str, scope: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT r.payload FROM runs r JOIN conversations c "
                "ON c.id=r.conversation_id WHERE r.id=? AND c.scope=?",
                (run_id, scope),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def request_run(self, conversation_id: str, request_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM runs WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def history(self, conversation_id: str) -> list[str]:
        with self.connection() as db:
            rows = list(
                db.execute(
                    "SELECT prompt,context_eligible,status FROM runs "
                    "WHERE conversation_id=? AND mode='chat' ORDER BY created_at, rowid",
                    (conversation_id,),
                )
            )
            chain = []
            for row in rows:
                if row["status"] in {"running", "cancelling"}:
                    continue
                if row["status"] == "completed" and row["context_eligible"]:
                    chain.append(row["prompt"])
                else:
                    raise ValueError(
                        "上轮未取得完整查询证据，连续口径已暂停。请新建对话并完整重述需求。"
                    )
            return chain

    def context_state(self, conversation_id: str) -> dict:
        try:
            return {"context_turns": len(self.history(conversation_id)), "context_paused": False}
        except ValueError:
            return {"context_turns": 0, "context_paused": True}

    def add_run(self, run: dict) -> None:
        with self.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM runs WHERE conversation_id=?", (run["conversation_id"],)
            ).fetchone()[0]
            if count >= 100:
                raise ValueError("本会话已达到 100 条请求，请新建对话。")
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    run["id"],
                    run["conversation_id"],
                    run["request_id"],
                    run["created_at"],
                    run["mode"],
                    run["prompt"],
                    run["status"],
                    json.dumps(run, ensure_ascii=False),
                ),
            )
            db.execute(
                "UPDATE conversations SET updated_at=?, "
                "title=CASE WHEN title='新建对话' THEN ? ELSE title END WHERE id=?",
                (now(), run["prompt"].replace("\n", " ")[:36], run["conversation_id"]),
            )

    def update_run(self, run: dict, *, context_eligible: bool = False) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status=?, payload=?, context_eligible=? WHERE id=?",
                (run["status"], json.dumps(run, ensure_ascii=False), context_eligible, run["id"]),
            )
