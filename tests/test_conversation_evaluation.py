"""Offline synthetic reports exercise the real session and evaluator, never model/DB I/O."""

import asyncio
import copy
import hashlib
import json
import stat

import pytest
from pydantic import SecretStr

from db_agent import conversation_evaluation as evaluation
from db_agent import db as db_module
from db_agent import session as session_module
from db_agent.agent import AgentResponseError
from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings, Settings
from db_agent.conversation_context import conversation_prompt
from db_agent.presentation import AgentRunResult, QueryExecution, render_queries
from db_agent.query import QueryService


def frozen_provenance(document):
    def canonical(value):
        return hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    conversations = document["conversations"]
    return {
        "production_freeze_status": "frozen",
        "product_source_snapshot": evaluation.source_identity(),
        "case_content_sha256": canonical(conversations),
        "business_context_sha256": hashlib.sha256(
            document["business_context"].encode()
        ).hexdigest(),
        **{
            f"{split}_content_sha256": canonical(
                [item for item in conversations if item["split"] == split]
            )
            for split in ("dev", "holdout")
        },
        "preregistered_runs": {"sql": {"dev": 1, "holdout": 1}, "agent": {"dev": 1, "holdout": 2}},
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(evaluation, "PROJECT_ROOT", tmp_path)
    for name in (
        "src/db_agent/offline.py",
        "uv.lock",
        "scripts/evaluate_ecommerce.py",
        "scripts/evaluate_conversations.py",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic-source\n")
    document = {
        "suite_version": "conversation-eval-v1",
        "dataset_version": "ecommerce-v1",
        "required_dataset": {
            "orders": 1000000,
            "customers": 100000,
            "products": 10000,
            "seed": 20260910,
        },
        "business_context": "公开离线业务字典；用户口径，不包含参考SQL或答案。",
        "conversations": [
            {
                "id": f"conversation_{c}",
                "split": "dev" if c < 3 else "holdout",
                "turns": [
                    {
                        "prompt": f"synthetic_conversation_{c}_turn_{t}",
                        "tables": ["ec_orders"],
                        "reference_sql": (
                            f"SELECT id AS reference_only FROM ec_orders WHERE id={c * 10 + t}"
                        ),
                        "oracle": "oracle_only_marker: independent synthetic expectation",
                        "expected": {
                            "status": "ok",
                            "decision": "ALLOW",
                            "execution_status": "completed",
                            "rows": [[c * 10 + t]],
                            "ordered": True,
                            "truncated": False,
                        },
                    }
                    for t in range(1, 4)
                ],
            }
            for c in range(6)
        ],
    }
    document["provenance"] = frozen_provenance(document)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(document, ensure_ascii=False))
    monkeypatch.setattr(evaluation, "CASE_FILE", path)
    model = Settings.model_construct(
        model="synthetic-model-alias",
        api_key=SecretStr("model-secret-marker"),
        openai_base_url="https://example.invalid/private-provider-path",
    )
    database = DatabaseSettings.model_construct(
        password=SecretStr("database-secret-marker"),
        allowed_tables=("ec_orders", "secret_table"),
    )
    state = {"calls": [], "sql_calls": [], "behavior": None}
    turns = {turn["prompt"]: turn for c in document["conversations"] for turn in c["turns"]}

    def result_for(turn, sql):
        report = {
            "status": "ok",
            "decision": "ALLOW",
            "execution_status": "completed",
            "error": None,
            "findings": [],
            "query_id": "synthetic-query",
            "sql_fingerprint": "a" * 64,
            "result": {
                "columns": [{"name": "id", "type": "bigint"}],
                "rows": copy.deepcopy(turn["expected"]["rows"]),
                "row_count": 1,
                "truncated": False,
                "truncation_reason": None,
                "server_statement_status": "completed",
            },
        }
        return QueryExecution(sql, report)

    async def offline_agent(
        prompt, settings, connector, record, analysis, query, *, previous_requests=None
    ):
        turn = turns[prompt.rsplit("\n\n", 1)[-1]]
        sql = f"SELECT id FROM ec_orders WHERE id={turn['expected']['rows'][0][0]}"
        execution = result_for(turn, sql)
        result = AgentRunResult(
            render_queries([execution]),
            [execution],
            4,
            ["describe_table", "execute_query"],
            semantic_reviews=[{"verdict": "match"}],
            query_intents=[
                {
                    "request_sha256": hashlib.sha256(
                        conversation_prompt(previous_requests, prompt).encode(),
                    ).hexdigest(),
                }
            ],
        )
        state["calls"].append(
            {
                "prompt": prompt,
                "previous": copy.deepcopy(previous_requests),
                "scope": connector.authorized_table_candidates,
            }
        )
        if state["behavior"]:
            state["behavior"](result, len(state["calls"]))
        return result

    async def offline_query(self, sql):
        state["sql_calls"].append(sql)
        turn = next(t for t in turns.values() if t["reference_sql"] == sql)
        return result_for(turn, sql).report

    async def no_database(**kwargs):
        pytest.fail("offline conversation evaluation attempted MySQL I/O")

    monkeypatch.setattr(session_module, "run_agent_observed", offline_agent)
    monkeypatch.setattr(QueryService, "execute", offline_query)
    monkeypatch.setattr(db_module.aiomysql, "connect", no_database)
    return {
        "root": tmp_path,
        "path": path,
        "document": document,
        "state": state,
        "model": model,
        "database": database,
        "analysis": AnalysisSettings.model_construct(),
        "query": QuerySettings.model_construct(),
    }


def run(setup, mode="agent", repeat=1):
    return asyncio.run(
        evaluation.evaluate(
            mode=mode,
            split="dev",
            repeat=repeat,
            database=setup["database"],
            analysis=setup["analysis"],
            query=setup["query"],
            model=setup["model"] if mode == "agent" else None,
            path=setup["path"],
        )
    )


@pytest.mark.parametrize("mode", ["sql", "agent"])
@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "missing",
        "source",
        "case_hash",
        "dev_hash",
        "holdout_hash",
        "dev_rows",
        "holdout_rows",
        "business_context",
    ],
)
def test_unfrozen_or_tampered_suite_stops_before_session_or_database(
    setup, monkeypatch, mode, change
):
    data = setup["document"]
    provenance = data["provenance"]
    if change == "pending":
        provenance["production_freeze_status"] = "pending"
    elif change == "missing":
        del data["provenance"]
    elif change == "source":
        provenance["product_source_snapshot"]["sha256"] = "0" * 64
    elif change == "business_context":
        data["business_context"] += " altered dictionary"
    elif change.endswith("_hash"):
        provenance[change.removesuffix("_hash") + "_content_sha256"] = "0" * 64
    else:
        index = 0 if change == "dev_rows" else 3
        data["conversations"][index]["turns"][0]["expected"]["rows"] = [[999]]
    setup["path"].write_text(json.dumps(data, ensure_ascii=False))
    # Reading/reviewing an unfrozen suite remains possible; only execution is gated.
    evaluation.load_cases("dev", setup["path"])

    def forbidden(*args, **kwargs):
        pytest.fail("freeze rejection must precede model/session/database construction")

    monkeypatch.setattr(evaluation, "ConversationSession", forbidden)
    monkeypatch.setattr(evaluation, "MetadataConnector", forbidden)
    with pytest.raises(evaluation.EvaluationError, match="冻结"):
        run(setup, mode=mode)
    assert setup["state"]["calls"] == [] and setup["state"]["sql_calls"] == []
    assert not (setup["root"] / "outputs/evals").exists()


def test_three_turn_chains_keep_raw_history_and_reset_for_every_repeat(setup):
    report = run(setup, repeat=2)
    assert (report["planned_conversations"], report["passed_conversations"]) == (6, 6)
    assert (report["planned_turns"], report["passed_turns"]) == (18, 18)
    assert report["state"] == "completed" and report["inputs_unchanged"]
    assert report["preregistered_runs"] == setup["document"]["provenance"]["preregistered_runs"]
    calls = setup["state"]["calls"]
    assert len(calls) == 18
    for start in range(0, 18, 3):
        first, second, third = calls[start : start + 3]
        assert first["previous"] == []
        assert second["previous"] == [first["prompt"]]
        assert third["previous"] == [first["prompt"], second["prompt"]]
        assert first["prompt"].startswith(setup["document"]["business_context"] + "\n\n")
        assert "\n\n" not in second["prompt"] and "\n\n" not in third["prompt"]
    serialized = json.dumps(calls)
    assert "reference_only" not in serialized and "oracle_only_marker" not in serialized
    assert all(call["scope"] == ("ec_orders",) for call in calls)
    for chain in report["chains"]:
        for turn in chain["turns"]:
            assert turn["intent_request_sha256"] == [turn["context_sha256"]]
            assert turn["queries"][0]["result"]["rows"] == turn["expected"]["rows"]
    assert set(report["source"]["files"]) == {
        "src/db_agent/offline.py",
        "uv.lock",
        "scripts/evaluate_ecommerce.py",
        "scripts/evaluate_conversations.py",
    }


def test_failed_turn_keeps_dependents_in_denominator_and_calls_real_suspended_session(setup):
    def fail(result, number):
        if number == 1:
            result.queries[0].report.update(
                status="error",
                decision="UNKNOWN",
                execution_status="not_started",
                result=None,
                error={"code": "SEMANTIC_REVIEW_INVALID"},
            )
            object.__setattr__(result, "answer", render_queries(result.queries))

    setup["state"]["behavior"] = fail
    report = run(setup)
    first = report["chains"][0]["turns"]
    assert first[0]["categories"] == ["environment"]
    assert [turn["categories"] for turn in first[1:]] == [["dependency"], ["dependency"]]
    assert all(turn["failure_code"] == "SESSION_RESET_REQUIRED" for turn in first[1:])
    assert all(turn["model_calls"] == 0 and not turn["queries"] for turn in first[1:])
    assert len(setup["state"]["calls"]) == 7  # No retry, reset, or reference-query substitution.
    assert report["attempted_turns"] == report["planned_turns"] == 9
    assert report["passed_turns"] == 6 and report["passed_conversations"] == 2


@pytest.mark.parametrize(
    "kind,category",
    [
        ("wrong_rows", "data_error"),
        ("incomplete", "data_error"),
        ("bad_answer", "explanation_error"),
        ("bad_hash", "data_error"),
        ("missing_hash", "data_error"),
        ("extra_query", "data_error"),
        ("budget", "budget"),
    ],
)
def test_incorrect_results_answers_and_contexts_do_not_pass(setup, kind, category):
    def corrupt(result, number):
        if number != 1:
            return
        if kind == "wrong_rows":
            result.queries[0].report["result"]["rows"] = [[999999]]
        elif kind == "incomplete":
            result.queries[0].report["result"]["server_statement_status"] = "unknown"
        elif kind == "bad_answer":
            object.__setattr__(result, "answer", "synthetic free-text claim")
        elif kind == "bad_hash":
            result.query_intents[0]["request_sha256"] = "0" * 64
        elif kind == "missing_hash":
            result.query_intents.clear()
        elif kind == "extra_query":
            result.queries.append(result.queries[0])
        else:
            object.__setattr__(result, "model_calls", 5)

    setup["state"]["behavior"] = corrupt
    report = run(setup)
    turn = report["chains"][0]["turns"][0]
    assert not turn["passed"] and category in turn["categories"]
    assert report["passed_conversations"] < report["planned_conversations"]


def test_sql_mode_uses_every_full_reference_without_loading_model(setup, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("SQL mode loaded model or instantiated a conversation")

    monkeypatch.setattr(evaluation, "load_settings", forbidden)
    monkeypatch.setattr(evaluation, "ConversationSession", forbidden)
    for name, value in (
        ("database", setup["database"]),
        ("analysis", setup["analysis"]),
        ("query", setup["query"]),
    ):
        monkeypatch.setattr(evaluation, f"load_{name}_settings", lambda value=value: value)
    assert evaluation.main(["--mode", "sql", "--split", "dev", "--repeat", "1"]) == 0
    assert setup["state"]["calls"] == []
    assert setup["state"]["sql_calls"] == [
        turn["reference_sql"] for c in setup["document"]["conversations"][:3] for turn in c["turns"]
    ]
    report = json.loads(next((setup["root"] / "outputs/evals").glob("*.json")).read_text())
    assert report["model"] is None and report["passed_turns"] == 9
    assert "9/9" in capsys.readouterr().out


@pytest.mark.parametrize("kind,code", [("source", "SOURCE_CHANGED"), ("case", "CASE_CHANGED")])
def test_changed_inputs_stop_after_preserving_current_evidence(setup, kind, code):
    def change(result, number):
        if number == 1:
            target = setup["path"] if kind == "case" else setup["root"] / "src/db_agent/offline.py"
            target.write_text(target.read_text() + "\n")

    setup["state"]["behavior"] = change
    report = run(setup)
    assert report["state"] == "incomplete" and report["failure_code"] == code
    assert not report["inputs_unchanged"]
    assert report["passed_turns"] == report["attempted_turns"] == 1
    assert report["planned_turns"] == 9 and report["failed_turns"] == 8
    assert report["chains"][0]["turns"][0]["queries"][0]["result"]["rows"] == [[1]]
    assert len(setup["state"]["calls"]) == 1


def test_cancelled_evaluation_keeps_completed_turn_and_marks_remaining_incomplete(setup):
    def cancel(result, number):
        if number == 2:
            raise asyncio.CancelledError

    setup["state"]["behavior"] = cancel
    with pytest.raises(asyncio.CancelledError):
        run(setup)
    report = json.loads(next((setup["root"] / "outputs/evals").glob("*.json")).read_text())
    assert report["state"] == "incomplete" and report["failure_code"] == "INTERRUPTED"
    assert report["passed_turns"] == 1 and report["failed_turns"] == 8
    assert report["chains"][0]["turns"][0]["queries"][0]["result"]["rows"] == [[1]]
    assert report["chains"][0]["turns"][1]["state"] == "interrupted"
    assert report["chains"][0]["turns"][1]["execution_status"] == "unknown"
    assert len(setup["state"]["calls"]) == 2


def test_report_exists_before_first_run_and_preserves_safe_partial_observation(setup):
    snapshots = []

    def fail(result, number):
        report_path = next((setup["root"] / "outputs/evals").glob("*.json"))
        snapshots.append(json.loads(report_path.read_text()))
        assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
        if number == 1:
            raise AgentResponseError("provider-secret-marker", code="TIMEOUT", observation=result)

    setup["state"]["behavior"] = fail
    report = run(setup)
    first = report["chains"][0]["turns"][0]
    assert first["categories"] == ["budget"] and first["partial_observation"]
    assert first["queries"][0]["result"]["rows"] == [[1]]
    assert snapshots[0]["chains"][0]["turns"][0]["state"] == "running"
    report_path = setup["root"] / "outputs/evals" / f"{report['evaluation_id']}.json"
    saved = report_path.read_text()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    for private in (
        "provider-secret-marker",
        "model-secret-marker",
        "database-secret-marker",
        "https://example.invalid/private-provider-path",
    ):
        assert private not in saved
    assert report["model"]["configured_model"] == "synthetic-model-alias"
    assert (
        report["model"]["endpoint_sha256"]
        == hashlib.sha256(
            setup["model"].openai_base_url.encode(),
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "kind",
    [
        "wrong_split",
        "wrong_turn_count",
        "missing_table",
        "bad_dataset",
        "wrong_suite",
        "invalid_json",
    ],
)
def test_loader_rejects_incomplete_or_wrong_suite_material(setup, kind):
    data = setup["document"]
    if kind == "wrong_split":
        data["conversations"][0]["split"] = "holdout"
    elif kind == "wrong_turn_count":
        data["conversations"][0]["turns"].pop()
    elif kind == "missing_table":
        data["conversations"][0]["turns"][0]["tables"] = ["private_table"]
    elif kind == "bad_dataset":
        data["required_dataset"]["orders"] = 8
    elif kind == "wrong_suite":
        data["suite_version"] = "conversation-eval-v2"
    setup["path"].write_text("{" if kind == "invalid_json" else json.dumps(data))
    with pytest.raises(evaluation.EvaluationError):
        evaluation.load_cases("dev", setup["path"])
    assert setup["state"]["calls"] == [] and setup["state"]["sql_calls"] == []


@pytest.mark.parametrize("extra", [["--repeat", "4"], ["--path", "arbitrary.json"]])
def test_cli_has_only_fixed_case_path_and_bounded_repetition(extra):
    with pytest.raises(SystemExit) as caught:
        evaluation.main(["--mode", "sql", "--split", "dev", "--repeat", "1", *extra])
    assert caught.value.code == 2
