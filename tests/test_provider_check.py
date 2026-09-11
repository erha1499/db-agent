"""Offline HTTP fixtures for the explicit probe; never contact a real provider."""

import asyncio
import importlib.util
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from langchain_core.messages import AIMessage

from db_agent.config import Settings

SPEC = importlib.util.spec_from_file_location(
    "provider_check", Path(__file__).resolve().parents[1] / "scripts" / "check_provider.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def completion(*, structured=False, finish=None, arguments=None, output_tokens=5):
    message = {"role": "assistant", "content": "private-provider-response"}
    if structured:
        message["content"] = None
        message["tool_calls"] = [{
            "id": "call-private-identifier", "type": "function",
            "function": {
                "name": "ProviderProbe",
                "arguments": arguments or '{"value":7,"marker":"provider-probe-v1"}',
            },
        }]
    return {
        "id": "private-response-id", "object": "chat.completion", "created": 0,
        "model": "private-provider-model",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish or ("tool_calls" if structured else "stop")}],
        "usage": {"prompt_tokens": 3, "completion_tokens": output_tokens,
                  "total_tokens": 3 + output_tokens,
                  "completion_tokens_details": {"reasoning_tokens": 2}},
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


@pytest.fixture
def stub():
    state = {"responses": [completion(), completion(structured=True)], "requests": [],
             "delay": 0, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            response = state["responses"].pop(0)
            time.sleep(state["delay"])
            data = json.dumps(response).encode()
            try:
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        state["url"] = f"http://127.0.0.1:{server.server_port}/private-route/v1"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=2)


def settings(stub, **kwargs):
    return Settings(
        _env_file=None, openai_base_url=stub["url"], api_key="private-api-key",
        model="private-model-config", max_output_tokens=kwargs.pop("max_output_tokens", 137),
        **kwargs,
    )


@pytest.mark.parametrize("finish", ["tool_calls", "stop"])
def test_protocol_uses_actual_framework_wire_and_redacts(stub, finish):
    stub["responses"][1]["choices"][0]["finish_reason"] = finish
    report = asyncio.run(probe.run_probe(settings(stub)))
    assert report["protocol_compatible"] and report["clients_closed"], report["observations"]
    assert len(stub["requests"]) == 2
    assert report["http_statuses"] == [200, 200]
    for request, actual in zip(report["requests"], stub["requests"], strict=True):
        assert actual["model"] == "private-model-config"
        assert actual["max_completion_tokens"] == request["max_completion_tokens"] == 137
        assert not actual["stream"]
    assert report["requests"][1]["tool_choice_auto"]
    assert report["requests"][1]["tool_count"] == 1
    usage = report["observations"][1]["usage"]
    assert usage["normalized"]["output_tokens"] == 5
    assert usage["provider"]["reasoning_tokens"] == 2
    assert usage["reported_output_exceeds_requested"] is False
    serialized = json.dumps(report)
    for private in ("private-provider-response", "private-api-key", "private-route",
                    "private-model-config", "private-provider-model", "call-private-identifier"):
        assert private not in serialized
    assert not report["hard_token_limit_verified"]
    assert not report["billing_verified"]
    assert not report["provider_server_cancellation_verified"]


@pytest.mark.parametrize("response", [
    completion(structured=True, finish="length"),
    completion(structured=True, arguments='{"value":7'),
    completion(structured=True, arguments='{"value":8,"marker":"provider-probe-v1"}'),
    completion(structured=True, arguments='{"value":7,"marker":"provider-probe-v1","x":1}'),
    completion(),
])
def test_incomplete_or_wrong_tool_protocol_is_not_compatible(stub, response):
    stub["responses"][1] = response
    report = asyncio.run(probe.run_probe(settings(stub)))
    assert not report["protocol_compatible"]
    assert report["observations"][1]["protocol_valid"] is False
    assert report["clients_closed"]


@pytest.mark.parametrize("mutation", [
    "multiple", "unknown_tool", "free_text", "missing_finish", "content_filter", "unknown_finish",
])
def test_ambiguous_tool_protocol_is_rejected(stub, mutation):
    response = stub["responses"][1]
    message = response["choices"][0]["message"]
    if mutation == "multiple":
        message["tool_calls"].append(message["tool_calls"][0].copy())
    elif mutation == "unknown_tool":
        message["tool_calls"][0]["function"]["name"] = "UnexpectedTool"
    elif mutation == "free_text":
        message["content"] = "private-provider-response"
    elif mutation == "missing_finish":
        response["choices"][0]["finish_reason"] = None
    else:
        response["choices"][0]["finish_reason"] = mutation
    report = asyncio.run(probe.run_probe(settings(stub)))
    assert not report["protocol_compatible"]
    assert report["clients_closed"]


def test_parsed_object_cannot_replace_framework_call():
    message = AIMessage(content="", response_metadata={"finish_reason": "tool_calls"},
                        tool_calls=[{"name": "ProviderProbe", "id": "fixture-id",
                                     "args": {"value": 8, "marker": "provider-probe-v1"}}])
    response = {"raw": message, "parsed": probe.ProviderProbe(value=7, marker="provider-probe-v1"),
                "parsing_error": None}
    assert probe._structured_valid(response) is False


def test_usage_anomaly_is_observed_without_claiming_hard_limit(stub):
    stub["responses"][0] = completion(output_tokens=300)
    report = asyncio.run(probe.run_probe(settings(stub)))
    assert report["protocol_compatible"]
    assert report["observations"][0]["usage"]["reported_output_exceeds_requested"]
    assert not report["hard_token_limit_verified"]
    assert all(request["max_completion_tokens"] == 137 for request in stub["requests"])


def test_missing_usage_is_unknown_not_zero():
    usage = probe._usage(AIMessage(content="private-provider-response"), 137)
    assert usage["reported_output_exceeds_requested"] is None
    assert usage["normalized"]["output_tokens"] is None
    assert usage["provider"]["completion_tokens"] is None
    assert usage["output_counters_agree"] is None


def test_usage_normalization_disagreement_is_visible():
    message = AIMessage(content="", usage_metadata={
        "input_tokens": 3, "output_tokens": 5, "total_tokens": 8,
    }, response_metadata={"token_usage": {
        "prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10,
    }})
    usage = probe._usage(message, 137)
    assert usage["output_counters_agree"] is False
    assert usage["normalized"]["output_tokens"] == 5
    assert usage["provider"]["completion_tokens"] == 7


@pytest.mark.parametrize("configured,cap", [(137, 64), (32, 32)])
@pytest.mark.parametrize("finish", ["length", "stop"])
def test_token_limit_uses_lower_cap_and_separates_observation_from_truncation(
    stub, monkeypatch, capsys, configured, cap, finish,
):
    stub["responses"] = [completion(finish=finish, output_tokens=64)]
    config = settings(stub, max_output_tokens=configured)
    monkeypatch.setattr(probe, "load_settings", lambda: config)
    assert probe.main(["--run", "--mode", "token-limit"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["token_limit_response_observed"]
    assert report["length_finish_observed"] is (finish == "length")
    assert report["clients_closed"]
    assert len(report["observations"]) == len(stub["requests"]) == 1
    assert report["requests"][0]["max_completion_tokens"] == cap
    assert stub["requests"][0]["max_completion_tokens"] == cap
    assert "1 through 200" in stub["requests"][0]["messages"][1]["content"]
    limits = report["limits"]
    assert limits["configured_output_tokens"] == configured
    assert limits["requested_output_tokens"] == cap
    assert limits["http_timeout_seconds"] == config.request_timeout_seconds
    assert limits["total_timeout_seconds"] == config.run_timeout_seconds
    assert limits["automatic_retries"] == 0
    assert limits["max_model_calls"] == 1
    usage = report["observations"][0]["usage"]
    assert usage["reported_output_exceeds_requested"] is (64 > cap)
    assert usage["normalized"]["output_tokens"] == 64
    assert not report["hard_token_limit_verified"]
    assert not report["billing_verified"]
    assert "private-provider-response" not in json.dumps(report)


def test_token_limit_http_failure_is_not_an_observation(stub, monkeypatch, capsys):
    stub["status"] = 500
    monkeypatch.setattr(probe, "load_settings", lambda: settings(stub))
    assert probe.main(["--run", "--mode", "token-limit"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["token_limit_response_observed"]
    assert not report["length_finish_observed"]
    assert len(stub["requests"]) == 1
    assert report["clients_closed"]


@pytest.mark.parametrize("mode,outcome", [
    ("http-timeout", "http_timeout"), ("total-timeout", "total_timeout"),
])
def test_timeouts_are_distinguished_and_clients_closed(stub, mode, outcome):
    stub["delay"] = 0.1
    report = asyncio.run(probe.run_probe(settings(stub), mode))
    assert report["expected_timeout_observed"]
    assert report["observations"][0]["outcome"] == outcome
    assert report["clients_closed"]
    assert len(report["requests"]) <= 1
    assert len(stub["requests"]) <= 1
    assert report["limits"]["automatic_retries"] == 0
    assert not report["provider_server_cancellation_verified"]


def test_total_budget_is_shared_between_protocol_calls(stub):
    stub["delay"] = 0.2
    report = asyncio.run(probe.run_probe(settings(stub, run_timeout_seconds=0.3)))
    assert report["observations"][0]["protocol_valid"]
    assert report["observations"][1]["outcome"] == "total_timeout"
    assert not report["protocol_compatible"]
    assert report["clients_closed"]


def test_configured_call_budget_not_raised(stub):
    report = asyncio.run(probe.run_probe(settings(stub, max_model_calls=1)))
    assert len(stub["requests"]) == 1
    assert report["observations"][1]["outcome"] == "model_call_limit"
    assert not report["protocol_compatible"]


def test_http_error_details_redacted_and_no_retry(stub):
    stub["status"] = 500
    stub["responses"] = [{"error": {"message": "private-api-key private-provider-response"}}]
    report = asyncio.run(probe.run_probe(settings(stub, max_model_calls=1)))
    assert report["observations"][0]["outcome"] == "request_error"
    assert len(stub["requests"]) == 1
    assert "private-api-key" not in json.dumps(report)
    assert "private-provider-response" not in json.dumps(report)
    assert report["clients_closed"]


def test_main_requires_explicit_run_without_loading_settings(monkeypatch):
    monkeypatch.setattr(probe, "load_settings", lambda: pytest.fail("must not load credentials"))
    with pytest.raises(SystemExit) as exc:
        probe.main([])
    assert exc.value.code == 2


def test_main_reports_only_safe_configuration_failure(capsys):
    assert probe.main(["--run"]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "configuration_error"}
