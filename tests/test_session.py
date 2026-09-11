"""Session controller tests use offline Agent stubs, never a model or database."""

import asyncio
import os
import signal
import subprocess
import sys
from copy import deepcopy

import pytest
from pydantic import SecretStr

from db_agent import cli
from db_agent import session as session_module
from db_agent.agent import AgentResponseError
from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings, Settings
from db_agent.conversation_context import MAX_CONTEXT_BYTES, MAX_TURNS
from db_agent.presentation import AgentRunResult, QueryExecution
from db_agent.session import ConversationError, ConversationSession


def completed(*, rows=None):
    rows = [[1]] if rows is None else rows
    return QueryExecution("SELECT 1", {
        "status": "ok", "decision": "ALLOW", "execution_status": "completed", "error": None,
        "result": {
            "columns": [{"name": "n", "type": "int"}], "rows": rows,
            "row_count": len(rows), "truncated": False,
            "truncation_reason": None, "server_statement_status": "completed",
        },
    })


def observation(*queries, tools=None):
    return AgentRunResult(
        answer="可信的本轮回答", queries=list(queries), model_calls=4,
        tool_calls=tools if tools is not None else ["execute_query"] * len(queries),
    )


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    model = Settings.model_construct(
        api_key=SecretStr("offline-model-key"), openai_base_url="https://model.invalid/v1",
        model="offline-model",
    )
    database = DatabaseSettings.model_construct(
        password=SecretStr("offline-database-password"), allowed_tables=("orders",),
    )
    analysis = AnalysisSettings.model_construct()
    query = QuerySettings.model_construct()
    connections, calls = [], []

    class OfflineConnector:
        def __init__(self, settings):
            self.settings = settings.model_copy(deep=True)
            connections.append(self)

    async def offline_agent(prompt, settings, connector, record, analysis, query,
                            *, previous_requests=None):
        calls.append({
            "prompt": prompt, "settings": settings, "connector": connector,
            "record": record, "analysis": analysis, "query": query,
            "previous": deepcopy(previous_requests), "loop": asyncio.get_running_loop(),
        })
        return observation(completed())

    monkeypatch.setattr(session_module, "MetadataConnector", OfflineConnector)
    monkeypatch.setattr(session_module, "run_agent_observed", offline_agent)
    return model, database, analysis, query, connections, calls


def new_session(setup):
    return ConversationSession(*setup[:4])


def test_success_keeps_only_raw_user_requests_and_fresh_runtime_inputs(setup):
    session = new_session(setup)
    assert not setup[4] and not setup[5]
    first, second = "  查二月订单\t", "按客户拆分"
    with asyncio.Runner() as runner:
        runner.run(session.submit(first))
        runner.run(session.submit(second))
    assert session.requests == [first, second]
    assert session.turn_count == 2 and not session.needs_reset
    assert [call["previous"] for call in setup[5]] == [[], [first]]
    assert [call["prompt"] for call in setup[5]] == [first, second]
    assert setup[4][0] is not setup[4][1]
    assert setup[5][0]["loop"] is setup[5][1]["loop"]
    session.requests.append("outside mutation")
    assert session.requests == [first, second]
    assert "SELECT" not in repr(session)
    assert "二月" not in repr(session)


@pytest.mark.parametrize("rows", [[], [[1]]])
def test_complete_empty_or_nonempty_results_allow_continuation(setup, monkeypatch, rows):
    result = observation(completed(rows=rows))

    async def run(*args, **kwargs):
        return result

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    session = new_session(setup)
    assert asyncio.run(session.submit("查询")) is result
    assert session.requests == ["查询"] and not session.needs_reset


@pytest.mark.parametrize("kind", [
    "truncated", "rejected", "unknown", "not_started", "missing_result", "missing_truncated",
    "zero_truncated", "missing_report", "extra_report", "metadata_only", "mixed",
])
def test_ineligible_outcome_requires_reset_without_falling_back(setup, monkeypatch, kind):
    session = new_session(setup)
    asyncio.run(session.submit("先前成功请求"))
    query = completed()
    result = observation(query)
    if kind == "truncated":
        query.report["result"]["truncated"] = True
        query.report["execution_status"] = "truncated"
    elif kind == "rejected":
        query.report.update(status="rejected", decision="BLOCK", execution_status="not_started")
    elif kind == "unknown":
        query.report.update(status="error", decision="UNKNOWN", execution_status="unknown")
    elif kind == "not_started":
        query.report["execution_status"] = "not_started"
    elif kind == "missing_result":
        query.report["result"] = None
    elif kind == "missing_truncated":
        del query.report["result"]["truncated"]
    elif kind == "zero_truncated":
        query.report["result"]["truncated"] = 0
    elif kind == "missing_report":
        result = observation(query, tools=["execute_query", "execute_query"])
    elif kind == "extra_report":
        result = observation(query, completed(), tools=["execute_query"])
    elif kind == "metadata_only":
        result = observation(tools=["describe_table"])
    else:
        rejected = completed()
        rejected.report.update(status="rejected", decision="REVIEW")
        result = observation(query, rejected)
    attempts = []

    async def run(*args, **kwargs):
        attempts.append(True)
        return result

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    assert asyncio.run(session.submit("失败请求")) is result
    assert session.requests == ["先前成功请求"] and session.needs_reset
    with pytest.raises(ConversationError) as caught:
        asyncio.run(session.submit("不要退回旧口径继续"))
    assert caught.value.code == "SESSION_RESET_REQUIRED"
    assert attempts == [True] and len(setup[4]) == 2
    session.reset()
    assert session.requests == [] and session.turn_count == 0 and not session.needs_reset


def test_all_complete_queries_in_one_round_are_eligible(setup, monkeypatch):
    async def run(*args, **kwargs):
        return observation(completed(), completed(rows=[]))

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    session = new_session(setup)
    asyncio.run(session.submit("两项查询"))
    assert session.requests == ["两项查询"] and not session.needs_reset


@pytest.mark.parametrize("field,value", [
    ("columns", None), ("columns", []), ("columns", [{}]),
    ("columns", [{"name": "n", "type": 1}]),
    ("rows", None), ("rows", [[1, 2]]), ("rows", [1]),
    ("row_count", True), ("row_count", 0),
])
def test_incomplete_or_malformed_success_report_is_not_inherited(
    setup, monkeypatch, field, value,
):
    query = completed()
    query.report["result"][field] = value

    async def run(*args, **kwargs):
        return observation(query)

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    session = new_session(setup)
    asyncio.run(session.submit("表面成功的请求"))
    assert session.needs_reset and session.requests == []
    with pytest.raises(ConversationError) as caught:
        asyncio.run(session.submit("不得继承不完整结果"))
    assert caught.value.code == "SESSION_RESET_REQUIRED" and len(setup[4]) == 1


@pytest.mark.parametrize("error", [RuntimeError("private-error"), AgentResponseError("private")])
def test_exception_is_preserved_and_requires_reset(setup, monkeypatch, error):
    session = new_session(setup)
    asyncio.run(session.submit("先前请求"))

    async def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    with pytest.raises(type(error)) as caught:
        asyncio.run(session.submit("失败请求"))
    assert caught.value is error and session.needs_reset
    with pytest.raises(ConversationError, match="重置"):
        asyncio.run(session.submit("继续"))
    session.reset()  # Busy is released even on exceptions.
    assert not session.needs_reset and session.requests == []


def test_concurrent_submit_and_reset_reject_and_cancellation_finishes_cleanup(setup, monkeypatch):
    session = new_session(setup)
    cleanup = []

    async def scenario():
        started = asyncio.Event()

        async def run(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleanup.append("finished")

        monkeypatch.setattr(session_module, "run_agent_observed", run)
        task = asyncio.create_task(session.submit("正在查询"))
        await started.wait()
        with pytest.raises(ConversationError) as submit_error:
            await session.submit("并发请求")
        with pytest.raises(ConversationError) as reset_error:
            session.reset()
        with pytest.raises(ConversationError) as invalidate_error:
            session.require_reset()
        assert (
            submit_error.value.code == reset_error.value.code
            == invalidate_error.value.code == "SESSION_BUSY"
        )
        assert len(setup[4]) == 1 and not session.needs_reset
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup == ["finished"] and session.needs_reset
        with pytest.raises(ConversationError) as caught:
            await session.submit("不得继续")
        assert caught.value.code == "SESSION_RESET_REQUIRED"
        session.reset()

    asyncio.run(scenario())
    assert session.requests == [] and not session.needs_reset


def test_configuration_and_session_instances_are_isolated(setup):
    first = new_session(setup)
    setup[0].model = "changed-model"
    setup[1].allowed_tables = ("other_table",)
    setup[2].review_scan_rows = 9
    setup[3].max_rows = 7
    second = new_session(setup)
    asyncio.run(first.submit("first"))
    asyncio.run(second.submit("second"))
    one, two = setup[5]
    assert one["settings"].model == "offline-model"
    assert one["connector"].settings.allowed_tables == ("orders",)
    assert one["analysis"].review_scan_rows == 100000 and one["query"].max_rows == 100
    assert two["settings"].model == "changed-model"
    assert two["connector"].settings.allowed_tables == ("other_table",)
    assert two["analysis"].review_scan_rows == 9 and two["query"].max_rows == 7
    one["settings"].max_model_calls = 10
    one["analysis"].review_scan_rows = 1
    one["query"].max_rows = 1
    asyncio.run(first.submit("follow-up"))
    assert setup[5][-1]["settings"].max_model_calls == 4
    assert setup[5][-1]["analysis"].review_scan_rows == 100000
    assert setup[5][-1]["query"].max_rows == 100
    assert setup[5][-1]["previous"] == ["first"]
    first.reset()
    assert first.requests == [] and second.requests == ["second"]


@pytest.mark.parametrize("prompt", ["", " \t", 1, "x" * (MAX_CONTEXT_BYTES + 1)])
def test_invalid_input_stops_before_runtime_or_connector(setup, prompt):
    session = new_session(setup)
    with pytest.raises(ValueError):
        asyncio.run(session.submit(prompt))
    assert not setup[4] and not setup[5] and session.needs_reset


def test_turn_limit_does_not_silently_drop_history(setup):
    session = new_session(setup)
    with asyncio.Runner() as runner:
        for n in range(MAX_TURNS):
            runner.run(session.submit(f"request {n}"))
        with pytest.raises(ValueError):
            runner.run(session.submit("超出轮数"))
    assert len(setup[5]) == MAX_TURNS and session.turn_count == MAX_TURNS
    assert session.needs_reset


def test_require_reset_retains_requests_but_blocks_submissions(setup):
    session = new_session(setup)
    asyncio.run(session.submit("已成功请求"))
    session.require_reset()
    assert session.needs_reset and session.requests == ["已成功请求"]
    with pytest.raises(ConversationError) as caught:
        asyncio.run(session.submit("不得继续"))
    assert caught.value.code == "SESSION_RESET_REQUIRED"
    assert len(setup[5]) == len(setup[4]) == 1
    session.reset()
    asyncio.run(session.submit("完整重述"))
    assert setup[5][-1]["previous"] == [] and not session.needs_reset


@pytest.fixture
def terminal(setup, monkeypatch):
    for name, value in zip(("settings", "database_settings", "analysis_settings", "query_settings"),
                           setup[:4]):
        monkeypatch.setattr(cli, f"load_{name}", lambda value=value: value)
    sessions, records, lifecycle = [], [], []
    original = ConversationSession

    def factory(*args, **kwargs):
        session = original(*args, **kwargs)
        sessions.append(session)
        return session

    class OfflineRecord:
        def __enter__(self):
            records.append(self)
            lifecycle.append("start")
            return self

        def __exit__(self, *args):
            lifecycle.append("end")

    monkeypatch.setattr(cli, "ConversationSession", factory)
    monkeypatch.setattr(cli, "RunRecord", OfflineRecord)

    def lines(items):
        iterator = iter(items)

        def read(_prompt):
            item = next(iterator, EOFError())
            if isinstance(item, BaseException):
                raise item
            return item

        monkeypatch.setattr("builtins.input", read)

    return sessions, records, lifecycle, lines


@pytest.mark.parametrize("inputs,exit_code", [
    ([], 0), (["/exit"], 0), (["/help", "", "/reset", "/unknown", "/exit"], 0),
    ([KeyboardInterrupt()], 130),
])
def test_cli_idle_commands_do_not_start_runs(setup, terminal, inputs, exit_code):
    sessions, records, lifecycle, lines = terminal
    lines(inputs)
    assert cli.main(["session"]) == exit_code
    assert not records and not lifecycle and not setup[4] and not setup[5]
    assert sessions[0].requests == [] and not sessions[0].needs_reset


def test_cli_uses_one_loop_distinct_records_and_resets_on_exit(setup, terminal, capsys):
    sessions, records, lifecycle, lines = terminal
    lines(["第一轮", "第二轮", "/reset", "重述请求"])
    assert cli.main(["session"]) == 0
    assert [call["previous"] for call in setup[5]] == [[], ["第一轮"], []]
    assert len({id(call["loop"]) for call in setup[5]}) == 1
    assert [call["record"] for call in setup[5]] == records
    assert len({id(record) for record in records}) == 3
    assert lifecycle == ["start", "end"] * 3
    assert capsys.readouterr().out.count("可信的本轮回答") == 3
    assert sessions[0].requests == []


def test_cli_failed_turn_requires_reset_and_keeps_error_observation(
    setup, terminal, monkeypatch, capsys,
):
    calls = []

    async def run(*args, **kwargs):
        calls.append(kwargs["previous_requests"])
        if len(calls) == 1:
            raise AgentResponseError(
                "private-provider-secret", observation=observation(completed()),
            )
        return observation(completed())

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    terminal[3](["失败请求", "不能沿用", "/reset", "完整重述", "/exit"])
    assert cli.main(["session"]) == 0
    captured = capsys.readouterr()
    assert calls == [[], []]
    assert "返回 1 行" in captured.out
    assert "private-provider-secret" not in captured.out + captured.err
    assert "/reset" in captured.err and "完整" in captured.err
    assert not terminal[0][0].needs_reset


def test_cli_cancel_waits_for_cleanup_before_accepting_commands(
    setup, terminal, monkeypatch, capsys,
):
    cleaned = []

    async def run(*args, **kwargs):
        try:
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    terminal[3](["取消请求", "失败后不发送", "/reset", "/exit"])
    assert cli.main(["session"]) == 0
    assert cleaned == [True] and len(setup[4]) == 1
    assert terminal[2] == ["start", "end", "start", "end"]
    captured = capsys.readouterr()
    assert "/reset" in captured.err and "已取消 SQL" not in captured.err


def test_cli_generic_exception_is_sanitized(setup, terminal, monkeypatch, capsys):
    async def run(*args, **kwargs):
        raise RuntimeError("offline-model-key private-prompt-marker")

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    terminal[3](["请求", "/exit"])
    assert cli.main(["session"]) == 0
    output = capsys.readouterr()
    assert "offline-model-key" not in output.out + output.err
    assert "private-prompt-marker" not in output.out + output.err
    assert "/reset" in output.err


def test_cli_incomplete_report_is_displayed_but_cannot_continue(
    setup, terminal, monkeypatch, capsys,
):
    calls = []

    async def run(*args, **kwargs):
        calls.append(kwargs["previous_requests"])
        query = completed()
        if len(calls) == 1:
            query.report["result"]["truncated"] = True
            query.report["execution_status"] = "truncated"
        return observation(query)

    monkeypatch.setattr(session_module, "run_agent_observed", run)
    terminal[3](["部分结果", "禁止沿用", "/reset", "重新描述", "/exit"])
    assert cli.main(["session"]) == 0
    output = capsys.readouterr()
    assert calls == [[], []]
    assert output.out.count("可信的本轮回答") == 2
    assert "未取得完整成功" in output.err


@pytest.mark.parametrize("exception", [KeyboardInterrupt, ValueError, RuntimeError])
def test_cli_answer_output_failure_requires_explicit_reset(
    setup, terminal, monkeypatch, capsys, exception,
):
    original_print = print
    failed = []

    def fail_first_answer(*args, **kwargs):
        if args == ("可信的本轮回答",) and not failed:
            failed.append(True)
            raise exception("private-output-error")
        return original_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", fail_first_answer)
    commands = iter(["第一轮", "禁止沿用", "/reset", "完整重述", "/exit"])

    def read(_prompt):
        command = next(commands)
        if command == "禁止沿用":
            assert terminal[0][0].needs_reset
            assert terminal[0][0].requests == ["第一轮"]
        if command == "/reset":
            assert len(setup[5]) == 1
        if command == "完整重述":
            assert not terminal[0][0].needs_reset and terminal[0][0].requests == []
        return command

    monkeypatch.setattr("builtins.input", read)
    assert cli.main(["session"]) == 0
    assert failed == [True]
    assert [call["prompt"] for call in setup[5]] == ["第一轮", "完整重述"]
    assert [call["previous"] for call in setup[5]] == [[], []]
    assert len(setup[4]) == 2 and terminal[0][0].requests == []
    output = capsys.readouterr()
    assert "/reset" in output.err
    assert "private-output-error" not in output.out + output.err


@pytest.mark.skipif(not hasattr(signal, "SIGINT") or os.name == "nt", reason="POSIX SIGINT")
def test_runner_sigint_cancels_and_waits_before_reset_without_poisoning_next_run(tmp_path):
    # A child process exercises Runner's real SIGINT handler without interrupting pytest.
    # Both Agent turns are offline stubs; no client, model or database is constructed.
    code = '''
import asyncio
import os
import signal
from pydantic import SecretStr
from db_agent import session as module
from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings, Settings
from db_agent.presentation import AgentRunResult, QueryExecution

cleaned = []
module.MetadataConnector = lambda _: object()
async def cancel(*args, **kwargs):
    try:
        asyncio.get_running_loop().call_soon(os.kill, os.getpid(), signal.SIGINT)
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0)
        cleaned.append(True)
module.run_agent_observed = cancel
session = module.ConversationSession(
    Settings.model_construct(api_key=SecretStr("offline"),
        openai_base_url="https://model.invalid", model="offline"),
    DatabaseSettings.model_construct(password=SecretStr("offline")),
    AnalysisSettings.model_construct(), QuerySettings.model_construct(),
)
with asyncio.Runner() as runner:
    try:
        runner.run(session.submit("cancel"))
        raise AssertionError("SIGINT did not interrupt")
    except KeyboardInterrupt:
        assert cleaned == [True] and session.needs_reset
    session.reset()
    async def complete(*args, **kwargs):
        query = QueryExecution("SELECT 1", {"status": "ok", "decision": "ALLOW",
            "execution_status": "completed", "result": {"truncated": False,
                "columns": [{"name": "n", "type": "int"}], "rows": [[1]], "row_count": 1}})
        return AgentRunResult("ok", [query], 1, ["execute_query"])
    module.run_agent_observed = complete
    runner.run(session.submit("new request"))
    assert session.requests == ["new request"] and not session.needs_reset
session.reset()
assert session.requests == []
print("CLEANUP_AND_REUSE_OK")
'''
    child = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "CLEANUP_AND_REUSE_OK"
