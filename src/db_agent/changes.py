"""Explicit human changes to one local synthetic inventory row; no model tools."""

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pymysql import MySQLError

from db_agent.config import ConfigurationError
from db_agent.conversations import ConversationStore
from db_agent.db import DatabaseError

TARGET = "local_inventory"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class ChangeTarget(StrictModel):
    kind: Literal["mysql"] = "mysql"
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[13316] = 13316
    database: Literal["db_agent_changes"] = "db_agent_changes"
    user: Literal["db_agent_changer"] = "db_agent_changer"
    password: SecretStr
    server_uuid: str = Field(pattern=r"^[a-f0-9-]{36}$")
    schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    def fingerprint(self):
        return digest(
            {
                **self.model_dump(mode="json", exclude={"password"}),
                "credential_revision": digest(self.password.get_secret_value()),
            }
        )

    def public(self):
        return {
            "id": TARGET,
            "kind": "mysql",
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "table": "inventory",
            "column": "quantity",
            "max_rows": 1,
            "max_quantity": 1000000,
        }


def load_change_target(path=Path("outputs/changes/target.json")):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("private file required")
            raw = file.read(8193)
        if len(raw) > 8192:
            raise ValueError("large config")
        return ChangeTarget.model_validate(json.loads(raw))
    except (OSError, ValueError):
        raise ConfigurationError("受控变更目标配置无效。") from None


class ChangeInput(StrictModel):
    target: Literal["local_inventory"]
    item_id: int = Field(ge=1, le=1000000000)
    quantity: int = Field(ge=0, le=1000000)
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")


class ApprovalInput(StrictModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class EmptyInput(StrictModel):
    pass


class RecoveryInput(StrictModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")


class Row(StrictModel):
    quantity: int = Field(ge=0, le=1000000)
    version: int = Field(ge=1, le=2147483646)


class Plan(StrictModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner: str
    scope: str
    target: Literal["local_inventory"]
    target_fingerprint: str
    request_id: str
    item_id: int = Field(ge=1, le=1000000000)
    before: Row
    after: Row
    created_at: float
    expires_at: float
    recovery_of: str | None = None


class ChangeStore:
    def __init__(self, store: ConversationStore):
        self.store = store
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                "request_id TEXT NOT NULL, plan TEXT NOT NULL, digest TEXT NOT NULL, "
                "status TEXT NOT NULL, evidence TEXT, approval TEXT, UNIQUE(scope,request_id))"
            )
            db.execute("UPDATE changes SET status='unknown' WHERE status='executing'")

    def get(self, scope, identifier=None, request_id=None):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM changes WHERE scope=? AND "
                + ("id=?" if identifier else "request_id=?"),
                (scope, identifier or request_id),
            ).fetchone()
        if row is None:
            return None
        plan = Plan.model_validate(json.loads(row["plan"]))
        if (
            digest(plan.model_dump()) != row["digest"]
            or plan.scope != scope
            or plan.id != row["id"]
        ):
            raise ValueError("变更记录完整性核对失败。")
        return {
            "plan": plan.model_dump(),
            "digest": row["digest"],
            "status": row["status"],
            "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
            "approval": json.loads(row["approval"]) if row["approval"] else None,
        }

    def list(self, scope):
        with self.store.connection() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM changes WHERE scope=? ORDER BY rowid DESC LIMIT 100", (scope,)
                )
            ]
        return [self.get(scope, identifier=i) for i in ids]

    def create(self, plan):
        body = plan.model_dump()
        with self.store.connection() as db:
            if (
                db.execute("SELECT COUNT(*) FROM changes WHERE scope=?", (plan.scope,)).fetchone()[
                    0
                ]
                >= 100
            ):
                raise ValueError("当前授权范围最多保存100笔变更。")
            db.execute(
                "INSERT INTO changes VALUES (?,?,?,?,?,'preview',NULL,NULL)",
                (plan.id, plan.scope, plan.request_id, json.dumps(body), digest(body)),
            )
        return self.get(plan.scope, plan.id)

    def transition(self, item, expected, status, evidence=None):
        with self.store.connection() as db:
            changed = db.execute(
                "UPDATE changes SET status=?,evidence=? WHERE id=? AND scope=? "
                "AND digest=? AND status=?",
                (
                    status,
                    json.dumps(evidence),
                    item["plan"]["id"],
                    item["plan"]["scope"],
                    item["digest"],
                    expected,
                ),
            )
            if status == "approved":
                approval = {
                    "digest": item["digest"],
                    "owner": item["plan"]["owner"],
                    "scope": item["plan"]["scope"],
                    "approved_at": time.time(),
                }
                db.execute(
                    "UPDATE changes SET approval=? WHERE id=?",
                    (json.dumps(approval), item["plan"]["id"]),
                )
            if changed.rowcount != 1:
                raise ValueError("变更状态已变化，请重新取回记录。")
        return self.get(item["plan"]["scope"], item["plan"]["id"])


class ChangeService:
    def __init__(self, web):
        self.web = web
        self.store = ChangeStore(web.store)
        self.lock = asyncio.Lock()
        self.pending = {}

    @asynccontextmanager
    async def operation(self):
        from db_agent.web import _session

        self.context()
        task = asyncio.current_task()
        if len(self.pending) >= 8:
            raise DatabaseError("CHANGE_BUSY", "变更请求过多，请稍后取回状态。")
        self.pending[task] = _session.get()
        try:
            async with asyncio.timeout(15), self.lock:
                self.context()
                yield
        except (OSError, MySQLError, TimeoutError):
            raise DatabaseError(
                "CHANGE_UNAVAILABLE", "变更取证未完成；已停止派发，请取回记录并核对提交状态。"
            ) from None
        finally:
            self.pending.pop(task, None)

    def context(self, approval=False):
        self.web.authorize()
        from db_agent.web import _session

        session = _session.get()
        identity = self.web.identity(session)
        target = self.web.change_target
        if (
            getattr(self.web.db_settings, "kind", "mysql") != "mysql"
            or not target
            or TARGET not in identity.change_targets
            or (approval and not identity.change_approve)
        ):
            raise DatabaseError(
                "CHANGE_PERMISSION_DENIED", "当前身份或数据源未获准使用此变更目标。"
            )
        scope = digest([session.username, session.generation, target.fingerprint()])
        return identity, target, scope

    def check(self, item, *, approval=False, fresh=False):
        identity, target, scope = self.context(approval)
        plan = Plan.model_validate(item["plan"])
        if (
            plan.owner != identity.username
            or plan.scope != scope
            or plan.target_fingerprint != target.fingerprint()
            or digest(plan.model_dump()) != item["digest"]
            or plan.after.version != plan.before.version + 1
            or plan.before.quantity == plan.after.quantity
        ):
            raise DatabaseError("CHANGE_BINDING", "变更目标、授权或内容不一致。")
        if fresh and time.time() >= plan.expires_at:
            raise DatabaseError("CHANGE_EXPIRED", "预览和审批已过期，请重新预览。")
        if fresh and plan.recovery_of:
            original = self.store.get(scope, plan.recovery_of)
            proof = original and original["evidence"]
            if (
                not original
                or original["status"] != "committed"
                or not proof
                or proof.get("outcome") != "committed"
                or not proof.get("receipt_verified")
                or plan.before.model_dump() != original["plan"]["after"]
                or plan.after.quantity != original["plan"]["before"]["quantity"]
            ):
                raise DatabaseError("CHANGE_RECOVERY", "恢复依据已失效，请先核对原变更。")
        return target

    def get(self, identifier):
        _, _, scope = self.context()
        item = self.store.get(scope, identifier)
        if not item:
            raise DatabaseError("CHANGE_NOT_FOUND", "变更不存在或不属于当前授权范围。")
        self.check(item)
        return item

    def connector(self, guard):
        from db_agent.changes_db import ChangeConnector

        return ChangeConnector(self.web.change_target, guard)

    async def preview(self, payload, recovery_of=None):
        async with self.operation():
            identity, target, scope = self.context()
            existing = self.store.get(scope, request_id=payload.request_id)
            if existing:
                p = existing["plan"]
                if (
                    p["item_id"] != payload.item_id
                    or p["after"]["quantity"] != payload.quantity
                    or p["recovery_of"] != recovery_of
                ):
                    raise ValueError("请求标识已绑定其他内容。")
                return existing
            before = await self.connector(lambda: self.context()).read(payload.item_id)
            if recovery_of:
                original = self.get(recovery_of)
                if (
                    original["status"] != "committed"
                    or not original["evidence"]
                    or original["evidence"].get("outcome") != "committed"
                    or not original["evidence"].get("receipt_verified")
                    or before != original["plan"]["after"]
                ):
                    raise DatabaseError(
                        "CHANGE_DRIFT", "原变更未确认提交或当前前值已变化，不能恢复。"
                    )
            if before["quantity"] == payload.quantity:
                raise ValueError("新值与当前值相同，无需变更。")
            self.context()
            stamp = time.time()
            return self.store.create(
                Plan(
                    id=uuid4().hex,
                    owner=identity.username,
                    scope=scope,
                    target=TARGET,
                    target_fingerprint=target.fingerprint(),
                    request_id=payload.request_id,
                    item_id=payload.item_id,
                    before=Row(**before),
                    after=Row(quantity=payload.quantity, version=before["version"] + 1),
                    created_at=stamp,
                    expires_at=stamp + 300,
                    recovery_of=recovery_of,
                )
            )

    async def approve(self, identifier, approval):
        async with self.operation():
            item = self.get(identifier)
            self.check(item, approval=True, fresh=True)
            if approval.digest != item["digest"]:
                raise ValueError("审批摘要不一致，请重新审阅预览。")
            if item["status"] == "approved":
                return item
            return self.store.transition(item, "preview", "approved")

    def authorize_execution(self, claimed):
        self.check(claimed, approval=True, fresh=True)
        persisted = self.store.get(claimed["plan"]["scope"], claimed["plan"]["id"])
        proof = persisted and persisted["approval"]
        if (
            not persisted
            or persisted["status"] != "executing"
            or persisted["digest"] != claimed["digest"]
            or not proof
            or proof.get("digest") != claimed["digest"]
            or proof.get("owner") != claimed["plan"]["owner"]
            or proof.get("scope") != claimed["plan"]["scope"]
        ):
            raise DatabaseError("CHANGE_APPROVAL", "审批或执行凭证不可用，已停止。")

    async def execute(self, identifier):
        async with self.operation():
            item = self.get(identifier)
            self.check(item, approval=True)
            if item["status"] != "approved":
                # Retries never dispatch again; executing is also an uncertain outcome.
                return item
            self.check(item, approval=True, fresh=True)
            claimed = self.store.transition(item, "approved", "executing")
            try:
                evidence = await self.connector(
                    lambda: self.authorize_execution(claimed),
                ).execute(claimed)
            except BaseException:
                try:
                    self.store.transition(claimed, "executing", "unknown")
                except (OSError, sqlite3.Error):
                    pass  # Durable executing remains uncertain; never dispatch it again.
                raise
            return self.store.transition(claimed, "executing", evidence["outcome"], evidence)

    async def reconcile(self, identifier):
        async with self.operation():
            item = self.get(identifier)
            if item["status"] not in {"unknown", "executing", "committed"}:
                return item
            evidence = await self.connector(lambda: self.check(item)).reconcile(item)
            status = "committed" if item["status"] == "committed" else evidence["outcome"]
            return self.store.transition(item, item["status"], status, evidence)

    async def recover(self, identifier, payload):
        item = self.get(identifier)
        return await self.preview(
            ChangeInput(
                target=TARGET,
                item_id=item["plan"]["item_id"],
                quantity=item["plan"]["before"]["quantity"],
                request_id=payload.request_id,
            ),
            recovery_of=identifier,
        )
