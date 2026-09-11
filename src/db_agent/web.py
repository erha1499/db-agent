"""Single-process, loopback-only Web adapter for the existing guarded services."""

import asyncio
import fcntl
import hashlib
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from db_agent.agent import AgentResponseError, run_agent_observed
from db_agent.analysis import SqlAnalysisService
from db_agent.config import (
    ConfigurationError,
    load_analysis_settings,
    load_database_settings,
    load_query_settings,
    load_settings,
)
from db_agent.conversation_context import conversation_prompt
from db_agent.conversations import ConversationStore, now, source_scope
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.presentation import has_complete_query_results
from db_agent.query import QueryService
from db_agent.records import RunRecord
from db_agent.result_delivery import (
    SCOPE_NOTE,
    AnalysisInput,
    ExportInput,
    analyze_result,
    html_report,
    validate_result,
)
from db_agent.web_identity import (
    COOKIE,
    MODEL_BOUNDARY,
    SESSION_SECONDS,
    LoginSession,
    Sessions,
    read_identities,
)

_session: ContextVar[LoginSession | None] = ContextVar("web_session", default=None)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(strict=True, min_length=1, max_length=16384)
    request_id: str = Field(strict=True, pattern=r"^[a-zA-Z0-9_-]{16,64}$")
    mode: str = Field(default="chat", pattern=r"^(chat|analyze|query)$")


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(strict=True, min_length=1, max_length=32)
    password: str = Field(strict=True, min_length=1, max_length=128)


class TitleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(strict=True, min_length=1, max_length=80)


class WebRecord(RunRecord):
    """Expose the same finite, redacted events even when diagnostic logging fails."""

    def __init__(self, events: list[dict]):
        super().__init__()
        self.events = events

    def emit(self, event: str, **kwargs):
        super().emit(event, **kwargs)
        if len(self.events) < 128:
            self.events.append(
                {
                    "seq": len(self.events) + 1,
                    "time": now(),
                    "event": event,
                    **{
                        key: value
                        for key, value in kwargs.items()
                        if key
                        in {
                            "status",
                            "code",
                            "operation",
                            "duration_ms",
                            "decision",
                            "execution_status",
                            "row_count",
                            "truncated",
                        }
                        and value is not None
                    },
                }
            )


def _error(code: str, message: str, status: int = 400):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


class WebRuntime:
    def __init__(self, path: Path, *, service=None, identity=None):
        self.store = service.store if service else ConversationStore(path)
        self.service = service
        self.identity = identity
        self.tasks: dict[str, asyncio.Task] = {}
        self.run_sessions: dict[str, LoginSession] = {}
        self.live: dict[str, dict] = {}
        self.unsaved_conversations: set[str] = set()
        self.connector = None
        self.database_error = None
        self.model_error = None
        self.settings = None
        try:
            db_settings = service.db_settings if service else load_database_settings()
            if identity:
                db_settings = db_settings.model_copy(update={"allowed_tables":
                                                            tuple(identity.allowed_tables)})
            guard = service.authorize if service else None
            self.connector = MetadataConnector(db_settings, authorization_check=guard)
            self.scope = source_scope(db_settings)
            if service:
                self.scope = hashlib.sha256(json.dumps([
                    self.scope, identity.username, service.generation,
                ]).encode()).hexdigest()
            self.model_connector = MetadataConnector(
                db_settings.model_copy(update={"allowed_tables": tuple(identity.model_tables)})
                if identity else db_settings, authorization_check=guard,
            )
            self.analysis = load_analysis_settings()
            self.query = load_query_settings()
        except ConfigurationError:
            self.connector = None
            self.scope = "unconfigured"
            self.database_error = "请先在项目 .env 中完成数据库配置，再重启 Web 服务。"
        try:
            self.settings = load_settings()
        except ConfigurationError:
            self.model_error = "请先在项目 .env 中完成模型配置，再重启 Web 服务。"

    def conversation(self, conversation_id):
        value = self.store.get(conversation_id, self.scope)
        if value is None:
            raise HTTPException(404, "会话不存在或不属于当前数据源配置。")
        value["runs"] = [self.live.get(run["id"], run) for run in value["runs"]]
        value.update(self.store.context_state(conversation_id))
        return value

    def run(self, run_id):
        # The runtime and its live map belong to one trusted principal/generation.
        # A broken history disk must not prevent that principal cancelling a task.
        if run_id in self.live:
            return self.live[run_id]
        value = self.store.get_run(run_id, self.scope)
        if value is None:
            raise HTTPException(404, "运行记录不存在。")
        return self.live.get(run_id, value)

    def result_snapshot(self, conversation_id, run_id, result_id, selection=None):
        # Only persisted evidence under the server's source scope is deliverable.
        # IDs select data; none grants access or authorizes a database operation.
        if not all(re.fullmatch(r"[a-f0-9]{32}", value)
                   for value in (conversation_id, run_id, result_id)):
            raise HTTPException(404, "结果不存在或不属于当前会话。")
        run = self.store.get_run(run_id, self.scope)
        if run is None or run["conversation_id"] != conversation_id:
            raise HTTPException(404, "结果不存在或不属于当前会话。")
        if run["status"] != "completed" or conversation_id in self.unsaved_conversations:
            raise HTTPException(409, "运行未完成或历史未保存，无法交付结果。")
        matches = [q for q in run["queries"] if q["report"].get("result_id") == result_id]
        if len(matches) != 1:
            raise HTTPException(404, "结果不存在或标识不唯一。")
        query = matches[0]
        data = validate_result(query["report"])
        notes = [
            SCOPE_NOTE,
            "结果已截断，只分析已返回部分，不能推断原查询总量；服务器语句状态未确认。"
            if data["truncated"] else "当前 SQL 的结果已完整返回，不表示整个数据库的完整数据。",
            "此文件是保存时的结果快照，取回与分析不会重新查询数据库，也不会调用模型。",
            "DATETIME 无时区；查询会话为 +00:00 时 TIMESTAMP 按 UTC 返回。",
        ]
        if run.get("missing_query_reports"):
            notes.append("本轮还有查询调用缺少报告；本文件只包含此结果，不能代表整轮任务完成。")
        return {
            "version": "db-agent-result-v1", "conversation_id": conversation_id,
            "run_id": run_id, "result_id": result_id, "finished_at": run["finished_at"],
            "prompt": run["prompt"], "sql": query["sql"], "report": query["report"],
            "notes": notes,
            "analysis": analyze_result(data, selection) if selection else None,
        }

    def task_done(self, run: dict, task: asyncio.Task):
        # A task cancelled before its coroutine starts never enters execute's finally.
        if not task.cancelled():
            task.exception()
        if run["status"] in {"running", "cancelling"}:
            run.update(
                status="cancelled" if task.cancelled() else "failed",
                finished_at=now(),
                error={
                    "code": "CANCELLED" if task.cancelled() else "RUN_FAILED",
                    "message": "运行已停止；未确认数据库语句最终状态。",
                },
            )
            try:
                self.store.update_run(run)
            except (OSError, sqlite3.Error):
                self.unsaved_conversations.add(run["conversation_id"])
                run["error"] = {
                    "code": "HISTORY_WRITE_FAILED",
                    "message": "运行已停止，但历史保存失败。",
                }
            else:
                self.live.pop(run["id"], None)
        self.tasks.pop(run["id"], None)
        self.run_sessions.pop(run["id"], None)

    async def execute(self, run: dict, previous: list[str]):
        eligible = False
        try:
            with WebRecord(run["events"]) as record:
                if self.service:
                    self.service.authorize()
                if run["mode"] == "query":
                    report = await QueryService(
                        self.connector, self.analysis, self.query, record,
                    ).execute(run["prompt"])
                    run["queries"] = [{"sql": run["prompt"], "report": report}]
                    run["answer"] = (
                        "已取得 SQL 查询结果，请核对执行状态与结果范围。"
                        if report["status"] == "ok" else "此次 SQL 查询未取得结果，请查看报告。"
                    )
                elif run["mode"] == "analyze":
                    report = await SqlAnalysisService(
                        self.connector,
                        self.analysis,
                        record,
                    ).analyze(run["prompt"])
                    run["analyses"] = [{"sql": run["prompt"], "report": report}]
                    run["answer"] = {
                        "ALLOW": "SQL 检查通过。此次仅完成诊断，未执行业务查询。",
                        "BLOCK": "SQL 未通过检查，未执行业务查询。",
                        "REVIEW": "执行计划触发风险阈值，未执行业务查询。",
                        "UNKNOWN": "当前证据不足以判断 SQL，未执行业务查询。",
                    }[report["decision"]]
                else:
                    result = await run_agent_observed(
                        run["prompt"],
                        self.settings,
                        self.model_connector,
                        record,
                        self.analysis,
                        self.query,
                        previous_requests=previous,
                    )
                    run.update(
                        answer=result.answer,
                        queries=[asdict(item) for item in result.queries],
                        analyses=[asdict(item) for item in result.analyses],
                        missing_query_reports=max(
                            0,
                            result.tool_calls.count("execute_query") - len(result.queries),
                        ),
                    )
                    eligible = has_complete_query_results(result)
                if self.service:
                    self.service.authorize()
                run["status"] = "completed"
        except asyncio.CancelledError:
            run.update(
                status="cancelled",
                error={
                    "code": "CANCELLED",
                    "message": "运行已停止；未确认数据库语句最终状态。",
                },
            )
            raise
        except AgentResponseError as exc:
            run.update(
                status="failed", error={"code": exc.code or "MODEL_ERROR", "message": str(exc)}
            )
            if exc.observation:
                run.update(
                    answer=exc.observation.answer,
                    queries=[asdict(item) for item in exc.observation.queries],
                    analyses=[asdict(item) for item in exc.observation.analyses],
                    missing_query_reports=max(
                        0,
                        exc.observation.tool_calls.count("execute_query")
                        - len(exc.observation.queries),
                    ),
                )
        except Exception:
            run.update(
                status="failed",
                error={
                    "code": "RUN_FAILED",
                    "message": "运行失败，请检查模型与数据库服务后重新发送。",
                },
            )
        finally:
            run["finished_at"] = now()
            try:
                self.store.update_run(run, context_eligible=eligible)
            except (OSError, sqlite3.Error):
                self.unsaved_conversations.add(run["conversation_id"])
                run.update(
                    status="failed",
                    error={
                        "code": "HISTORY_WRITE_FAILED",
                        "message": "本次运行历史保存失败，请保留当前结果并检查本机存储。",
                    },
                )
            else:
                self.live.pop(run["id"], None)


class WebService:
    """One process, many trusted principals; global run budget remains one."""

    def __init__(self, store_path: Path, identity_path: Path):
        self.store = ConversationStore(store_path)
        self.identity_path = identity_path
        self.sessions = Sessions()
        self.runtimes: dict[str, WebRuntime] = {}
        self.generation = ""
        self.fingerprint = ""
        self.error = None
        with self.store.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS web_policy_state "
                       "(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT, generation TEXT)")
        self.refresh()

    def refresh(self):
        try:
            db_settings = load_database_settings()
            identities = read_identities(self.identity_path, db_settings.allowed_tables)
            try:
                settings = load_settings()
                model = settings.model_dump(mode="json", exclude={"api_key"})
            except ConfigurationError:
                model = None
            fingerprint = hashlib.sha256(json.dumps([
                identities.model_dump(mode="json"),
                db_settings.model_dump(mode="json", exclude={"password"}), model,
            ], sort_keys=True).encode()).hexdigest()
            error = None
        except ConfigurationError:
            fingerprint = "invalid"
            error = "Web 接入配置无效，请管理员检查身份文件与数据库配置。"
        if fingerprint != self.fingerprint:
            # A fresh durable generation on every observed change prevents restoring
            # an older config from resurrecting history, knowledge, or sessions.
            self.sessions.values.clear()
            for runtime in self.runtimes.values():
                for task in runtime.tasks.values():
                    task.cancel()
            with self.store.connection() as db:
                row = db.execute("SELECT fingerprint,generation FROM web_policy_state "
                                 "WHERE id=1").fetchone()
                generation = row["generation"] if row and row["fingerprint"] == fingerprint \
                    else uuid4().hex
                db.execute("INSERT OR REPLACE INTO web_policy_state VALUES (1,?,?)",
                           (fingerprint, generation))
            self.generation = generation
            self.fingerprint = fingerprint
        self.error = error
        if error:
            raise ConfigurationError(error)
        self.db_settings = db_settings
        self.identities = identities

    def authorize(self):
        try:
            self.refresh()
        except (ConfigurationError, OSError, sqlite3.Error):
            raise DatabaseError("PERMISSION_DENIED", "身份配置失效，操作已停止。") from None
        if not self.sessions.valid(_session.get(), self.generation):
            raise DatabaseError("PERMISSION_DENIED", "登录或授权已失效，请重新登录。")

    def identity(self, session):
        return next(user for user in self.identities.users if user.username == session.username)

    def runtime(self, session):
        key = f"{self.generation}:{session.username}"
        if key not in self.runtimes:
            self.runtimes[key] = WebRuntime(
                self.store.path, service=self, identity=self.identity(session),
            )
        return self.runtimes[key]

    def busy(self):
        return any(runtime.tasks for runtime in self.runtimes.values())

    async def watch(self):
        while True:
            await asyncio.sleep(0.2)
            try:
                self.refresh()
                valid = True
            except (ConfigurationError, OSError, sqlite3.Error):
                valid = False
                self.sessions.values.clear()
            for key, runtime in list(self.runtimes.items()):
                for run_id, task in list(runtime.tasks.items()):
                    if not valid or not self.sessions.valid(
                        runtime.run_sessions.get(run_id), self.generation,
                    ):
                        task.cancel()
                if not runtime.tasks and not key.startswith(self.generation + ":"):
                    self.runtimes.pop(key, None)


def create_app(
    *, store_path: Path | None = None, static_dir: Path | None = None,
    identity_path: Path | None = None,
) -> FastAPI:
    store_path = store_path or Path("outputs/web/conversations.sqlite3")
    static_dir = static_dir or Path(__file__).resolve().parents[2] / "frontend/dist"
    identity_path = identity_path or Path("outputs/web/identities.json")

    @asynccontextmanager
    async def lifespan(app):
        store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The SQLite history and task registry must have one process owner.
        with store_path.with_suffix(".lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("该 Web 历史已由另一个进程使用，请停止旧服务。") from None
            service = WebService(store_path, identity_path)
            app.state.service = service
            watcher = asyncio.create_task(service.watch())
            try:
                yield
            finally:
                tasks = [task for runtime in service.runtimes.values()
                         for task in runtime.tasks.values()]
                tasks.append(watcher)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        session = None
        protected = request.url.path.startswith("/api") and request.url.path not in {
            "/api/auth/session", "/api/auth/login", "/api/auth/logout",
        }
        try:
            hostname = urlsplit("http://" + request.headers.get("host", "")).hostname
        except ValueError:
            hostname = None
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            return _error("INVALID_HOST", "仅支持本机访问。", 403)
        if request.url.path.startswith("/api"):
            origin = request.headers.get("origin")
            if (
                request.headers.get("x-db-agent-client") != "web"
                or request.headers.get("sec-fetch-site") == "cross-site"
                or (origin and origin != str(request.base_url).rstrip("/"))
            ):
                return _error("INVALID_ORIGIN", "请从本机 DB Agent 页面访问。", 403)
            if request.method in {"POST", "PATCH"} and (
                request.headers.get("content-type", "").split(";")[0] != "application/json"
            ):
                return _error("INVALID_CONTENT_TYPE", "请求必须使用 JSON。", 415)
            # Read a bounded body before validation; never echo invalid inputs.
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > 100000:
                    return _error("REQUEST_TOO_LARGE", "请求内容过长。", 413)
            request._body = bytes(body)
            service = request.app.state.service
            try:
                service.refresh()
            except (ConfigurationError, OSError, sqlite3.Error):
                return _error("ACCESS_CONFIGURATION", "Web 接入配置不可用，请联系本机管理员。", 503)
            session = service.sessions.get(request.cookies.get(COOKIE), service.generation)
            if protected and session is None:
                return _error("AUTH_REQUIRED", "登录已失效，请重新登录。", 401)
        context = _session.set(session)
        try:
            response = await call_next(request)
            if protected:
                try:
                    service.authorize()
                except DatabaseError:
                    return _error("AUTH_REQUIRED", "登录或授权已变更，请重新登录。", 401)
        finally:
            _session.reset(context)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return _error("INVALID_INPUT", "输入格式不正确或超出限制。", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return _error("REQUEST_REJECTED", str(exc.detail), exc.status_code)

    @app.exception_handler(DatabaseError)
    async def database_error(request, exc):
        return _error(exc.code, exc.message, 400)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return _error("STORAGE_ERROR", "无法读写本机会话历史，请检查存储后重试。", 503)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return _error("INPUT_LIMIT", str(exc), 422)

    def runtime(request):
        return request.app.state.service.runtime(_session.get())

    def session_response(service, session):
        if session is None:
            return {"authenticated": False, "model_boundary": MODEL_BOUNDARY}
        return {
            "authenticated": True, "session_id": session.key,
            "identity": {**service.identity(session).public(),
                         "authorization_version": service.generation},
            "model_boundary": MODEL_BOUNDARY,
        }

    @app.get("/api/auth/session")
    async def auth_session(request: Request):
        return session_response(request.app.state.service, _session.get())

    @app.post("/api/auth/login")
    async def login(payload: LoginInput, request: Request):
        service = request.app.state.service
        generation = service.generation
        token, code = await asyncio.to_thread(
            service.sessions.login, payload.username, payload.password,
            service.identities.users, generation,
        )
        service.refresh()
        if generation != service.generation:
            return _error("AUTH_REQUIRED", "授权配置已变更，请重新登录。", 401)
        if code:
            return _error(code, "登录请求过多，请一分钟后重试。" if code == "LOGIN_RATE_LIMIT"
                          else "用户名或密码不正确，或账号已停用。",
                          429 if code == "LOGIN_RATE_LIMIT" else 401)
        old = _session.get()
        if old:
            service.sessions.values.pop(old.key, None)
        session = service.sessions.get(token, generation)
        response = JSONResponse(session_response(service, session))
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, path="/api",
                            httponly=True, samesite="strict")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        service = request.app.state.service
        session = _session.get()
        if session:
            service.sessions.values.pop(session.key, None)
        response = JSONResponse(session_response(service, None))
        response.delete_cookie(COOKIE, path="/api", httponly=True, samesite="strict")
        return response

    @app.get("/api/status")
    async def status(request: Request):
        state = runtime(request)
        return {
            "database": state.connector.database if state.connector else None,
            "database_configured": state.connector is not None,
            "model_configured": state.settings is not None,
            "database_error": state.database_error,
            "model_error": state.model_error,
            "read_only": True,
            "active_run_id": next(iter(state.tasks), None),
            "identity": session_response(request.app.state.service, _session.get())["identity"],
            "model_boundary": MODEL_BOUNDARY,
        }

    @app.get("/api/conversations")
    async def conversations(request: Request):
        state = runtime(request)
        items = state.store.list(state.scope)
        for item in items:
            item["active_run_id"] = next(
                (
                    run_id
                    for run_id, run in state.live.items()
                    if run_id in state.tasks and run["conversation_id"] == item["id"]
                ),
                None,
            )
        return {"conversations": items}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(request: Request):
        state = runtime(request)
        return state.store.create(state.scope)

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str, request: Request):
        return runtime(request).conversation(conversation_id)

    @app.patch("/api/conversations/{conversation_id}")
    async def rename(conversation_id: str, payload: TitleInput, request: Request):
        state = runtime(request)
        state.conversation(conversation_id)
        if not payload.title.strip():
            raise HTTPException(422, "会话名称不能为空。")
        state.store.rename(conversation_id, payload.title.strip())
        return state.conversation(conversation_id)

    @app.delete("/api/conversations/{conversation_id}")
    async def delete(conversation_id: str, request: Request):
        state = runtime(request)
        item = state.conversation(conversation_id)
        if any(run["id"] in state.tasks for run in item["runs"]):
            raise HTTPException(409, "请先停止运行，再删除会话。")
        state.store.delete(conversation_id)
        for run in item["runs"]:
            state.live.pop(run["id"], None)
        state.unsaved_conversations.discard(conversation_id)
        return {"deleted": True}

    @app.post("/api/conversations/{conversation_id}/runs", status_code=202)
    async def start_run(conversation_id: str, payload: RunInput, request: Request):
        state = runtime(request)
        state.conversation(conversation_id)
        if conversation_id in state.unsaved_conversations:
            raise HTTPException(503, "本会话历史保存失败，请检查存储并重新启动服务。")
        previous = state.store.request_run(conversation_id, payload.request_id)
        if previous:
            if previous["prompt"] != payload.prompt or previous["mode"] != payload.mode:
                raise HTTPException(409, "同一请求标识不能用于不同内容。")
            return state.run(previous["id"])
        if request.app.state.service.busy():
            raise HTTPException(409, "已有任务正在运行，请等待完成或先停止它。")
        if not state.connector:
            raise HTTPException(503, state.database_error)
        if payload.mode == "chat" and state.settings is None:
            raise HTTPException(503, state.model_error)
        if payload.mode == "chat" and not state.identity.model_enabled:
            raise HTTPException(403, "当前身份未允许使用模型，请选择 SQL 查询或诊断。")
        if not payload.prompt.strip() or len(payload.prompt.encode()) > 16384:
            raise HTTPException(422, "请输入问题或 SQL，长度不能超过 16 KiB。")
        previous_requests = state.store.history(conversation_id) if payload.mode == "chat" else []
        if payload.mode == "chat":
            conversation_prompt(previous_requests, payload.prompt)
        run = dict(
            id=uuid4().hex,
            conversation_id=conversation_id,
            request_id=payload.request_id,
            created_at=now(),
            finished_at=None,
            mode=payload.mode,
            prompt=payload.prompt,
            status="running",
            answer=None,
            error=None,
            queries=[],
            analyses=[],
            events=[],
            missing_query_reports=0,
        )
        state.store.add_run(run)
        state.live[run["id"]] = run
        state.tasks[run["id"]] = asyncio.create_task(state.execute(run, previous_requests))
        state.run_sessions[run["id"]] = _session.get()
        state.tasks[run["id"]].add_done_callback(lambda task: state.task_done(run, task))
        return run

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str, request: Request):
        return runtime(request).run(run_id)

    result_path = "/api/conversations/{conversation_id}/runs/{run_id}/results/{result_id}"

    @app.get(result_path)
    async def saved_result(conversation_id: str, run_id: str, result_id: str, request: Request):
        if request.query_params:
            raise HTTPException(422, "结果取回不接受额外参数。")
        return runtime(request).result_snapshot(conversation_id, run_id, result_id)

    @app.post(result_path + "/analysis")
    async def result_analysis(
        conversation_id: str, run_id: str, result_id: str,
        payload: AnalysisInput, request: Request,
    ):
        if request.query_params:
            raise HTTPException(422, "分析不接受额外参数。")
        return runtime(request).result_snapshot(conversation_id, run_id, result_id, payload)

    @app.post(result_path + "/export")
    async def export_result(
        conversation_id: str, run_id: str, result_id: str,
        payload: ExportInput, request: Request,
    ):
        if request.query_params:
            raise HTTPException(422, "导出不接受额外参数。")
        snapshot = runtime(request).result_snapshot(
            conversation_id, run_id, result_id, payload.analysis,
        )
        content = (
            json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2)
            if payload.format == "json" else html_report(snapshot)
        )
        return Response(
            content, media_type="application/json" if payload.format == "json" else "text/html",
            headers={"Content-Disposition":
                     f'attachment; filename="db-agent-result-{result_id}.{payload.format}"'},
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        state = runtime(request)
        run = state.run(run_id)
        task = state.tasks.get(run_id)
        if task and not task.done() and run["status"] == "running":
            run["status"] = "cancelling"
            task.cancel()
            try:
                state.store.update_run(run)
            except (OSError, sqlite3.Error):
                # Actual cancellation must not depend on the history disk being writable.
                pass
        return run

    @app.get("/api/schema/tables")
    async def tables(request: Request):
        state = runtime(request)
        if not state.connector:
            raise HTTPException(503, state.database_error)
        return await state.connector.list_tables()

    @app.get("/api/schema/tables/{table}")
    async def describe(table: str, request: Request):
        state = runtime(request)
        if not state.connector:
            raise HTTPException(503, state.database_error)
        return await state.connector.describe_table(table)

    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (static_dir / "index.html").is_file():
            return _error(
                "FRONTEND_NOT_BUILT",
                "请先在 frontend 执行 npm ci 和 npm run build。",
                503,
            )
        return FileResponse(static_dir / "index.html")

    return app
