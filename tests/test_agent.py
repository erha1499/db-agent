"""离线协议测试：HTTP 返回值均为测试 stub，不代表真实模型或数据库结果。"""

import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from db_agent import agent as agent_module
from db_agent.cli import main

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
            response = json.dumps(state["response"]).encode()
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
    assert "tools" not in body
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
    assert "数据库能力尚未接入" in output.out
    assert output.err == ""
    assert len(stub_server["requests"]) == 1


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
def test_incomplete_answers_are_reported_as_failures(
    stub_server, capsys, response, expected_error
):
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
