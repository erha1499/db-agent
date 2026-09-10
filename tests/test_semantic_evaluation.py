"""Offline evaluation behavior: real review middleware, fake metadata and model transport."""

import asyncio
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import APITimeoutError

from db_agent import evaluation
from db_agent.agent import AgentResponseError
from db_agent.config import AnalysisSettings, DatabaseSettings, Settings
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.intents import QueryIntent
from db_agent.semantics import SemanticReview

SPEC = importlib.util.spec_from_file_location(
    "evaluate_semantics",
    Path(__file__).resolve().parents[1] / "scripts/evaluate_semantics.py",
)
semantic_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(semantic_eval)


def response(verdict="match", replacement=None):
    payload = {
        "verdict": verdict,
        "checks": dict.fromkeys(
            ("scope", "filters", "time", "aggregation", "columns", "ordering"),
            "stub evidence",
        ),
        "issues": [] if verdict == "match" else ["stub issue"],
        "replacement_sql": replacement,
    }
    return {
        "raw": AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "SemanticReview",
                    "args": payload,
                    "id": "synthetic-call",
                }
            ],
            response_metadata={"finish_reason": "tool_calls"},
        ),
        "parsed": SemanticReview.model_validate(payload),
        "parsing_error": None,
    }


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.upper().startswith(("DB_AGENT_", "OPENAI_", "LANGCHAIN_", "LANGSMITH_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def limits():
    return SimpleNamespace(
        database=DatabaseSettings(
            _env_file=None,
            password="synthetic-reader-secret",
            allowed_tables=tuple(sorted(evaluation.TABLES)),
        ),
        analysis=AnalysisSettings(_env_file=None),
        settings=Settings(
            _env_file=None,
            api_key="synthetic-model-secret",
            model="synthetic-model-alias",
            openai_base_url="http://127.0.0.1:1/synthetic/v1",
        ),
    )


@pytest.fixture
def cases(limits):
    return {case["id"]: case for case in semantic_eval.load_cases(limits.analysis)[1]}


@pytest.fixture
def fake_io(monkeypatch):
    state = SimpleNamespace(
        clients=[], models=[], requests=[], metadata=[], responses=[], wait=False
    )

    class SyncClient:
        def __init__(self):
            self.closed = False
            state.clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    class AsyncClient(SyncClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    class Reviewer:
        async def ainvoke(self, messages):
            state.requests.append(messages)
            if state.wait:
                await asyncio.sleep(10)
            result = state.responses.pop(0) if state.responses else response()
            if isinstance(result, BaseException):
                raise result
            return result

    class Model:
        def __init__(self, **kwargs):
            state.models.append(kwargs)

        def with_structured_output(self, schema, **kwargs):
            assert schema in (SemanticReview, QueryIntent)
            assert kwargs["method"] == "function_calling" and kwargs["include_raw"] is True
            if schema is QueryIntent:
                # This runner diagnoses the review component only. Binding an
                # interpreter is harmless; invoking it would change its scope.
                class UnusedInterpreter:
                    async def ainvoke(self, messages):
                        pytest.fail("review component unexpectedly extracted an intent")
                return UnusedInterpreter()
            return Reviewer()

    async def describe(connector, table):
        state.metadata.append(table)
        return {"database": "db_agent", "table": table, "columns": [{"name": "id", "type": "int"}]}

    async def forbidden(*args, **kwargs):
        pytest.fail("semantic evaluation attempted database work beyond controlled metadata")

    monkeypatch.setattr(semantic_eval, "DefaultHttpxClient", SyncClient)
    monkeypatch.setattr(semantic_eval, "DefaultAsyncHttpxClient", AsyncClient)
    monkeypatch.setattr(semantic_eval, "ChatOpenAI", Model)
    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    for name in ("execute_checked", "explain_checked", "_connection"):
        monkeypatch.setattr(MetadataConnector, name, forbidden)
    return state


def run(case, limits):
    return asyncio.run(
        semantic_eval.run_case(
            case,
            connector=MetadataConnector(limits.database),
            analysis=limits.analysis,
            settings=limits.settings,
        )
    )


def evaluate(limits, repeat=1):
    return asyncio.run(
        semantic_eval.evaluate(
            repeat=repeat,
            database=limits.database,
            analysis=limits.analysis,
            settings=limits.settings,
        )
    )


def test_fixed_suite_has_distinct_public_cases_and_scope_is_derived_offline(limits):
    manifest, cases = semantic_eval.load_cases(limits.analysis)
    assert len(cases) == len({case["id"] for case in cases}) > 0
    assert manifest["role"] == "exposed_development_regression"
    assert manifest["case_file_sha256"] == semantic_eval._digest(
        semantic_eval.CASE_FILE.read_bytes()
    )
    for case in cases:
        checked = MetadataConnector(limits.database).check_sql(case["sql"], limits.analysis)
        assert checked.decision == "ALLOW"
        assert case["tables"] == list(checked.tables)


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "remote.example"),
        ("host", "localhost"),
        ("port", 3306),
        ("database", "other"),
        ("user", "other_reader"),
        ("allowed_tables", ()),
    ],
)
def test_wrong_target_or_missing_permission_stops_before_metadata_or_model(
    limits,
    fake_io,
    field,
    value,
):
    limits.database = limits.database.model_copy(update={field: value})
    with pytest.raises(evaluation.EvaluationError):
        evaluate(limits)
    assert fake_io.models == fake_io.requests == fake_io.metadata == []


def test_evaluator_cannot_relax_plan_threshold_or_change_frozen_budgets(limits, fake_io):
    limits.analysis = limits.analysis.model_copy(update={"review_scan_rows": 100001})
    with pytest.raises(evaluation.EvaluationError):
        evaluate(limits)
    limits.analysis = AnalysisSettings(_env_file=None)
    limits.settings = limits.settings.model_copy(update={"max_model_calls": 5})
    with pytest.raises(evaluation.EvaluationError):
        evaluate(limits)
    assert fake_io.models == []


def test_original_review_uses_actual_middleware_once_and_never_executes_sql(limits, cases, fake_io):
    case = cases["where_customer_match"]
    result = run(case, limits)
    assert result["detection_passed"] is True
    assert result["actual_verdict"] == "match" and result["model_calls"] == 1
    assert result["tool_calls"] == ["describe_table"]
    assert len(fake_io.requests) == 1 and fake_io.metadata == ["ec_orders"]
    payload = json.loads(fake_io.requests[0][1].content)
    assert set(payload) == {"user_request", "candidate_sql", "schemas"}
    assert payload["candidate_sql"] == case["sql"] and payload["user_request"] == case["prompt"]
    assert payload["schemas"][0]["table"] == "ec_orders"
    assert result["input_sha256"] == semantic_eval._input(
        case["sql"], case["prompt"], payload["schemas"]
    )
    assert len(fake_io.clients) == 2 and all(client.closed for client in fake_io.clients)
    model = fake_io.models[0]
    assert model["max_retries"] == 0 and model["streaming"] is False
    assert model["use_responses_api"] is False
    assert model["base_url"] == limits.settings.openai_base_url
    assert model["api_key"] == limits.settings.api_key


def test_mismatch_without_repair_still_counts_as_successful_detection(limits, cases, fake_io):
    fake_io.responses = [response("mismatch")]
    result = run(cases["where_customer_mismatch"], limits)
    assert result["detection_passed"] is True
    assert result["repair"]["suggested"] is False
    assert result["repair"]["passed"] is None
    assert result["model_calls"] == 1


def test_repair_is_rechecked_in_independent_context_without_a_third_review(limits, cases, fake_io):
    corrected = cases["where_customer_match"]["sql"]
    fake_io.responses = [
        response("mismatch", corrected),
        response("mismatch", "SELECT id FROM ec_orders"),
    ]
    result = run(cases["where_customer_mismatch"], limits)
    assert result["detection_passed"] is True
    assert result["repair"]["static_decision"] == "ALLOW"
    assert result["repair"]["attempted"] is True and result["repair"]["passed"] is False
    assert result["model_calls"] == 2 and result["tool_calls"] == ["describe_table"]
    second = json.loads(fake_io.requests[1][1].content)
    assert second["candidate_sql"] == corrected
    assert second["user_request"] == cases["where_customer_mismatch"]["prompt"]
    assert "stub issue" not in fake_io.requests[1][1].content
    assert result["input_sha256"] != result["repair"]["input_sha256"]


@pytest.mark.parametrize("replacement", ["DELETE FROM ec_orders", "SELECT id FROM orders"])
def test_invalid_or_unauthorized_repair_never_reaches_metadata_or_a_second_review(
    limits,
    cases,
    fake_io,
    replacement,
):
    fake_io.responses = [response("mismatch", replacement)]
    result = run(cases["where_customer_mismatch"], limits)
    assert result["detection_passed"] is True
    assert result["repair"]["suggested"] is True and result["repair"]["passed"] is False
    assert result["repair"]["static_decision"] != "ALLOW"
    assert result["repair"]["attempted"] is False and len(fake_io.requests) == 1


def test_repair_failure_does_not_erase_original_detection(limits, cases, fake_io):
    fake_io.responses = [
        response("mismatch", cases["where_customer_match"]["sql"]),
        RuntimeError("private provider error with password=synthetic-model-secret"),
    ]
    result = run(cases["where_customer_mismatch"], limits)
    assert result["detection_passed"] is True and result["error_code"] is None
    assert result["repair"]["error_code"] == "SEMANTIC_REVIEW_FAILED"
    assert "private provider" not in json.dumps(result)
    assert "synthetic-model-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "failure,code",
    [
        (RuntimeError("private upstream body"), "SEMANTIC_REVIEW_FAILED"),
        (AgentResponseError("private unspecified detail"), "AGENT_RESPONSE_ERROR"),
        (AgentResponseError("private timeout detail", code="TIMEOUT"), "TIMEOUT"),
        (AgentResponseError("private model detail", code="MODEL_CALL_LIMIT"), "MODEL_CALL_LIMIT"),
        (AgentResponseError("private tool detail", code="TOOL_CALL_LIMIT"), "TOOL_CALL_LIMIT"),
        (AgentResponseError("private graph detail", code="GRAPH_RECURSION_LIMIT"),
         "GRAPH_RECURSION_LIMIT"),
        (AgentResponseError("private unknown detail", code="private_unknown_code"),
         "AGENT_RESPONSE_ERROR"),
        (APITimeoutError(request=httpx.Request("POST", "http://invalid.test/private")), "TIMEOUT"),
    ],
)
def test_errors_are_counted_and_sanitized_with_owned_client_cleanup(
    limits, cases, fake_io, failure, code
):
    fake_io.responses = [failure]
    result = run(cases["where_customer_match"], limits)
    assert result["detection_passed"] is False and result["error_code"] == code
    assert "private" not in json.dumps(result)
    assert all(client.closed for client in fake_io.clients)


def test_repair_timeout_keeps_original_detection_and_reports_timeout(limits, cases, fake_io):
    fake_io.responses = [
        response("mismatch", cases["where_customer_match"]["sql"]),
        AgentResponseError("private timeout detail", code="TIMEOUT"),
    ]
    result = run(cases["where_customer_mismatch"], limits)
    assert result["detection_passed"] is True and result["error_code"] is None
    assert result["repair"]["attempted"] is True and result["repair"]["passed"] is False
    assert result["repair"]["error_code"] == "TIMEOUT"
    assert result["model_calls"] == len(fake_io.requests) == 2
    assert "private" not in json.dumps(result)
    assert all(client.closed for client in fake_io.clients)


def test_invalid_review_is_not_reported_as_a_semantic_mismatch(limits, cases, fake_io):
    invalid = response()
    invalid["raw"].response_metadata["finish_reason"] = "length"
    fake_io.responses = [invalid]
    result = run(cases["where_customer_match"], limits)
    assert result["actual_verdict"] is None and result["error_code"] == "SEMANTIC_REVIEW_INVALID"


def test_total_deadline_and_cancellation_keep_cleanup_and_never_return_success(
    limits, cases, fake_io
):
    fake_io.wait = True
    limits.settings = limits.settings.model_copy(update={"run_timeout_seconds": 0.01})
    result = run(cases["where_customer_match"], limits)
    assert result["error_code"] == "RUN_TIMEOUT" and not result["detection_passed"]
    assert all(client.closed for client in fake_io.clients)
    fake_io.wait = False
    fake_io.responses = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        run(cases["where_customer_match"], limits)
    assert all(client.closed for client in fake_io.clients)


def test_all_repetitions_and_failures_are_retained_with_explicit_denominators(
    limits, cases, fake_io, capsys
):
    report = evaluate(limits, repeat=2)
    total = len(cases) * 2
    matched = sum(case["expected_verdict"] == "match" for case in cases.values())
    assert report["task_count"] == len(cases)
    assert (
        report["attempt_count"]
        == report["completed_attempt_count"]
        == len(report["attempts"])
        == total
    )
    assert report["detection_passed_count"] == matched * 2
    assert report["detection_failed_count"] == total - matched * 2
    assert report["repair_suggested_count"] == report["repair_review_attempt_count"] == 0
    assert {item["iteration"] for item in report["attempts"]} == {1, 2}
    assert len({item["run_id"] for item in report["attempts"]}) == total
    assert len(fake_io.requests) == total and len(fake_io.clients) == total * 2
    assert all(client.closed for client in fake_io.clients)
    assert report["model"]["configured_model"] == "synthetic-model-alias"
    assert report["model"]["endpoint_sha256"] == semantic_eval._digest(
        limits.settings.openai_base_url.encode()
    )
    assert "scripts/evaluate_semantics.py" in report["source"]["files"]
    serialized = json.dumps(report)
    output = capsys.readouterr().out
    for secret in (
        "synthetic-reader-secret",
        "synthetic-model-secret",
        limits.settings.openai_base_url,
    ):
        assert secret not in serialized + output
    assert len(output.splitlines()) == total and all(
        ": PASS" in line or ": FAIL" in line for line in output.splitlines()
    )
    assert "SELECT" not in output and "stub evidence" not in output


@pytest.mark.parametrize(
    "argv", [[], ["--repeat", "1", "--case-file", "other.json"], ["--target", "other"]]
)
def test_cli_requires_repeat_and_has_no_arbitrary_case_or_target_options(argv):
    with pytest.raises(SystemExit) as error:
        semantic_eval.main(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("repeat", [0, 21, True])
def test_repeat_range_is_rejected_before_any_io(limits, fake_io, repeat):
    with pytest.raises(evaluation.EvaluationError):
        evaluate(limits, repeat)
    assert fake_io.models == fake_io.metadata == []


def test_report_writer_uses_exclusive_private_uuid_path(
    limits, cases, fake_io, monkeypatch, tmp_path,
):
    report = evaluate(limits)
    monkeypatch.setattr(evaluation, "PROJECT_ROOT", tmp_path)
    path = semantic_eval.write_report(report)
    assert path == tmp_path / "outputs" / "evals" / f"{report['evaluation_id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["attempt_count"] == len(cases)
    with pytest.raises(FileExistsError):
        semantic_eval.write_report(report)


def test_detection_and_repair_denominators_are_independent(limits, cases, fake_io):
    for case in cases.values():
        if case["expected_verdict"] == "mismatch":
            fake_io.responses.extend([
                response("mismatch", "SELECT id FROM ec_orders"), response("match"),
            ])
        else:
            fake_io.responses.append(response(case["expected_verdict"]))
    report = evaluate(limits)
    repairs = sum(case["expected_verdict"] == "mismatch" for case in cases.values())
    assert report["detection_passed_count"] == report["attempt_count"] == len(cases)
    assert report["repair_suggested_count"] == report["repair_review_attempt_count"] == repairs
    assert report["repair_passed_count"] == repairs and report["repair_failed_count"] == 0
    assert len(fake_io.requests) == len(cases) + repairs


def test_cli_setup_errors_never_echo_exception_text(monkeypatch, capsys):
    def failed():
        raise DatabaseError("sensitive-code", "private endpoint and password")

    monkeypatch.setattr(semantic_eval, "load_database_settings", failed)
    assert semantic_eval.main(["--repeat", "1"]) == 1
    output = capsys.readouterr().out
    assert "private" not in output and "sensitive-code" not in output
