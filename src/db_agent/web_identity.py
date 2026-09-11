"""Local, operator-managed Web identities. No browser-supplied authorization."""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from db_agent.config import ConfigurationError, _validate_identifier

COOKIE = "db_agent_session"
SESSION_SECONDS = 3600
MODEL_BOUNDARY = (
    "仅用于本机隔离合成环境。智能查询会把你提交的问题、成功查询的历史原始请求、"
    "显式引用的已确认知识、模型授权表结构及 SQL/计划摘要发送给配置模型；"
    "结果行不发送给模型。请勿输入不允许该模型使用的内容。SQL 查询和诊断不调用模型。"
)


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度须为 12–128 字符。")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt-v1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    _, salt, expected = encoded.split("$")
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32,
    )
    return hmac.compare_digest(actual, bytes.fromhex(expected))


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    username: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    display_name: str = Field(min_length=1, max_length=64)
    password_hash: str = Field(pattern=r"^scrypt-v1\$[a-f0-9]{32}\$[a-f0-9]{64}$")
    enabled: bool = True
    allowed_tables: list[str] = Field(max_length=100)
    change_targets: list[Literal["local_inventory"]] = Field(default_factory=list, max_length=1)
    change_approve: bool = False
    model_enabled: bool = False
    model_tables: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("allowed_tables", "model_tables")
    @classmethod
    def table_names(cls, values):
        for value in values:
            _validate_identifier(value)
        if len(set(values)) != len(values):
            raise ValueError("duplicate tables")
        return sorted(values)

    def public(self):
        return self.model_dump(exclude={"password_hash", "enabled"})


class IdentityFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    version: int = Field(ge=1, le=1)
    users: list[Identity] = Field(min_length=1, max_length=32)


def read_identities(path: Path, allowed_tables: tuple[str, ...]) -> IdentityFile:
    """Read only the explicitly configured private regular file; fail closed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError("private owner-only file required")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("too large")
        data = IdentityFile.model_validate(json.loads(raw))
        names = [user.username for user in data.users]
        if len(set(names)) != len(names):
            raise ValueError("duplicate users")
        for user in data.users:
            if not set(user.allowed_tables) <= set(allowed_tables):
                raise ValueError("identity cannot extend database allowlist")
            if not set(user.model_tables) <= set(user.allowed_tables):
                raise ValueError("model cannot extend identity allowlist")
            if not user.model_enabled and user.model_tables:
                raise ValueError("disabled model cannot have tables")
        return data
    except (OSError, ValueError, ValidationError):
        raise ConfigurationError(
            "Web 身份配置无效：请检查指定的私有 JSON 文件、600 权限及表范围。"
        ) from None


@dataclass(frozen=True)
class LoginSession:
    username: str
    generation: str
    expires: float
    key: str


class Sessions:
    def __init__(self):
        self.values: dict[str, LoginSession] = {}
        self.attempts: list[float] = []
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))

    def get(self, token: str | None, generation: str) -> LoginSession | None:
        if not token or not re.fullmatch(r"[a-zA-Z0-9_-]{43}", token):
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        session = self.values.get(key)
        if session and session.generation == generation and session.expires > time.monotonic():
            return session
        self.values.pop(key, None)
        return None

    def valid(self, session: LoginSession | None, generation: str) -> bool:
        return bool(session and session.generation == generation
                    and self.values.get(session.key) == session
                    and session.expires > time.monotonic())

    def login(self, username: str, password: str, users: list[Identity], generation: str):
        stamp = time.monotonic()
        self.attempts = [item for item in self.attempts if stamp - item < 60]
        if len(self.attempts) >= 10:
            return None, "LOGIN_RATE_LIMIT"
        self.attempts.append(stamp)
        user = next((item for item in users if item.username == username), None)
        valid = verify_password(password, user.password_hash if user else self.dummy_hash)
        if not user or not user.enabled or not valid:
            return None, "LOGIN_FAILED"
        self.values = {key: item for key, item in self.values.items() if item.expires > stamp}
        if len(self.values) >= 128:
            return None, "LOGIN_RATE_LIMIT"
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).hexdigest()
        self.values[key] = LoginSession(username, generation, stamp + SESSION_SECONDS, key)
        return token, None
