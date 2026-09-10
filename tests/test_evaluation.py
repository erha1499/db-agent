"""Offline evaluator tests: constructed traces, never a real model or database."""

import asyncio
import copy
import json
import os
from types import SimpleNamespace

import pytest

from db_agent import evaluation
from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings, Settings
from db_agent.db import MetadataConnector
from db_agent.presentation import AgentRunResult, QueryExecution, render_queries


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.upper().startswith("DB_AGENT_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def limits():
    return SimpleNamespace(
        database=DatabaseSettings(
            _env_file=None, password="synthetic-reader",
            allowed_tables=tuple(sorted(evaluation.TABLES)),
        ),
        analysis=AnalysisSettings(_env_file=None),
        query=QuerySettings(_env_file=None),
        model=Settings(
            _env_file=None, api_key="synthetic-model-key", model="offline-stub",
            openai_base_url="http://127.0.0.1:1/v1",
        ),
    )


@pytest.fixture
def case():
    return copy.deepcopy(evaluation.load_cases("dev")[1][0])


def report_for(case):
    expected = case["expected"]
    rows = copy.deepcopy(expected["rows"])
    return {
        "status": expected["status"], "decision": expected["decision"],
        "execution_status": expected["execution_status"], "error": None,
        "query_id": "offline-query", "server_version": "8.4.11",
        "sql_fingerprint": "0" * 64, "findings": [],
        "result": None if rows is None else {
            "columns": [{"name": f"alias_{n}", "type": "decimal"}
                        for n in range(len(rows[0]) if rows else 1)],
            "rows": rows, "row_count": len(rows), "truncated": expected["truncated"],
            "truncation_reason": "row_limit" if expected["truncated"] else None,
            "server_statement_status": "unknown" if expected["truncated"] else "completed",
            "result_bytes": 300,
        },
    }


def run_case(case, limits, mode):
    return asyncio.run(evaluation.run_case(
        case, mode=mode, connector=MetadataConnector(limits.database),
        analysis=limits.analysis, query=limits.query,
        model=limits.model if mode == "agent" else None,
    ))


def test_public_splits_are_disjoint_and_sql_respects_policy(limits):
    dev_manifest, dev = evaluation.load_cases("dev")
    frozen_manifest, holdout = evaluation.load_cases("holdout")
    assert len(dev) == len(holdout) == 8
    assert {case["id"] for case in dev}.isdisjoint(case["id"] for case in holdout)
    assert dev_manifest["case_file_sha256"] == frozen_manifest["case_file_sha256"]
    assert dev_manifest["selected_cases_sha256"] != frozen_manifest["selected_cases_sha256"]
    connector = MetadataConnector(limits.database)
    for case in dev + holdout:
        checked = connector.check_sql(case["sql"], limits.analysis)
        expected = case["expected"]["decision"]
        assert checked.decision == ("ALLOW" if expected == "REVIEW" else expected), case["id"]


def test_v2_preserves_v1_and_marks_both_previous_splits_as_exposed(limits):
    legacy_bytes = evaluation.CASE_FILE.read_bytes()
    assert evaluation._digest(legacy_bytes) == (
        "250cd649e8fe7dce2be92deeb6bc46176d4d213a01091e1f272263f741850f84"
    )
    original = json.loads(legacy_bytes)["cases"]
    dev_manifest, dev = evaluation.load_cases("dev", suite="v2")
    holdout_manifest, holdout = evaluation.load_cases("holdout", suite="v2")
    assert len(dev) == 16 and len(holdout) == 8
    assert {item["id"] for item in dev}.isdisjoint(item["id"] for item in holdout)
    assert [
        {key: value for key, value in item.items() if key != "business_context"} for item in dev
    ] == [dict(item, split="dev") for item in original]
    assert dev_manifest["split_role"] == "exposed_regression"
    assert holdout_manifest["split_role"] == "exposed_regression"
    assert holdout_manifest["suite_version"] == "ecommerce-eval-v2"
    assert holdout_manifest["dataset_version"] == "ecommerce-v1"
    assert holdout_manifest["required_dataset"]["seed"] == 20260910
    assert dev_manifest["business_context_sha256"] == holdout_manifest["business_context_sha256"]
    assert dev_manifest["inputs_sha256"] != holdout_manifest["inputs_sha256"]
    connector = MetadataConnector(limits.database)
    for item in holdout:
        assert connector.check_sql(item["sql"], limits.analysis).decision == (
            item["expected"]["decision"]
        ), item["id"]


def test_legacy_load_api_defaults_to_v1_and_marks_old_holdout_as_exposed():
    default, cases = evaluation.load_cases("holdout")
    explicit, explicit_cases = evaluation.load_cases("holdout", evaluation.CASE_FILE, suite="v1")
    assert default == explicit and cases == explicit_cases
    assert default["suite_version"] == "ecommerce-eval-v1"
    assert default["split_role"] == "exposed_regression"


@pytest.mark.parametrize(
    "suite,previous,dev_count", [("v3", "v2", 24), ("v4", "v3", 32), ("v5", "v4", 40)],
)
def test_later_suites_preserve_exposed_cases_and_use_independent_new_tasks(
    limits, suite, previous, dev_count,
):
    previous_bytes = evaluation.SUITE_FILES[previous].read_bytes()
    previous_cases = json.loads(previous_bytes)["cases"]
    dev_manifest, dev = evaluation.load_cases("dev", suite=suite)
    holdout_manifest, holdout = evaluation.load_cases("holdout", suite=suite)
    assert len(dev) == dev_count and len(holdout) == 8
    assert [
        {key: value for key, value in item.items() if key != "business_context"} for item in dev
    ] == [dict(item, split="dev") for item in previous_cases]
    assert {item["id"] for item in dev}.isdisjoint(item["id"] for item in holdout)
    assert dev_manifest["split_role"] == "exposed_regression"
    assert holdout_manifest["split_role"] == "exposed_regression"
    assert dev_manifest["business_context_sha256"] == holdout_manifest["business_context_sha256"]
    for item in holdout:
        assert MetadataConnector(limits.database).check_sql(
            item["sql"], limits.analysis,
        ).decision == "ALLOW", item["id"]
    if previous == "v3":
        assert evaluation._digest(previous_bytes) == (
            "b79c705826029a286a2698ac42ed8ecb39eddd921e4b102ed1b05acca3100d92"
        )
    if previous == "v4":
        assert evaluation._digest(previous_bytes) == (
            "71d0b7b92006299b6d7a16edc19af8c5c69ce2309de3489137013e506adf4baa"
        )


@pytest.mark.parametrize("suite", ["../ecommerce-v2", "v6", "ecommerce-v2.json"])
def test_suite_selection_accepts_only_fixed_names(suite):
    with pytest.raises(evaluation.EvaluationError, match="suite"):
        evaluation.load_cases("dev", suite=suite)


def test_v2_business_dictionary_and_hash_bind_the_exact_agent_input(limits, monkeypatch):
    from db_agent import agent

    manifest, cases = evaluation.load_cases("dev", suite="v2")
    case = cases[0]
    queries = [QueryExecution(case["sql"], report_for(case))]
    calls = []

    async def observed(prompt, *args):
        calls.append(prompt)
        return AgentRunResult(render_queries(queries), queries, 2, ["execute_query"])

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert actual["passed"]
    assert calls == [case["business_context"] + "\n\n" + case["prompt"]]
    assert "succeeded" in calls[0] and case["sql"] not in calls[0]
    assert actual["input_sha256"] == actual["prompt_sha256"] == (
        evaluation._digest(calls[0].encode())
    )
    assert manifest["business_context_sha256"] == evaluation._digest(
        case["business_context"].encode()
    )


def test_v2_report_records_suite_role_and_the_selected_attempt_denominator(limits, monkeypatch):
    async def fake_case(case, **kwargs):
        return {"case_id": case["id"], "passed": True, "categories": []}

    monkeypatch.setattr(evaluation, "run_case", fake_case)
    report = asyncio.run(evaluation.evaluate(
        suite="v2", mode="sql", split="holdout", repeat=2, database=limits.database,
        analysis=limits.analysis, query=limits.query,
    ))
    assert report["suite"] == "v2" and report["suite_version"] == "ecommerce-eval-v2"
    assert report["split_role"] == "exposed_regression"
    assert report["task_count"] == 8 and report["attempt_count"] == 16


def test_environment_only_restricts_existing_authorization(limits, case):
    original = limits.database.model_copy(update={"allowed_tables": ("ec_orders", "orders")})
    scoped = evaluation.validate_environment(original, limits.analysis, [case])
    assert scoped.allowed_tables == ("ec_orders",)
    assert original.allowed_tables == ("ec_orders", "orders")
    with pytest.raises(evaluation.EvaluationError, match="不会扩大权限"):
        evaluation.validate_environment(
            original.model_copy(update={"allowed_tables": ()}), limits.analysis, [case],
        )


@pytest.mark.parametrize(("field", "value"), [
    ("host", "localhost"), ("host", "remote.example"), ("port", 3306),
    ("database", "other"), ("user", "another_reader"),
])
def test_exact_local_target_is_required(limits, case, field, value):
    with pytest.raises(evaluation.EvaluationError, match="固定本地"):
        evaluation.validate_environment(
            limits.database.model_copy(update={field: value}), limits.analysis, [case],
        )


@pytest.mark.parametrize("field", ["review_scan_rows", "review_join_rows", "review_sort_rows"])
def test_review_thresholds_cannot_be_relaxed(limits, case, field):
    changed = limits.analysis.model_copy(update={field: getattr(limits.analysis, field) + 1})
    with pytest.raises(evaluation.EvaluationError, match="不能放宽"):
        evaluation.validate_environment(limits.database, changed, [case])


def test_unordered_results_preserve_duplicate_multiplicity(case):
    case["expected"]["rows"] = [[1], [2], [1]]
    actual = report_for(case)
    actual["result"]["rows"] = [[1], [1], [2]]
    assert evaluation.assess_report(case, actual) == []
    actual["result"]["rows"] = [[1], [2], [2]]
    assert evaluation.assess_report(case, actual) == ["data_error"]
    case["expected"]["ordered"] = True
    actual["result"]["rows"] = [[1], [1], [2]]
    assert evaluation.assess_report(case, actual) == ["data_error"]


def test_complete_empty_and_truncated_are_not_interchangeable():
    cases = evaluation.load_cases("dev")[1]
    for name in ("dev_empty_product", "dev_truncated"):
        case = next(item for item in cases if item["id"] == name)
        actual = report_for(case)
        assert evaluation.assess_report(case, actual) == []
        actual["result"]["truncated"] = not actual["result"]["truncated"]
        assert evaluation.assess_report(case, actual) == ["data_error"]


def test_rejected_task_with_dispatched_timeout_is_also_unsafe_execution():
    case = next(item for item in evaluation.load_cases("dev")[1]
                if item["id"] == "dev_scan_limit")
    actual = report_for(case)
    actual.update(execution_status="unknown", error={"code": "TIMEOUT"})
    assert evaluation.assess_report(case, actual) == ["budget", "unsafe_execution"]


@pytest.mark.parametrize(("code", "category"), [
    ("TIMEOUT", "budget"), ("RESPONSE_LIMIT", "budget"),
    ("CONNECTION_ERROR", "environment"), ("PERMISSION_DENIED", "environment"),
    ("SQL_REFERENCE_ERROR", "data_error"), ("UNSUPPORTED_RESULT_TYPE", "data_error"),
    ("SEMANTIC_MISMATCH", "data_error"), ("SEMANTIC_UNCERTAIN", "data_error"),
    ("SEMANTIC_REVIEW_INVALID", "environment"), ("SEMANTIC_REVIEW_FAILED", "environment"),
    ("MODEL_CALL_LIMIT", "budget"), ("TOOL_CALL_LIMIT", "budget"),
    ("GRAPH_RECURSION_LIMIT", "budget"),
])
def test_safe_error_classification(case, code, category):
    actual = report_for(case)
    actual.update(result=None, error={"code": code})
    assert evaluation.assess_report(case, actual) == [category]


def test_sql_mode_uses_query_service_and_does_not_call_agent(limits, case, monkeypatch):
    from db_agent import agent

    calls = []

    async def execute(self, sql):
        calls.append(sql)
        return report_for(case)

    async def forbidden(*args):
        pytest.fail("SQL mode must not call a model")

    monkeypatch.setattr(evaluation.QueryService, "execute", execute)
    monkeypatch.setattr(agent, "run_agent_observed", forbidden)
    actual = run_case(case, limits, "sql")
    assert actual["passed"] and calls == [case["sql"]]
    assert actual["model_calls"] == 0
    assert actual["explanation_scope"] == "not_applicable"


def test_agent_mode_scores_real_observation_without_reference_sql_fallback(
    limits, case, monkeypatch,
):
    from db_agent import agent

    calls = []
    generated_sql = "SELECT COUNT(*) AS n, SUM(total_amount) AS value FROM ec_orders"
    query = QueryExecution(generated_sql, report_for(case))

    async def observed(prompt, *args):
        calls.append(prompt)
        return AgentRunResult(render_queries([query]), [query], 2, ["execute_query"])

    async def forbidden(*args):
        pytest.fail("evaluator must not substitute reference SQL for real Agent traces")

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    monkeypatch.setattr(evaluation.QueryService, "execute", forbidden)
    actual = run_case(case, limits, "agent")
    assert actual["passed"]
    assert calls == [case["prompt"]] and case["sql"] not in calls[0]
    assert actual["queries"][0]["sql_sha256"] == evaluation._digest(generated_sql.encode())
    assert actual["explanation_scope"] == "deterministic_query_rendering_only"
    assert actual["free_semantics"] == "not_evaluated"


@pytest.mark.parametrize("failure", ["wrong_rows", "free_text", "no_query", "repeated_query"])
def test_agent_failures_cannot_be_masked_by_a_successful_model_response(
    limits, case, monkeypatch, failure,
):
    from db_agent import agent

    report = report_for(case)
    if failure == "wrong_rows":
        report["result"]["rows"] = [[3, "999.00"]]
    queries = [QueryExecution("SELECT id FROM ec_orders", report)]
    if failure == "no_query":
        queries = []
    if failure == "repeated_query":
        queries *= 2
    answer = render_queries(queries)
    if failure == "free_text":
        answer += "二月有三十天。"

    async def observed(*args):
        return AgentRunResult(answer, queries, 2, ["execute_query"] * len(queries))

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert not actual["passed"]
    assert ("explanation_error" if failure == "free_text" else "data_error") in (
        actual["categories"]
    )


def test_lower_result_cap_does_not_override_a_stricter_environment(limits, monkeypatch):
    from db_agent import agent

    case = next(item for item in evaluation.load_cases("dev")[1]
                if item["id"] == "dev_truncated")
    limits.query.max_rows = 1
    observed_limits = []

    async def observed(prompt, settings, connector, record, analysis, query):
        observed_limits.append(query.max_rows)
        return AgentRunResult(render_queries([]), [], 1, [])

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    assert not run_case(case, limits, "agent")["passed"]
    assert observed_limits == [1] and limits.query.max_rows == 1


def test_raw_exception_and_credentials_never_enter_evaluation_reports(
    limits, case, monkeypatch,
):
    from db_agent import agent

    async def observed(*args):
        raise RuntimeError("private-provider-body synthetic-model-key synthetic-reader")

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    result = run_case(case, limits, "agent")
    assert result["categories"] == ["environment"]
    assert all(word not in json.dumps(result) for word in (
        "private-provider-body", "synthetic-model-key", "synthetic-reader",
    ))


@pytest.mark.parametrize("completed_first", [False, True])
@pytest.mark.parametrize("code", [
    "MODEL_CALL_LIMIT", "TOOL_CALL_LIMIT", "GRAPH_RECURSION_LIMIT", "TIMEOUT",
])
def test_budget_failure_preserves_trusted_partial_evidence_without_passing(
    limits, case, monkeypatch, completed_first, code,
):
    from db_agent import agent

    pending_sql = "SELECT id FROM ec_orders WHERE id = 7"
    not_started = QueryExecution(pending_sql, {
        "status": "error", "decision": "UNKNOWN", "execution_status": "not_started",
        "result": None, "error": {"code": code},
    })
    queries = ([QueryExecution(case["sql"], report_for(case))] if completed_first else [])
    queries.append(not_started)
    reviews = [{"sql": case["sql"], "verdict": "match" if completed_first else "mismatch",
                "checks": {"filters": "constructed-review-evidence"}}]
    intents = [{"contract": {"query": None, "uncertainties": ["constructed-intent-evidence"]},
                "request_sha256": "0" * 64,
                "selected_sql": case["sql"], "selection": "AST_MATCH"}]
    partial = AgentRunResult(
        render_queries(queries), queries, 2,
        ["execute_query", "describe_table"] + (["execute_query"] if completed_first else []),
        reviews, intents,
    )
    error = agent.AgentResponseError(
        "模型或工具调用次数达到预算，已停止运行。", code=code, observation=partial,
    )

    async def observed(*args):
        raise error

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert not actual["passed"] and actual["categories"] == ["budget"]
    assert actual["failure_code"] == code and actual["partial_observation"] is True
    assert actual["model_calls"] == 2 and actual["tool_calls"] == partial.tool_calls
    assert actual["semantic_reviews"] == reviews
    assert actual["query_intents"] == intents
    assert actual["missing_query_reports"] == 0
    assert [item["sql"] for item in actual["queries"]] == [item.sql for item in queries]
    assert actual["queries"][-1]["execution_status"] == "not_started"
    assert actual["queries"][-1]["result"] is None
    if completed_first:
        assert actual["queries"][0]["execution_status"] == "completed"
        assert actual["queries"][0]["result"]["rows"] == case["expected"]["rows"]
    assert actual["answer_sha256"] is None and actual["explanation_scope"] == "not_observed"
    for value in (pending_sql, case["sql"], "constructed-review-evidence",
                  "constructed-intent-evidence"):
        assert value not in str(error) + repr(error)


@pytest.mark.parametrize("source", ["foreign_exception", "invalid_observation"])
def test_arbitrary_exception_payload_cannot_become_trusted_observation(
    limits, case, monkeypatch, source,
):
    from db_agent import agent

    queries = [QueryExecution("SELECT private_exception_marker", report_for(case))]
    if source == "foreign_exception":
        error = RuntimeError("private-provider-body")
        error.observation = AgentRunResult(render_queries(queries), queries, 1, ["execute_query"])
    else:
        error = agent.AgentResponseError(
            "模型或工具调用次数达到预算，已停止运行。", code="MODEL_CALL_LIMIT",
            observation=SimpleNamespace(queries=queries, model_calls=1),
        )

    async def observed(*args):
        raise error

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert not actual["passed"] and actual["queries"] == []
    assert actual["model_calls"] is None
    assert "partial_observation" not in actual
    assert "private_exception_marker" not in json.dumps(actual)


@pytest.mark.parametrize(("kind", "code", "category"), [
    ("token", "TOKEN_LIMIT", "budget"),
    ("model", "MODEL_CALL_LIMIT", "budget"),
    ("tool", "TOOL_CALL_LIMIT", "budget"),
    ("graph", "GRAPH_RECURSION_LIMIT", "budget"),
    ("no_context", "AGENT_CALL_LIMIT", "budget"),
    ("unknown_context", "AGENT_CALL_LIMIT", "budget"),
    ("unknown_message", "EVALUATION_CALL_FAILED", "environment"),
])
def test_fixed_budget_failures_link_run_without_exposing_exception_context(
    limits, case, monkeypatch, tmp_path, kind, code, category,
):
    from db_agent import agent

    contexts = {
        "model": agent.ModelCallLimitExceededError(0, 4, None, 4),
        "tool": agent.ToolCallLimitExceededError(0, 6, None, 6, "private-tool-marker"),
        "graph": agent.GraphRecursionError("private-framework-marker"),
        "unknown_context": RuntimeError("private-context-marker"),
    }

    async def observed(*args):
        if kind == "token":
            raise agent.AgentResponseError("模型输出达到 token 上限，请缩小问题或调整输出预算")
        if kind == "unknown_message":
            raise agent.AgentResponseError("private-provider-marker 预算 synthetic-model-key")
        if kind == "no_context":
            raise agent.AgentResponseError("模型或工具调用次数达到预算，已停止运行。")
        try:
            raise contexts[kind]
        except Exception:
            raise agent.AgentResponseError("模型或工具调用次数达到预算，已停止运行。") from None

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert actual["categories"] == [category]
    assert actual["failure_code"] == code
    assert actual["queries"] == [] and actual["model_calls"] is None
    records = list((tmp_path / "outputs/runs").glob("*.jsonl"))
    assert len(records) == 1 and records[0].stem == actual["run_id"]
    rows = [json.loads(line) for line in records[0].read_text().splitlines()]
    assert all(row["run_id"] == actual["run_id"] for row in rows)
    assert rows[-1]["event"] == "run_finished" and rows[-1]["status"] == "error"
    text = json.dumps(actual) + records[0].read_text()
    assert all(value not in text for value in (
        "private-tool-marker", "private-framework-marker", "private-context-marker",
        "private-provider-marker", "synthetic-model-key",
    ))


@pytest.mark.parametrize(("submitted", "passed"), [
    ("DELETE FROM ec_orders WHERE id = 1", True),
    (" \nDELETE FROM ec_orders WHERE id = 1; \n", True),
    ("DELETE FROM ec_orders WHERE id = 2", False),
    ("SELECT User FROM mysql.user", False),
    ("DELETE FROM ec_orders WHERE id = 1;;", False),
])
def test_rejection_evidence_is_bound_to_the_requested_input(
    limits, monkeypatch, submitted, passed,
):
    from db_agent import agent

    case = next(item for item in evaluation.load_cases("holdout")[1]
                if item["id"] == "holdout_write_block")
    queries = [QueryExecution(submitted, report_for(case))]

    async def observed(*args):
        return AgentRunResult(render_queries(queries), queries, 2, ["execute_query"])

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert actual["passed"] is passed
    assert actual["queries"][0]["sql"] == submitted
    if not passed:
        assert actual["failure_code"] == "REJECTION_INPUT_MISMATCH"


def test_missing_query_report_is_an_action_failure_not_a_rendering_mismatch(
    limits, case, monkeypatch,
):
    from db_agent import agent

    queries = [QueryExecution(case["sql"], report_for(case))]

    async def observed(*args):
        return AgentRunResult(
            render_queries(queries, missing_reports=1), queries, 3,
            ["execute_query", "execute_query"],
        )

    monkeypatch.setattr(agent, "run_agent_observed", observed)
    actual = run_case(case, limits, "agent")
    assert actual["categories"] == ["data_error"]
    assert actual["missing_query_reports"] == 1
    assert actual["explanation_scope"] == "deterministic_query_rendering_only"


def test_attempt_denominator_covers_each_selected_case_and_repeat(limits, monkeypatch):
    calls = []

    async def fake_case(case, **kwargs):
        calls.append(case["id"])
        failed = len(calls) == 3
        return {"case_id": case["id"], "passed": not failed,
                "categories": ["data_error"] if failed else []}

    monkeypatch.setattr(evaluation, "run_case", fake_case)
    report = asyncio.run(evaluation.evaluate(
        mode="sql", split="holdout", repeat=2, database=limits.database,
        analysis=limits.analysis, query=limits.query,
    ))
    assert report["task_count"] == 8 and report["attempt_count"] == 16
    assert len(calls) == 16 and calls[:8] == calls[8:]
    assert report["passed_count"] == 15 and report["failed_count"] == 1
    assert report["category_counts"]["data_error"] == 1
    assert report["model"] is None
    assert all(word not in json.dumps(report) for word in (
        "synthetic-model-key", "synthetic-reader",
    ))


def test_malformed_case_file_does_not_echo_its_contents(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text('{"secret":"private-fixture-marker"}')
    with pytest.raises(evaluation.EvaluationError) as caught:
        evaluation.load_cases("dev", path)
    assert "private-fixture-marker" not in str(caught.value)


def test_report_is_exclusive_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "PROJECT_ROOT", tmp_path)
    report = {"evaluation_id": "synthetic-run", "failed_count": 0}
    path = evaluation.write_report(report)
    assert json.loads(path.read_text()) == report
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        evaluation.write_report(report)


@pytest.mark.parametrize(("failed", "exit_code"), [(0, 0), (1, 1)])
def test_cli_reports_failure_with_nonzero_exit_and_sql_mode_needs_no_model(
    limits, monkeypatch, tmp_path, capsys, failed, exit_code,
):
    async def fake_evaluate(**kwargs):
        assert kwargs["model"] is None
        return {"evaluation_id": "synthetic-run", "passed_count": 1 - failed,
                "attempt_count": 1, "failed_count": failed}

    monkeypatch.setattr(evaluation, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evaluation, "evaluate", fake_evaluate)
    monkeypatch.setattr(evaluation, "load_database_settings", lambda: limits.database)
    monkeypatch.setattr(evaluation, "load_analysis_settings", lambda: limits.analysis)
    monkeypatch.setattr(evaluation, "load_query_settings", lambda: limits.query)
    monkeypatch.setattr(evaluation, "load_settings", lambda: pytest.fail("model config was loaded"))
    assert evaluation.main(["--mode", "sql", "--split", "dev", "--repeat", "1"]) == exit_code
    assert "sql/dev:" in capsys.readouterr().out
