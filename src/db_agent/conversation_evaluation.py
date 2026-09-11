"""Fixed local three-turn acceptance, with immutable inputs and incremental evidence."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from db_agent.agent import AgentResponseError
from db_agent.config import (
    AnalysisSettings,
    ConfigurationError,
    DatabaseSettings,
    QuerySettings,
    Settings,
    load_analysis_settings,
    load_database_settings,
    load_query_settings,
    load_settings,
)
from db_agent.conversation_context import conversation_prompt
from db_agent.db import MetadataConnector
from db_agent.evaluation import (
    CATEGORIES,
    TABLES,
    EvaluationError,
    _failure,
    _trace,
    assess_report,
    validate_environment,
)
from db_agent.presentation import AgentRunResult, render_queries
from db_agent.query import QueryService
from db_agent.records import RunRecord
from db_agent.session import ConversationError, ConversationSession

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = PROJECT_ROOT / "evals/conversations-v1.json"
SUITE_VERSION = "conversation-eval-v1"


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_hash(value) -> str:
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def _content_hash(value) -> str:
    """Match the suite's declared compact canonical content-hash encoding."""
    return _digest(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def load_cases(split: str, path: Path | None = None) -> tuple[dict, list[dict]]:
    if split not in {"dev", "holdout"}:
        raise EvaluationError("会话评测 split 必须为 dev 或 holdout。")
    path = CASE_FILE if path is None else path
    try:
        raw = path.read_bytes()
        if len(raw) > 262144:
            raise ValueError
        document = json.loads(raw)
        conversations = document["conversations"]
        context, required = document["business_context"], document["required_dataset"]
        if (
            document["suite_version"] != SUITE_VERSION
            or document["dataset_version"] != "ecommerce-v1"
            or not isinstance(context, str)
            or not context.strip()
            or len(context) > 4096
            or not isinstance(conversations, list)
            or len(conversations) != 6
            or any(
                required.get(key) != expected
                for key, expected in (
                    ("orders", 1000000),
                    ("customers", 100000),
                    ("products", 10000),
                    ("seed", 20260910),
                )
            )
        ):
            raise ValueError
        identifiers = set()
        for item in conversations:
            if (
                not isinstance(item["id"], str)
                or not item["id"]
                or item["id"] in identifiers
                or item["split"] not in {"dev", "holdout"}
                or not isinstance(item["turns"], list)
                or len(item["turns"]) != 3
            ):
                raise ValueError
            identifiers.add(item["id"])
            history = []
            for turn in item["turns"]:
                expected = turn["expected"]
                if (
                    any(
                        not isinstance(turn[key], str) or not turn[key].strip()
                        for key in ("prompt", "reference_sql", "oracle")
                    )
                    or len(turn["reference_sql"].encode()) > 16384
                    or not isinstance(turn["tables"], list)
                    or not turn["tables"]
                    or any(not isinstance(name, str) for name in turn["tables"])
                    or not set(turn["tables"]).issubset(TABLES)
                    or expected["status"] != "ok"
                    or expected["decision"] != "ALLOW"
                    or expected["execution_status"] != "completed"
                    or expected["truncated"] is not False
                    or type(expected["ordered"]) is not bool
                    or not isinstance(expected["rows"], list)
                    or any(not isinstance(row, list) for row in expected["rows"])
                ):
                    raise ValueError
                current = context + "\n\n" + turn["prompt"] if not history else turn["prompt"]
                conversation_prompt(history, current)
                history.append(current)
        if any(sum(c["split"] == name for c in conversations) != 3 for name in ("dev", "holdout")):
            raise ValueError
        selected = [item for item in conversations if item["split"] == split]
        content_hashes = {"case_content_sha256": _content_hash(conversations)}
        content_hashes.update(
            {
                f"{name}_content_sha256": _content_hash(
                    [item for item in conversations if item["split"] == name]
                )
                for name in ("dev", "holdout")
            }
        )
        selected_hash = _json_hash(selected)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise EvaluationError("会话评测案例缺失、超界或格式无效。") from None
    return {
        "suite_version": SUITE_VERSION,
        "dataset_version": "ecommerce-v1",
        "case_file_sha256": _digest(raw),
        "selected_cases_sha256": selected_hash,
        "content_hashes": content_hashes,
        "provenance": deepcopy(document.get("provenance")),
        "business_context_sha256": _digest(context.encode()),
        "business_context": context,
        "required_dataset": required,
    }, selected


def source_identity() -> dict:
    paths = [
        *sorted((PROJECT_ROOT / "src/db_agent").glob("*.py")),
        PROJECT_ROOT / "uv.lock",
        PROJECT_ROOT / "scripts/evaluate_ecommerce.py",
        PROJECT_ROOT / "scripts/evaluate_conversations.py",
    ]
    hashes = {str(path.relative_to(PROJECT_ROOT)): _digest(path.read_bytes()) for path in paths}
    return {"files": hashes, "sha256": _json_hash(hashes)}


def _verify_freeze(manifest: dict, source: dict) -> None:
    provenance = manifest["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("production_freeze_status") != "frozen"
        or not isinstance(provenance.get("product_source_snapshot"), dict)
        or provenance["product_source_snapshot"].get("sha256") != source["sha256"]
        or provenance.get("business_context_sha256") != manifest["business_context_sha256"]
        or any(provenance.get(name) != value for name, value in manifest["content_hashes"].items())
    ):
        raise EvaluationError("会话评测尚未冻结，或当前源码、案例内容与冻结声明不符。")


class _InputChanged(Exception):
    def __init__(self, code):
        self.code = code


def _unchanged(source: dict, path: Path, case_hash: str) -> None:
    try:
        if source_identity() != source:
            raise _InputChanged("SOURCE_CHANGED")
        if _digest(path.read_bytes()) != case_hash:
            raise _InputChanged("CASE_CHANGED")
    except OSError:
        raise _InputChanged("INPUT_UNAVAILABLE") from None


def _checkpoint(path: Path, report: dict) -> None:
    temporary = path.with_name(f".{path.stem}.{uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _observation(outcome: dict, observed: AgentRunResult) -> None:
    outcome.update(
        queries=[_trace(q.sql, q.report) for q in observed.queries],
        model_calls=observed.model_calls,
        tool_calls=observed.tool_calls,
        answer_sha256=_digest(observed.answer.encode()),
        intent_request_sha256=[item.get("request_sha256") for item in observed.query_intents],
    )


async def _turn(turn, current, session, connector, analysis, query, model, outcome, record):
    if session is None:
        report = await QueryService(connector, analysis, query, record).execute(
            turn["reference_sql"]
        )
        outcome["queries"] = [_trace(turn["reference_sql"], report)]
        outcome["categories"].extend(assess_report(turn, report))
        return
    # This is the real session API, including its reset-required state. Never repair
    # a failed predecessor with the reference SQL or silently start another session.
    outcome["context_sha256"] = _digest(conversation_prompt(session.requests, current).encode())
    outcome["intended_context_sha256"] = outcome["context_sha256"]
    observed = await session.submit(current, record)
    _observation(outcome, observed)
    if len(observed.queries) != 1:
        outcome["categories"].append("data_error")
    else:
        outcome["categories"].extend(assess_report(turn, observed.queries[0].report))
    missing = max(0, observed.tool_calls.count("execute_query") - len(observed.queries))
    if missing or observed.tool_calls.count("execute_query") != 1:
        outcome["categories"].append("data_error")
    if observed.answer != render_queries(observed.queries, missing_reports=missing):
        outcome["categories"].append("explanation_error")
    successful = any(q.report.get("status") == "ok" for q in observed.queries)
    hashes = outcome["intent_request_sha256"]
    if (successful and not hashes) or any(h != outcome["context_sha256"] for h in hashes):
        outcome["categories"].append("data_error")
        outcome["failure_code"] = "REQUEST_HASH_MISMATCH"
    if (
        observed.model_calls > model.max_model_calls
        or len(observed.tool_calls) > model.max_tool_calls
    ):
        outcome["categories"].append("budget")
    outcome["session_needs_reset"] = session.needs_reset


def _totals(report):
    turns = [turn for chain in report["chains"] for turn in chain["turns"]]
    for chain in report["chains"]:
        chain["passed"] = all(turn["passed"] for turn in chain["turns"])
    report.update(
        attempted_turns=sum(turn["state"] != "not_attempted" for turn in turns),
        passed_turns=sum(turn["passed"] for turn in turns),
        failed_turns=sum(not turn["passed"] for turn in turns),
        passed_conversations=sum(chain["passed"] for chain in report["chains"]),
        failed_conversations=sum(not chain["passed"] for chain in report["chains"]),
        category_counts={
            category: sum(category in turn["categories"] for turn in turns)
            for category in (*CATEGORIES, "dependency", "incomplete")
        },
    )


async def evaluate(
    *,
    mode: str,
    split: str,
    repeat: int,
    database: DatabaseSettings,
    analysis: AnalysisSettings,
    query: QuerySettings,
    model: Settings | None = None,
    path: Path | None = None,
) -> dict:
    if mode not in {"sql", "agent"} or type(repeat) is not int or not 1 <= repeat <= 3:
        raise EvaluationError("会话评测 mode 必须为 sql/agent，repeat 必须为 1 至 3。")
    if mode == "agent" and model is None:
        raise EvaluationError("Agent 会话评测需要明确模型配置。")
    path = CASE_FILE if path is None else path
    manifest, conversations = load_cases(split, path)
    context = manifest.pop("business_context")
    source = source_identity()
    _verify_freeze(manifest, source)
    scoped = validate_environment(
        database,
        analysis,
        [turn for conversation in conversations for turn in conversation["turns"]],
    )
    report = {
        "evaluation_id": uuid4().hex,
        "started_at": datetime.now(UTC).isoformat(),
        "suite_version": SUITE_VERSION,
        "mode": mode,
        "split": split,
        "split_role": "development" if split == "dev" else "held_out_acceptance",
        "repeat": repeat,
        "preregistered_runs": deepcopy(manifest["provenance"].get("preregistered_runs")),
        "state": "running",
        "case_manifest": manifest,
        "source": source,
        "conversation_count": len(conversations),
        "planned_conversations": len(conversations) * repeat,
        "planned_turns": len(conversations) * repeat * 3,
        "analysis_limits": analysis.model_dump(),
        "query_limits": query.model_dump(),
        "model": None
        if mode == "sql"
        else {
            "configured_model": model.model,
            "endpoint_sha256": _digest(model.openai_base_url.encode()),
            **{
                field: getattr(model, field)
                for field in (
                    "max_model_calls",
                    "max_tool_calls",
                    "max_output_tokens",
                    "request_timeout_seconds",
                    "run_timeout_seconds",
                )
            },
            "provider_reported_version": None,
        },
        "environment": {
            "host": "127.0.0.1",
            "port": 13306,
            "database": "db_agent",
            "user": "db_agent_reader",
            "allowed_tables": list(scoped.allowed_tables),
        },
        "packages": {name: version(name) for name in ("db-agent", "sqlglot", "langchain-openai")},
        "limitations": [
            "仅验收本地合成场景；模型固定用例通过不是一般语义等价或生产可靠性保证。",
            "回答只核对可信查询报告的程序渲染，不评价开放式自然语言解释。",
            "未运行轮次仍在计划分母中；失败后的依赖停止不能算作业务成功。",
            "配置输出上限不等于提供方实际 token/成本硬上限已验证。",
        ],
        "chains": [
            {
                "conversation_id": item["id"],
                "iteration": iteration,
                "passed": False,
                "turns": [
                    {
                        "turn": index + 1,
                        "state": "not_attempted",
                        "passed": False,
                        "categories": ["incomplete"],
                        "queries": [],
                        "failure_code": None,
                        "run_id": None,
                        "model_calls": 0 if mode == "sql" else None,
                        "tool_calls": [],
                        "context_sha256": None,
                        "raw_prompt_sha256": _digest(turn["prompt"].encode()),
                        "reference_sql_sha256": _digest(turn["reference_sql"].encode()),
                        "turn_case_sha256": _json_hash(turn),
                        "expected": deepcopy(turn["expected"]),
                    }
                    for index, turn in enumerate(item["turns"])
                ],
            }
            for iteration in range(1, repeat + 1)
            for item in conversations
        ],
    }
    output = PROJECT_ROOT / "outputs/evals" / f"{report['evaluation_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.close(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    _totals(report)
    _checkpoint(output, report)
    active = None
    try:
        for chain in report["chains"]:
            conversation = next(c for c in conversations if c["id"] == chain["conversation_id"])
            session = (
                ConversationSession(model, scoped, analysis, query) if mode == "agent" else None
            )
            connector = MetadataConnector(scoped) if mode == "sql" else None
            for index, (turn, outcome) in enumerate(
                zip(conversation["turns"], chain["turns"], strict=True)
            ):
                _unchanged(source, path, manifest["case_file_sha256"])
                active = outcome
                current = context + "\n\n" + turn["prompt"] if index == 0 else turn["prompt"]
                outcome.update(
                    state="running", categories=[], current_request_sha256=_digest(current.encode())
                )
                started = time.monotonic()
                with RunRecord() as record:
                    outcome["run_id"] = record.run_id
                    _checkpoint(output, report)
                    try:
                        await _turn(
                            turn,
                            current,
                            session,
                            connector,
                            analysis,
                            query,
                            model,
                            outcome,
                            record,
                        )
                    except ConversationError as exc:
                        outcome["categories"].append(
                            "dependency" if exc.code == "SESSION_RESET_REQUIRED" else "environment"
                        )
                        outcome["failure_code"] = exc.code
                        outcome["model_calls"] = 0
                        outcome["context_sha256"] = None
                    except TimeoutError:
                        outcome["categories"].append("budget")
                        outcome["failure_code"] = "TIMEOUT"
                    except Exception as exc:
                        category, code = _failure(exc)
                        outcome["categories"].append(category)
                        outcome["failure_code"] = code
                        if isinstance(exc, AgentResponseError) and isinstance(
                            exc.observation, AgentRunResult
                        ):
                            _observation(outcome, exc.observation)
                            outcome["partial_observation"] = True
                    finally:
                        outcome["duration_ms"] = round((time.monotonic() - started) * 1000)
                outcome["categories"] = sorted(set(outcome["categories"]))
                hashes = outcome.get("intent_request_sha256", [])
                outcome["intent_context_verified"] = bool(hashes) and all(
                    value == outcome["context_sha256"] for value in hashes
                )
                if hashes and not outcome["intent_context_verified"]:
                    outcome["categories"] = sorted(set([*outcome["categories"], "data_error"]))
                    outcome["failure_code"] = "REQUEST_HASH_MISMATCH"
                outcome.update(state="finished", passed=not outcome["categories"])
                _totals(report)
                _checkpoint(output, report)
                state = "PASS" if outcome["passed"] else ",".join(outcome["categories"])
                print(
                    f"{mode}/{split} {chain['iteration']}/{repeat} "
                    f"{conversation['id']} turn {index + 1}: {state}",
                    flush=True,
                )
                active = None
                _unchanged(source, path, manifest["case_file_sha256"])
        report["state"] = "completed"
    except _InputChanged as exc:
        report.update(state="incomplete", failure_code=exc.code)
    except BaseException as exc:
        code = (
            "INTERRUPTED"
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            else "EVALUATION_INCOMPLETE"
        )
        report.update(state="incomplete", failure_code=code)
        if active is not None:
            active.update(
                state="interrupted",
                passed=False,
                categories=["incomplete"],
                failure_code=code,
                execution_status="unknown",
            )
        raise
    finally:
        report["completed_at"] = datetime.now(UTC).isoformat()
        try:
            _unchanged(source, path, manifest["case_file_sha256"])
            report["inputs_unchanged"] = True
        except _InputChanged as exc:
            report.update(inputs_unchanged=False, state="incomplete", failure_code=exc.code)
        _totals(report)
        _checkpoint(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定本地三轮会话 SQL/真实 Agent 评测")
    parser.add_argument("--mode", choices=("sql", "agent"), required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--repeat", type=int, choices=range(1, 4), required=True)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            evaluate(
                mode=args.mode,
                split=args.split,
                repeat=args.repeat,
                database=load_database_settings(),
                analysis=load_analysis_settings(),
                query=load_query_settings(),
                model=load_settings() if args.mode == "agent" else None,
            )
        )
    except (EvaluationError, ConfigurationError):
        print("会话评测输入、配置或环境校验失败。")
        return 2
    except KeyboardInterrupt:
        print("会话评测已中断；已取得证据保存在 outputs/evals，未完成项不算通过。")
        return 130
    except Exception:
        print("会话评测未完成；请核对 outputs/evals 中的增量报告。")
        return 1
    print(f"报告：outputs/evals/{report['evaluation_id']}.json")
    print(
        f"会话 {report['passed_conversations']}/{report['planned_conversations']}；"
        f"轮次 {report['passed_turns']}/{report['planned_turns']}；状态 {report['state']}"
    )
    return 0 if report["state"] == "completed" and report["failed_turns"] == 0 else 1
