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
    }
    assert body["messages"][-1] == {"role": "user", "content": "解释 SELECT 1 的含义"}
    assert TEST_KEY not in json.dumps(body)


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
def test_http_errors_hide_raw_details_and_are_not_retried(stub_server, capsys, status):
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


def test_total_budget_cancels_pending_agent_call(stub_server, monkeypatch, capsys):
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
    assert events[-1]["code"] == "TimeoutError"


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
