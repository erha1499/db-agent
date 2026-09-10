"""离线协议测试：HTTP 返回值均为测试 stub，不代表真实模型或数据库结果。"""

import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from db_agent import agent as agent_module
from db_agent.cli import main
from db_agent.db import MetadataConnector

TEST_KEY = "synthetic-project-key"
TEST_MODEL = "synthetic-project-model"


def completion(content="这是本地 stub 的回答。", finish_reason="stop"):
    return {
        "id": "chatcmpl-local-stub",
        "object": "chat.completion",
        "created": 0,
        "model": TEST_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.upper().startswith(("DB_AGENT_", "OPENAI_", "LANGCHAIN_", "LANGSMITH_")):
            monkeypatch.delenv(name)
        elif name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("DB_AGENT_API_KEY", TEST_KEY)
    monkeypatch.setenv("DB_AGENT_MODEL", TEST_MODEL)
    monkeypatch.setenv("DB_AGENT_MAX_OUTPUT_TOKENS", "137")
    monkeypatch.setenv("DB_AGENT_MYSQL_PASSWORD", "synthetic-reader-secret")
    monkeypatch.setenv("DB_AGENT_MYSQL_ALLOWED_TABLES", '["orders", "customers"]')
    # 全局值故意不同，确保实际请求使用项目配置。
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unrelated-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/unrelated")
    monkeypatch.setenv("OPENAI_MODEL", "synthetic-unrelated-model")


@pytest.fixture
def stub_server(monkeypatch):
    state = {"status": 200, "response": completion(), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(body),
                }
            )
            responses = state.get("responses")
            payload = responses.pop(0) if responses else state["response"]
            response = json.dumps(payload).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        state["base_url"] = f"http://127.0.0.1:{server.server_port}/gateway/v1"
        monkeypatch.setenv("DB_AGENT_OPENAI_BASE_URL", state["base_url"])
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.fixture
def captured_http_clients(monkeypatch):
    sync_factory = agent_module.DefaultHttpxClient
    async_factory = agent_module.DefaultAsyncHttpxClient
    clients = []

    def sync_client(*args, **kwargs):
        client = sync_factory(*args, **kwargs)
        clients.append(client)
        return client

    def async_client(*args, **kwargs):
        client = async_factory(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(agent_module, "DefaultHttpxClient", sync_client)
    monkeypatch.setattr(agent_module, "DefaultAsyncHttpxClient", async_client)
    return clients


@pytest.fixture
def isolate_query_reports_from_semantic_review(monkeypatch):
    """Opt-in offline isolation for report/termination tests, not semantic approval tests."""
    async def prepare_query(self, request):
        return request

    monkeypatch.setattr(agent_module.RuntimeMiddleware, "_prepare_query", prepare_query)


def test_chat_uses_project_endpoint_credentials_and_budgets(stub_server, capsys):
    assert main(["chat", "解释 SELECT 1 的含义"]) == 0

    output = capsys.readouterr()
    assert output.out == "这是本地 stub 的回答。\n"
    assert output.err == ""
    assert len(stub_server["requests"]) == 1
    request = stub_server["requests"][0]
    assert request["path"] == "/gateway/v1/chat/completions"
    assert request["authorization"] == f"Bearer {TEST_KEY}"
    body = request["body"]
    assert body["model"] == TEST_MODEL
    assert body["stream"] is False
    assert body.get("max_completion_tokens", body.get("max_tokens")) == 137
    assert {tool["function"]["name"] for tool in body["tools"]} == {
        "list_tables",
        "describe_table",
        "analyze_sql",
        "execute_query",
    }
    assert body["messages"][-1] == {"role": "user", "content": "解释 SELECT 1 的含义"}
    assert TEST_KEY not in json.dumps(body)


@pytest.mark.parametrize("shared_loop", [False, True])
def test_repeated_agent_runs_can_reuse_endpoint_and_credentials(
    stub_server, capsys, shared_loop, captured_http_clients,
):
    from db_agent.config import load_settings

    if shared_loop:
        settings = load_settings()

        async def run_twice():
            first = await agent_module.run_agent("第一次请求", settings)
            second = await agent_module.run_agent("第二次请求", settings)
            return first, second

        assert asyncio.run(run_twice()) == ("这是本地 stub 的回答。", "这是本地 stub 的回答。")
    else:
        assert main(["chat", "第一次请求"]) == 0
        assert main(["chat", "第二次请求"]) == 0
        assert capsys.readouterr().out == "这是本地 stub 的回答。\n" * 2

    assert len(stub_server["requests"]) == 2
    assert {request["authorization"] for request in stub_server["requests"]} == {
        f"Bearer {TEST_KEY}",
    }
    assert {request["path"] for request in stub_server["requests"]} == {
        "/gateway/v1/chat/completions",
    }
    assert len(captured_http_clients) == 4
    assert len({id(client) for client in captured_http_clients}) == 4
    assert all(client.is_closed for client in captured_http_clients)


def test_config_hides_values_and_does_not_call_model(stub_server, capsys):
    assert main(["config"]) == 0

    output = capsys.readouterr()
    assert "DB_AGENT_API_KEY" in output.out
    assert "DB_AGENT_MODEL" in output.out
    assert "DB_AGENT_OPENAI_BASE_URL" in output.out
    for value in (TEST_KEY, TEST_MODEL, stub_server["base_url"]):
        assert value not in output.out + output.err
    assert output.err == ""
    assert stub_server["requests"] == []


def test_check_requires_exact_acknowledgement(stub_server, capsys):
    stub_server["response"] = completion("DB_AGENT_OK")

    assert main(["check"]) == 0

    output = capsys.readouterr()
    assert "模型连通性检查通过" in output.out
    assert "数据库连接请使用 db check 单独验证" in output.out
    assert output.err == ""
    assert len(stub_server["requests"]) == 1
    assert "tools" not in stub_server["requests"][0]["body"]


def test_check_rejects_unexpected_answer_without_echoing_it(stub_server, capsys):
    stub_server["response"] = completion("unexpected-sensitive-response")

    assert main(["check"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "未返回预期的连通性确认文本" in output.err
    assert "unexpected-sensitive-response" not in output.err


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_hide_raw_details_and_are_not_retried(
    stub_server, capsys, status, captured_http_clients,
):
    stub_server["status"] = status
    stub_server["response"] = {
        "error": {
            "message": f"raw-private-error {TEST_KEY}",
            "type": "stub_error",
            "code": "stub_failure",
        }
    }

    assert main(["chat", "你好"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "模型调用失败" in output.err
    assert "raw-private-error" not in output.err
    assert TEST_KEY not in output.err
    assert "Traceback" not in output.err
    assert len(stub_server["requests"]) == 1
    assert len(captured_http_clients) == 2
    assert all(client.is_closed for client in captured_http_clients)


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (completion("   "), "模型未返回文本回答"),
        (completion("partial-stub-response", "length"), "模型输出达到 token 上限"),
    ],
)
def test_incomplete_answers_are_reported_as_failures(stub_server, capsys, response, expected_error):
    stub_server["response"] = response

    assert main(["chat", "你好"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert expected_error in output.err
    assert "partial-stub-response" not in output.err
    assert len(stub_server["requests"]) == 1


def test_total_budget_cancels_pending_agent_call(
    stub_server, monkeypatch, capsys, captured_http_clients,
):
    state = {"started": False, "cancelled": False}

    class WaitingAgent:
        async def ainvoke(self, *args, **kwargs):
            state["started"] = True
            try:
                await asyncio.Event().wait()
            finally:
                state["cancelled"] = True

    monkeypatch.setattr(agent_module, "create_agent", lambda **kwargs: WaitingAgent())
    monkeypatch.setenv("DB_AGENT_RUN_TIMEOUT_SECONDS", "0.01")

    assert main(["chat", "你好"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "超过总时间预算，已停止等待" in output.err
    assert state == {"started": True, "cancelled": True}
    assert stub_server["requests"] == []
    assert len(captured_http_clients) == 2
    assert all(client.is_closed for client in captured_http_clients)


@pytest.mark.parametrize("phase", ["model_construction", "external_cancellation"])
def test_agent_closes_owned_http_clients_when_startup_or_run_is_interrupted(
    stub_server, monkeypatch, phase, captured_http_clients,
):
    from db_agent.config import load_settings

    if phase == "model_construction":
        def fail_model(**kwargs):
            raise ValueError("synthetic-construction-failure")

        monkeypatch.setattr(agent_module, "ChatOpenAI", fail_model)
        expected = ValueError
    else:
        class CancelledAgent:
            async def ainvoke(self, *args, **kwargs):
                raise asyncio.CancelledError

        monkeypatch.setattr(agent_module, "create_agent", lambda **kwargs: CancelledAgent())
        expected = asyncio.CancelledError

    with pytest.raises(expected):
        asyncio.run(agent_module.run_agent("离线资源清理验证", load_settings()))
    assert len(captured_http_clients) == 2
    assert all(client.is_closed for client in captured_http_clients)
    assert stub_server["requests"] == []


def tool_completion(*calls, finish_reason="tool_calls"):
    response = completion(None, finish_reason)
    response["choices"][0]["message"]["tool_calls"] = [
        {
            "id": f"synthetic-call-{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        for index, (name, arguments) in enumerate(calls)
    ]
    return response


def test_tool_result_is_returned_to_model_with_call_id(stub_server, monkeypatch, capsys, tmp_path):
    calls = []

    async def describe(self, table):
        calls.append(table)
        return {"table": table, "columns": [{"name": "synthetic_column"}], "indexes": []}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "orders"})),
        completion("根据工具结构，orders 有 synthetic_column 字段。"),
    ]
    assert main(["chat", "private-prompt-marker 请查看订单结构"]) == 0
    assert calls == ["orders"]
    requests = stub_server["requests"]
    assert len(requests) == 2
    tool_message = requests[1]["body"]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "synthetic-call-0"
    assert "synthetic_column" in tool_message["content"]
    assert "synthetic-reader-secret" not in json.dumps(requests)
    schemas = json.dumps(requests[0]["body"]["tools"])
    for field in ('"host"', '"database"', '"role"', '"password"'):
        assert field not in schemas
    logs = list((tmp_path / "outputs/runs").glob("*.jsonl"))
    assert len(logs) == 1
    text = logs[0].read_text()
    for value in ("private-prompt-marker", "synthetic_column", "synthetic-reader-secret", TEST_KEY):
        assert value not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert len([e for e in events if e["event"] == "model_finished"]) == 2
    assert len([e for e in events if e["event"] == "tool_finished"]) == 1
    assert events[-1]["status"] == "ok"
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "arguments",
    [
        {"table": "mysql.user"},
        {"table": "orders; DROP TABLE orders"},
        {"table": "orders", "database": "mysql"},
        {"table": 123},
    ],
)
def test_invalid_tool_arguments_never_reach_connector(stub_server, monkeypatch, capsys, arguments):
    calls = []

    async def describe(self, table):
        calls.append(table)
        raise AssertionError("connector must not be called")

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    stub_server["responses"] = [
        tool_completion(("describe_table", arguments)),
        completion("工具参数被拒绝。"),
    ]
    assert main(["chat", "检查表结构"]) == 0
    assert calls == []
    content = stub_server["requests"][1]["body"]["messages"][-1]["content"]
    assert "工具参数无效" in content
    assert capsys.readouterr().err == ""


def test_unauthorized_table_is_rejected_without_connecting(stub_server, monkeypatch, capsys):
    from db_agent import db

    calls = []

    async def connect(**kwargs):
        calls.append(True)
        raise AssertionError("unauthorized request connected")

    monkeypatch.setattr(db.aiomysql, "connect", connect)
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "private_table"})),
        completion("该表未获授权。"),
    ]
    assert main(["chat", "请读取 private_table"]) == 0
    assert calls == []
    content = stub_server["requests"][1]["body"]["messages"][-1]["content"]
    assert "PERMISSION_DENIED" in content
    assert capsys.readouterr().err == ""


def test_tool_errors_are_sanitized_before_model_feedback(stub_server, monkeypatch, capsys):
    async def describe(self, table):
        raise RuntimeError("raw-driver-secret-marker")

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "orders"})),
        completion("未能取得元数据。"),
    ]
    assert main(["chat", "检查订单"]) == 0
    assert "raw-driver-secret-marker" not in json.dumps(stub_server["requests"])
    assert "TOOL_ERROR" in stub_server["requests"][1]["body"]["messages"][-1]["content"]
    assert "raw-driver-secret-marker" not in str(capsys.readouterr())


def test_multiple_tool_calls_are_serialized(stub_server, monkeypatch, capsys):
    state = {"active": 0, "maximum": 0, "calls": []}

    async def describe(self, table):
        state["active"] += 1
        state["maximum"] = max(state["maximum"], state["active"])
        await asyncio.sleep(0.01)
        state["calls"].append(table)
        state["active"] -= 1
        return {"table": table}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    stub_server["responses"] = [
        tool_completion(
            ("describe_table", {"table": "orders"}), ("describe_table", {"table": "customers"})
        ),
        completion("已读取两张表结构。"),
    ]
    assert main(["chat", "查看两张表"]) == 0
    assert state == {"active": 0, "maximum": 1, "calls": ["orders", "customers"]}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("budget", ["model", "tool"])
def test_call_budgets_stop_repeated_requests(stub_server, monkeypatch, capsys, budget):
    calls = []

    async def tables(self):
        calls.append(True)
        return {"tables": []}

    monkeypatch.setattr(MetadataConnector, "list_tables", tables)
    if budget == "model":
        monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "2")
        stub_server["response"] = tool_completion(("list_tables", {}))
    else:
        monkeypatch.setenv("DB_AGENT_MAX_TOOL_CALLS", "1")
        stub_server["response"] = tool_completion(("list_tables", {}), ("list_tables", {}))
    assert main(["chat", "一直检查"]) == 1
    assert "调用次数达到预算" in capsys.readouterr().err
    assert len(stub_server["requests"]) == (2 if budget == "model" else 1)
    assert len(calls) <= (2 if budget == "model" else 0)


def test_truncated_tool_call_is_not_executed(stub_server, monkeypatch, capsys):
    calls = []

    async def tables(self):
        calls.append(True)
        return {"tables": []}

    monkeypatch.setattr(MetadataConnector, "list_tables", tables)
    stub_server["response"] = tool_completion(("list_tables", {}), finish_reason="length")
    assert main(["chat", "检查库表"]) == 1
    assert calls == []
    assert "token 上限" in capsys.readouterr().err


def test_database_cli_works_without_model_credentials(stub_server, monkeypatch, capsys):
    for name in ("DB_AGENT_API_KEY", "DB_AGENT_MODEL", "DB_AGENT_OPENAI_BASE_URL"):
        monkeypatch.delenv(name)

    async def check(self):
        return {"connection_ok": True, "database": "db_agent", "server_version": "synthetic"}

    monkeypatch.setattr(MetadataConnector, "check", check)
    assert main(["db", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["connection_ok"] is True
    assert stub_server["requests"] == []


def test_logging_failure_does_not_change_business_result(stub_server, capsys, tmp_path):
    (tmp_path / "outputs").write_text("existing-file")
    assert main(["chat", "正常回答"]) == 0
    output = capsys.readouterr()
    assert "这是本地 stub 的回答" in output.out
    assert output.err.count("运行记录写入失败") == 1


def test_total_timeout_cancels_tool_and_closes_database(stub_server, monkeypatch, capsys, tmp_path):
    from db_agent import db

    state = {"started": False, "closed": False}

    class Cursor:
        async def execute(self, sql, params):
            if "information_schema" in sql:
                state["started"] = True
                await asyncio.Event().wait()

        async def fetchone(self):
            return None

        async def close(self):
            pass

    class Connection:
        async def cursor(self):
            return Cursor()

        def close(self):
            state["closed"] = True

    async def connect(**kwargs):
        return Connection()

    monkeypatch.setattr(db.aiomysql, "connect", connect)
    monkeypatch.setenv("DB_AGENT_RUN_TIMEOUT_SECONDS", "0.3")
    stub_server["response"] = tool_completion(("describe_table", {"table": "orders"}))
    assert main(["chat", "检查 orders"]) == 1
    assert state == {"started": True, "closed": True}
    assert len(stub_server["requests"]) == 1
    assert "超过总时间预算" in capsys.readouterr().err
    path = next((tmp_path / "outputs/runs").glob("*.jsonl"))
    events = [json.loads(line) for line in path.read_text().splitlines()]
    tool_event = next(e for e in events if e["event"] == "tool_finished")
    assert tool_event["status"] == "error"
    assert tool_event["code"] == "CANCELLED"
    assert events[-1]["status"] == "error"
    assert events[-1]["code"] == "AgentResponseError"


def test_sql_block_is_business_result_returned_to_main_model(
    stub_server, monkeypatch, capsys, tmp_path
):
    from db_agent import db

    async def connect(**kwargs):
        raise AssertionError("blocked SQL must not connect")

    monkeypatch.setattr(db.aiomysql, "connect", connect)
    stub_server["responses"] = [
        tool_completion(("analyze_sql", {"sql": "DELETE FROM orders WHERE id = 987654321"})),
        completion("预检拒绝了写操作，未执行 SQL。"),
    ]
    assert main(["chat", "分析删除订单语句"]) == 0
    assert len(stub_server["requests"]) == 2  # No nested LLM inside analyze_sql.
    message = stub_server["requests"][1]["body"]["messages"][-1]
    report = json.loads(message["content"])
    assert message["tool_call_id"] == "synthetic-call-0"
    assert report["decision"] == "BLOCK"
    assert report["evidence_source"] == "static_only"
    assert report["plan_summary"] is None
    assert "987654321" not in message["content"]
    events = [
        json.loads(line)
        for path in (tmp_path / "outputs/runs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    event = next(e for e in events if e["event"] == "analysis_finished")
    assert event["decision"] == "BLOCK"
    assert event["report_id"] == report["report_id"]
    assert event["rule_ids"]
    assert "987654321" not in json.dumps(events)
    assert next(e for e in events if e["event"] == "tool_finished")["status"] == "ok"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "arguments",
    [
        {"sql": "SELECT 1", "database": "mysql"},
        {"sql": 123},
        {"sql": "SELECT 1", "approved": True},
    ],
)
def test_sql_tool_rejects_model_supplied_scope_or_approval(
    stub_server, monkeypatch, capsys, arguments
):
    from db_agent.analysis import SqlAnalysisService

    async def analyze(self, sql):
        pytest.fail("invalid arguments reached service")

    monkeypatch.setattr(SqlAnalysisService, "analyze", analyze)
    stub_server["responses"] = [
        tool_completion(("analyze_sql", arguments)),
        completion("参数无效。"),
    ]
    assert main(["chat", "分析 SQL"]) == 0
    message = stub_server["requests"][1]["body"]["messages"][-1]
    assert "工具参数无效" in message["content"]
    assert capsys.readouterr().err == ""


def test_analysis_cli_without_model_credentials(stub_server, monkeypatch, capsys):
    for name in ("DB_AGENT_API_KEY", "DB_AGENT_MODEL", "DB_AGENT_OPENAI_BASE_URL"):
        monkeypatch.delenv(name)
    assert main(["db", "analyze", "DELETE FROM orders"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["decision"] == "BLOCK"
    assert stub_server["requests"] == []


def test_analysis_cli_stdin_is_bounded(stub_server, monkeypatch, capsys):
    import io
    import sys

    monkeypatch.setenv("DB_AGENT_ANALYSIS_MAX_SQL_BYTES", "64")
    buffer = io.BytesIO(b"SELECT '" + b"x" * 100000 + b"'")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(buffer))
    assert main(["db", "analyze", "--stdin"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["decision"] == "UNKNOWN"
    assert buffer.tell() == 65
    assert stub_server["requests"] == []


@pytest.mark.parametrize("case", ["complete", "empty", "truncated", "rejected", "error"])
def test_execute_query_service_result_is_rendered_without_model_feedback_or_raw_logs(
    stub_server, monkeypatch, capsys, tmp_path, case,
    isolate_query_reports_from_semantic_review,
):
    from db_agent.plans import PlanAnalysis

    sql = "SELECT id FROM orders WHERE id = 987654321"
    calls = []
    decision = "REVIEW" if case == "rejected" else "ALLOW"
    execution = {"rejected": "not_started", "error": "unknown", "truncated": "truncated"}.get(
        case, "completed",
    )
    rows = [] if case == "empty" else [["private-row-marker"]]
    data = {
        "columns": [{"name": "id", "type": "bigint"}],
        "rows": rows,
        "row_count": len(rows),
        "truncated": case == "truncated",
        "truncation_reason": "row_limit" if case == "truncated" else None,
        "result_bytes": 256,
        "server_statement_status": "unknown" if case == "truncated" else "completed",
    }

    async def execute_checked(self, actual_sql, analysis_limits, query_limits):
        calls.append(actual_sql)
        return {
            "check": self.check_sql(actual_sql, analysis_limits),
            "assessment": PlanAnalysis(decision, (), {"tables": [], "operations": []}),
            "decision": decision,
            "execution_status": execution,
            "result": None if case in {"rejected", "error"} else data,
            "server_version": "8.4.11",
            "error": (
                {"code": "TIMEOUT", "message": "查询结果未确认。"} if case == "error" else None
            ),
        }

    monkeypatch.setattr(MetadataConnector, "execute_checked", execute_checked)
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": sql})),
        completion("2026年二月29日，DATETIME必为UTC；未执行成功，截断总量为999999。"),
    ]
    assert main(["chat", "private-prompt-marker 查询订单数据"]) == 0
    assert calls == [sql]
    assert len(stub_server["requests"]) == 1
    assert len(stub_server["responses"]) == 1  # Unused final text is never requested.
    assert "private-row-marker" not in json.dumps(stub_server["requests"])
    schemas = stub_server["requests"][0]["body"]["tools"]
    schema = next(tool["function"]["parameters"] for tool in schemas
                  if tool["function"]["name"] == "execute_query")
    assert set(schema["properties"]) == {"sql"}
    assert schema["required"] == ["sql"]
    logs = "\n".join(path.read_text() for path in (tmp_path / "outputs/runs").glob("*.jsonl"))
    for private in (sql, "987654321", "private-row-marker", "private-prompt-marker", TEST_KEY):
        assert private not in logs
    events = [json.loads(line) for line in logs.splitlines()]
    event = next(event for event in events if event["event"] == "query_finished")
    assert event["decision"] == decision
    assert event["execution_status"] == execution
    output = capsys.readouterr()
    assert output.err == ""
    for fabricated in ("2026年二月29日", "DATETIME必为UTC", "未执行成功", "截断总量为999999"):
        assert fabricated not in output.out
    assert sql in output.out
    if case in {"complete", "truncated"}:
        assert "private-row-marker" in output.out
    elif case == "empty":
        assert "返回 0 行" in output.out and "空集" in output.out
    else:
        assert "private-row-marker" not in output.out


@pytest.mark.parametrize("arguments", [
    {}, {"sql": 123}, {"sql": None}, {"sql": ""}, {"sql": ["SELECT id FROM orders"]},
    {"sql": "SELECT id FROM orders", "database": "mysql"},
    {"sql": "SELECT id FROM orders", "approved": True},
    {"sql": "SELECT id FROM orders", "report_id": "old-report"},
    {"sql": "SELECT id FROM orders", "max_rows": 999999},
])
def test_execute_query_tool_accepts_only_a_strict_sql_argument(
    stub_server, monkeypatch, capsys, tmp_path, arguments,
):
    from db_agent.query import QueryService

    async def execute(self, sql):
        pytest.fail("invalid execute_query arguments reached the application service")

    async def describe(self, table):
        pytest.fail("invalid arguments must be rejected before schema collection or review")

    monkeypatch.setattr(QueryService, "execute", execute)
    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    stub_server["responses"] = [
        tool_completion(("execute_query", arguments)),
        completion("查询工具参数无效。"),
    ]
    assert main(["chat", "查询订单"]) == 0
    assert len(stub_server["requests"]) == 1
    assert len(stub_server["responses"]) == 1
    path = next((tmp_path / "outputs/runs").glob("*.jsonl"))
    events = [json.loads(line) for line in path.read_text().splitlines()]
    event = next(event for event in events if event["event"] == "tool_finished")
    assert event["code"] == "INVALID_ARGUMENT" and event["status"] == "error"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(("status", "execution", "expected_exit"), [
    ("ok", "completed", 0), ("ok", "truncated", 0),
    ("rejected", "not_started", 3), ("error", "unknown", 1),
])
def test_query_cli_without_model_credentials_uses_business_status_for_exit_code(
    stub_server, monkeypatch, capsys, status, execution, expected_exit,
):
    from db_agent.query import QueryService

    for name in ("DB_AGENT_API_KEY", "DB_AGENT_MODEL", "DB_AGENT_OPENAI_BASE_URL"):
        monkeypatch.delenv(name)
    calls = []
    response = {
        "status": status,
        "decision": "REVIEW" if status == "rejected" else "ALLOW",
        "execution_status": execution,
        "result": {"rows": [], "row_count": 0} if status == "ok" else None,
        "error": {"code": "TIMEOUT", "message": "结果未确认。"} if status == "error" else None,
    }

    async def execute(self, sql):
        calls.append(sql)
        return response

    monkeypatch.setattr(QueryService, "execute", execute)
    assert main(["db", "query", "SELECT id FROM orders"]) == expected_exit
    output = capsys.readouterr()
    assert json.loads(output.out) == response
    assert output.err == ""
    assert calls == ["SELECT id FROM orders"]
    assert stub_server["requests"] == []


def test_query_cli_direct_static_rejection_uses_exit_three_without_connecting(
    stub_server, monkeypatch, capsys,
):
    from db_agent import db

    async def connect(**kwargs):
        pytest.fail("a blocked CLI query must not connect")

    monkeypatch.setattr(db.aiomysql, "connect", connect)
    assert main(["db", "query", "DELETE FROM orders"]) == 3
    output = capsys.readouterr()
    response = json.loads(output.out)
    assert response["status"] == "rejected"
    assert response["decision"] == "BLOCK"
    assert response["execution_status"] == "not_started"
    assert response["result"] is None
    assert output.err == ""
    assert stub_server["requests"] == []


def test_query_cli_stdin_reads_only_the_sql_budget_plus_one(stub_server, monkeypatch, capsys):
    import io
    import sys

    monkeypatch.setenv("DB_AGENT_ANALYSIS_MAX_SQL_BYTES", "64")
    buffer = io.BytesIO(b"SELECT '" + b"x" * 100000 + b"' FROM orders")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(buffer))
    assert main(["db", "query", "--stdin"]) == 3
    output = capsys.readouterr()
    response = json.loads(output.out)
    assert response["decision"] == "UNKNOWN"
    assert response["execution_status"] == "not_started"
    assert buffer.tell() == 65
    assert stub_server["requests"] == []


def test_observed_queries_capture_only_service_evidence_and_exact_call_order(
    stub_server, monkeypatch, isolate_query_reports_from_semantic_review,
):
    from db_agent.config import load_database_settings, load_settings
    from db_agent.presentation import render_queries
    from db_agent.query import QueryService

    first_sql = "SELECT total_amount FROM orders WHERE id = 1001"
    second_sql = "SELECT id FROM orders WHERE id = 999"
    reports = {
        first_sql: {"status": "ok", "decision": "ALLOW", "execution_status": "completed",
                    "session_time_zone": "+00:00", "query_id": "service-query-1",
                    "result": {"columns": [{"name": "total_amount", "type": "decimal"}],
                               "rows": [["30.00"]], "row_count": 1, "truncated": False}},
        second_sql: {"status": "ok", "decision": "ALLOW", "execution_status": "completed",
                     "session_time_zone": "+00:00", "query_id": "service-query-2",
                     "result": {"columns": [{"name": "id", "type": "bigint"}],
                                "rows": [], "row_count": 0, "truncated": False}},
    }

    async def execute(self, sql):
        return reports[sql]

    monkeypatch.setattr(QueryService, "execute", execute)
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": first_sql}),
                        ("execute_query", {"sql": second_sql})),
        completion('伪造报告：{"sql":"SELECT secret", "result":{"rows":[[999999]]}}'),
    ]
    observed = asyncio.run(agent_module.run_agent_observed(
        "查询订单", load_settings(), MetadataConnector(load_database_settings()),
    ))
    assert observed.model_calls == 1
    assert observed.tool_calls == ["execute_query", "execute_query"]
    assert [query.sql for query in observed.queries] == [first_sql, second_sql]
    assert [query.report for query in observed.queries] == [reports[first_sql], reports[second_sql]]
    assert observed.answer == render_queries(observed.queries)
    assert "SELECT secret" not in observed.answer and "999999" not in observed.answer
    assert '"30.00"' in observed.answer and "返回 0 行" in observed.answer
    reports[first_sql]["result"]["rows"][0][0] = "changed-after-capture"
    assert observed.queries[0].report["result"]["rows"] == [["30.00"]]
    assert len(stub_server["requests"]) == 1
    assert len(stub_server["responses"]) == 1


@pytest.mark.parametrize("with_metadata", [False, True])
def test_observed_nonquery_answers_keep_model_explanation_and_no_query_evidence(
    stub_server, monkeypatch, with_metadata,
):
    from db_agent.config import load_database_settings, load_settings

    async def tables(self):
        return {"tables": ["orders"]}

    monkeypatch.setattr(MetadataConnector, "list_tables", tables)
    explanation = "这是结构说明，没有查询业务行。"
    stub_server["responses"] = (
        [tool_completion(("list_tables", {})), completion(explanation)]
        if with_metadata else [completion(explanation)]
    )
    observed = asyncio.run(agent_module.run_agent_observed(
        "解释表结构", load_settings(), MetadataConnector(load_database_settings()),
    ))
    assert observed.answer == explanation and observed.queries == []
    assert observed.model_calls == (2 if with_metadata else 1)
    assert observed.tool_calls == (["list_tables"] if with_metadata else [])
    assert "### 查询" not in observed.answer


@pytest.mark.parametrize("arguments", [{"sql": 123}, {"sql": "SELECT id FROM orders"}])
def test_failed_query_attempt_has_no_trusted_report_and_cannot_publish_fake_model_data(
    stub_server, monkeypatch, arguments, isolate_query_reports_from_semantic_review,
):
    from db_agent.config import load_database_settings, load_settings
    from db_agent.presentation import render_queries
    from db_agent.query import QueryService

    calls = []

    async def execute(self, sql):
        calls.append(sql)
        raise RuntimeError("private-driver-error")

    monkeypatch.setattr(QueryService, "execute", execute)
    stub_server["responses"] = [
        tool_completion(("execute_query", arguments)), completion("已查询成功，总额999999。"),
    ]
    observed = asyncio.run(agent_module.run_agent_observed(
        "查询订单", load_settings(), MetadataConnector(load_database_settings()),
    ))
    assert observed.queries == [] and observed.tool_calls == ["execute_query"]
    assert observed.answer == render_queries([], missing_reports=1)
    assert "999999" not in observed.answer and "private-driver-error" not in observed.answer
    assert calls == ([arguments["sql"]] if isinstance(arguments["sql"], str) else [])
    assert observed.model_calls == 1 and len(stub_server["requests"]) == 1


@pytest.mark.parametrize("failure_first", [False, True])
@pytest.mark.parametrize("failure", ["arguments", "service"])
def test_mixed_query_attempts_keep_success_and_report_missing_evidence(
    stub_server, monkeypatch, failure_first, failure,
    isolate_query_reports_from_semantic_review,
):
    from db_agent.config import load_database_settings, load_settings
    from db_agent.presentation import render_queries
    from db_agent.query import QueryService

    sql = "SELECT total_amount FROM orders WHERE id = 1001"
    report = {
        "status": "ok", "decision": "ALLOW", "execution_status": "completed",
        "session_time_zone": "+00:00",
        "result": {"columns": [{"name": "total_amount", "type": "decimal"}],
                   "rows": [["30.00"]], "row_count": 1, "truncated": False},
    }

    async def execute(self, actual_sql):
        if actual_sql != sql:
            raise RuntimeError("private-driver-error")
        return report

    monkeypatch.setattr(QueryService, "execute", execute)
    success_call = ("execute_query", {"sql": sql})
    failed_call = ("execute_query", {"sql": 123 if failure == "arguments" else "SELECT invalid"})
    calls = [failed_call, success_call] if failure_first else [success_call, failed_call]
    stub_server["responses"] = [
        tool_completion(*calls), completion("所有请求都已成功，总额999999。"),
    ]
    observed = asyncio.run(agent_module.run_agent_observed(
        "查询两项数据", load_settings(), MetadataConnector(load_database_settings()),
    ))
    assert observed.tool_calls == ["execute_query", "execute_query"]
    assert observed.model_calls == 1 and len(stub_server["requests"]) == 1
    assert len(observed.queries) == 1
    assert observed.queries[0].sql == sql and observed.queries[0].report == report
    assert observed.answer == render_queries(observed.queries, missing_reports=1)
    assert sql in observed.answer and '"30.00"' in observed.answer
    assert "有 1 次查询工具请求未取得可确认的执行报告" in observed.answer
    assert "执行状态无法确认" in observed.answer
    for claim in ("所有请求都已成功", "999999", "private-driver-error", "SQL 未执行"):
        assert claim not in observed.answer


def test_static_query_rejection_ends_before_an_unneeded_model_budget_check(
    stub_server, monkeypatch,
):
    from db_agent.config import load_database_settings, load_settings
    from db_agent.query import QueryService

    async def execute(self, sql):
        return {"status": "rejected", "decision": "BLOCK", "execution_status": "not_started"}

    monkeypatch.setattr(QueryService, "execute", execute)
    monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "1")
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": "DELETE FROM orders"}))]
    observed = asyncio.run(agent_module.run_agent_observed(
        "拒绝删除订单", load_settings(), MetadataConnector(load_database_settings()),
    ))
    assert observed.model_calls == 1
    assert observed.tool_calls == ["execute_query"]
    assert observed.queries[0].report["decision"] == "BLOCK"
    assert "未执行" in observed.answer
    assert len(stub_server["requests"]) == 1


@pytest.mark.parametrize("attempt_fifth", [False, True])
def test_graph_allows_four_model_calls_but_fifth_is_stopped_by_model_budget(
    stub_server, monkeypatch, attempt_fifth,
):
    from db_agent.config import load_database_settings, load_settings

    metadata_calls = []

    async def describe(self, table):
        metadata_calls.append(("describe_table", table))
        return {"table": table, "columns": [{"name": "total_amount", "type": "decimal"}],
                "indexes": []}

    async def tables(self):
        metadata_calls.append(("list_tables", None))
        return {"tables": ["orders", "customers"]}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    monkeypatch.setattr(MetadataConnector, "list_tables", tables)
    monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "4")
    monkeypatch.setenv("DB_AGENT_MAX_TOOL_CALLS", "6")
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "orders"})),
        tool_completion(("describe_table", {"table": "customers"})),
        tool_completion(("list_tables", {})),
        tool_completion(("list_tables", {})) if attempt_fifth else completion("结构核对完成。"),
        completion("不应发起第五次模型调用。"),
    ]
    run = agent_module.run_agent_observed(
        "说明订单和客户结构", load_settings(), MetadataConnector(load_database_settings()),
    )
    if attempt_fifth:
        with pytest.raises(agent_module.AgentResponseError, match="调用次数达到预算") as error:
            asyncio.run(run)
        assert isinstance(error.value.__context__, agent_module.ModelCallLimitExceededError)
    else:
        observed = asyncio.run(run)
        assert observed.model_calls == 4
        assert observed.tool_calls == ["describe_table", "describe_table", "list_tables"]
        assert observed.queries == []
        assert observed.answer == "结构核对完成。"
    assert len(stub_server["requests"]) == 4
    assert metadata_calls == [
        ("describe_table", "orders"), ("describe_table", "customers"), ("list_tables", None),
    ] + ([("list_tables", None)] if attempt_fifth else [])
    assert len(stub_server["responses"]) == 1


@pytest.mark.parametrize("query_first", [False, True])
@pytest.mark.parametrize("metadata_operation", ["describe_table", "analyze_sql"])
def test_query_and_metadata_same_round_finish_without_extra_model_call(
    stub_server, monkeypatch, query_first, metadata_operation,
    isolate_query_reports_from_semantic_review,
):
    from db_agent.analysis import SqlAnalysisService
    from db_agent.config import load_database_settings, load_settings
    from db_agent.query import QueryService

    expected_sql = "SELECT id FROM orders"
    completed = []
    report = {
        "status": "ok", "decision": "ALLOW", "execution_status": "completed",
        "session_time_zone": "+00:00",
        "result": {"columns": [{"name": "id", "type": "bigint"}], "rows": [[1]],
                   "row_count": 1, "truncated": False},
    }

    async def execute(self, sql):
        assert sql == expected_sql
        completed.append("execute_query")
        return report

    async def describe(self, table):
        assert table == "orders"
        completed.append("describe_table")
        return {"table": table, "columns": [{"name": "id"}], "indexes": []}

    async def analyze(self, sql):
        assert sql == expected_sql
        completed.append("analyze_sql")
        return {"decision": "ALLOW", "findings": []}

    monkeypatch.setattr(QueryService, "execute", execute)
    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    monkeypatch.setattr(SqlAnalysisService, "analyze", analyze)
    monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "1")
    query_call = ("execute_query", {"sql": expected_sql})
    metadata_call = (
        metadata_operation,
        {"table": "orders"} if metadata_operation == "describe_table" else {"sql": expected_sql},
    )
    calls = [query_call, metadata_call] if query_first else [metadata_call, query_call]
    stub_server["responses"] = [tool_completion(*calls), completion("不应请求的最终模型文本。")]
    observed = asyncio.run(agent_module.run_agent_observed(
        "查询订单并查看结构或诊断", load_settings(), MetadataConnector(load_database_settings()),
    ))
    assert completed == [call[0] for call in calls]
    assert observed.tool_calls == completed
    assert observed.model_calls == 1 and len(stub_server["requests"]) == 1
    assert len(stub_server["responses"]) == 1
    assert len(observed.queries) == 1 and observed.queries[0].report == report
    assert "当前 SQL 结果已完整返回" in observed.answer
    assert "不应请求" not in observed.answer


def test_query_capability_contract_is_sent_in_prompt_and_sql_tool_descriptions(stub_server, capsys):
    assert main(["chat", "说明支持的查询能力"]) == 0
    body = stub_server["requests"][0]["body"]
    prompt = body["messages"][0]["content"]
    descriptions = [tool["function"]["description"] for tool in body["tools"]
                    if tool["function"]["name"] in {"analyze_sql", "execute_query"}]
    assert len(descriptions) == 2
    for text in [prompt, *descriptions]:
        assert "ASCII" in text and "COUNT/SUM/AVG/MIN/MAX" in text
        assert "COALESCE/ROUND" in text and "保留 NULL" in text
    assert capsys.readouterr().err == ""


def test_observed_query_cancellation_propagates_and_releases_clients(
    stub_server, monkeypatch, captured_http_clients,
    isolate_query_reports_from_semantic_review,
):
    from db_agent.config import load_database_settings, load_settings
    from db_agent.query import QueryService

    state = {"cancelled": False}

    async def run():
        entered = asyncio.Event()

        async def execute(self, sql):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                state["cancelled"] = True

        monkeypatch.setattr(QueryService, "execute", execute)
        stub_server["response"] = tool_completion(
            ("execute_query", {"sql": "SELECT id FROM orders"}),
        )
        task = asyncio.create_task(agent_module.run_agent_observed(
            "查询订单", load_settings(), MetadataConnector(load_database_settings()),
        ))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert state["cancelled"] and len(stub_server["requests"]) == 1
    assert len(captured_http_clients) == 2
    assert all(client.is_closed for client in captured_http_clients)
