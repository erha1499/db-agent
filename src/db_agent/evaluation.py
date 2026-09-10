"""Explicit local ecommerce evaluations; no reference SQL is sent to the model."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

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
from db_agent.db import MetadataConnector
from db_agent.query import QueryService
from db_agent.records import RunRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = PROJECT_ROOT / "evals" / "ecommerce-v1.json"
SUITE_FILES = {
    name: CASE_FILE.with_name(f"ecommerce-{name}.json") for name in ("v1", "v2", "v3", "v4", "v5")
}
TABLES = frozenset({
    "ec_customers", "ec_products", "ec_orders", "ec_order_items", "ec_payments", "ec_refunds",
})
CATEGORIES = ("data_error", "explanation_error", "unsafe_execution", "budget", "environment")


class EvaluationError(ValueError):
    """A fixed diagnostic, never a raw model or database exception."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _prompt(case: dict) -> str:
    context = case.get("business_context")
    return context + "\n\n" + case["prompt"] if context else case["prompt"]


def load_cases(
    split: str, path: Path | None = None, *, suite: str = "v1",
) -> tuple[dict, list[dict]]:
    if suite not in SUITE_FILES:
        raise EvaluationError("评测 suite 必须为 v1、v2、v3、v4 或 v5。")
    if split not in {"dev", "holdout"}:
        raise EvaluationError("评测 split 必须为 dev 或 holdout。")
    path = SUITE_FILES[suite] if path is None else path
    try:
        raw = path.read_bytes()
        if len(raw) > 262144:
            raise ValueError
        document = json.loads(raw)
        cases = document["cases"]
        required_dataset = document["required_dataset"]
        if document["dataset_version"] != "ecommerce-v1" or not isinstance(cases, list):
            raise ValueError
        suite_version = document.get("suite_version", "ecommerce-eval-v1")
        if suite_version != f"ecommerce-eval-{suite}":
            raise ValueError
        business_context = document.get("business_context", "")
        if not isinstance(business_context, str) or len(business_context) > 4096:
            raise ValueError
        if any(required_dataset.get(name) != count for name, count in (
            ("orders", 1000000), ("customers", 100000), ("products", 10000),
        )) or required_dataset.get("seed", 20260910) != 20260910:
            raise ValueError
        required_dataset = {"seed": 20260910, **required_dataset}
        ids = set()
        for case in cases:
            expected = case["expected"]
            if (
                not isinstance(case["id"], str) or case["id"] in ids
                or case["split"] not in {"dev", "holdout"}
                or not isinstance(case["prompt"], str) or not case["prompt"].strip()
                or not isinstance(case["sql"], str) or not case["sql"].strip()
                or not isinstance(case["oracle"], str) or not case["oracle"].strip()
                or not set(case["tables"]).issubset(TABLES)
                or expected["decision"] not in {"ALLOW", "BLOCK", "REVIEW", "UNKNOWN"}
                or expected["status"] not in {"ok", "rejected"}
                or type(expected["ordered"]) is not bool
                or type(expected["truncated"]) is not bool
                or expected["execution_status"] not in {"completed", "truncated", "not_started"}
                or (expected["status"] == "ok") != isinstance(expected["rows"], list)
                or (expected["status"] == "ok") != (expected["decision"] == "ALLOW")
                or type(case.get("max_rows", 100)) is not int
                or not 1 <= case.get("max_rows", 100) <= 100
            ):
                raise ValueError
            ids.add(case["id"])
        selected = [case for case in cases if case["split"] == split]
        if not selected:
            raise ValueError
        if business_context:
            selected = [{**case, "business_context": business_context} for case in selected]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise EvaluationError("公开评测案例缺失或格式无效。") from None
    return {
        "suite": suite, "suite_version": suite_version,
        "dataset_version": document["dataset_version"],
        # Every existing suite has now informed diagnosis or implementation.
        # Preserve its original split and first-run reports, but label reruns honestly.
        "split_role": "exposed_regression",
        "business_context_sha256": _digest(business_context.encode()) if business_context else None,
        "inputs_sha256": _digest(json.dumps([
            {"id": case["id"], "prompt": _prompt(case), "sql": case["sql"]}
            for case in selected
        ], ensure_ascii=False, sort_keys=True).encode()),
        "case_file_sha256": _digest(raw),
        "selected_cases_sha256": _digest(json.dumps(
            selected, ensure_ascii=False, sort_keys=True,
        ).encode()),
        "required_dataset": required_dataset,
    }, selected


def validate_environment(
    database: DatabaseSettings, analysis: AnalysisSettings, cases: list[dict],
) -> DatabaseSettings:
    if (database.host, database.port, database.database, database.user) != (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    ):
        raise EvaluationError("评测只允许固定本地 db_agent_reader 数据源。")
    required = {table for case in cases for table in case["tables"]}
    granted = set(database.allowed_tables) & TABLES
    if not required.issubset(granted):
        raise EvaluationError("当前配置缺少所选案例需要的 ec 表授权；评测不会扩大权限。")
    defaults = AnalysisSettings.model_construct()
    if any(getattr(analysis, field) > getattr(defaults, field) for field in (
        "review_scan_rows", "review_join_rows", "review_sort_rows",
    )):
        raise EvaluationError("评测不能放宽默认扫描、连接或排序审核阈值。")
    # Intersection can only remove permissions. Never grant a table from the case file.
    return database.model_copy(update={"allowed_tables": tuple(sorted(granted))}, deep=True)


def _same_rows(actual: object, expected: list, ordered: bool) -> bool:
    if not isinstance(actual, list):
        return False
    # A multiset preserves duplicates while ignoring order the SQL never promised.
    def encode(row):
        return json.dumps(row, ensure_ascii=False, sort_keys=True)

    if ordered:
        return list(map(encode, actual)) == list(map(encode, expected))
    return Counter(map(encode, actual)) == Counter(map(encode, expected))


def assess_report(case: dict, report: dict) -> list[str]:
    expected = case["expected"]
    failures = []
    actual_result = report.get("result")
    if expected["status"] == "rejected" and (
        actual_result is not None or report.get("execution_status") != "not_started"
    ):
        failures.append("unsafe_execution")
    error = report.get("error")
    if error:
        code = error.get("code")
        if code in {"TIMEOUT", "RESULT_LIMIT", "RESPONSE_LIMIT", "MODEL_CALL_LIMIT",
                    "TOOL_CALL_LIMIT", "GRAPH_RECURSION_LIMIT"}:
            failures.append("budget")
        elif code in {"SYNTAX_ERROR", "SQL_REFERENCE_ERROR", "UNSUPPORTED_RESULT_TYPE",
                      "UNSUPPORTED_RESULT_VALUE", "INVALID_RESULT", "SEMANTIC_MISMATCH",
                      "SEMANTIC_UNCERTAIN"}:
            failures.append("data_error")
        else:
            failures.append("environment")
        return sorted(set(failures))
    if any(report.get(field) != expected[field] for field in (
        "status", "decision", "execution_status",
    )):
        failures.append("data_error")
    if expected["status"] == "ok":
        if not isinstance(actual_result, dict):
            failures.append("data_error")
        elif (
            not _same_rows(actual_result.get("rows"), expected["rows"], expected["ordered"])
            or actual_result.get("row_count") != len(expected["rows"])
            or actual_result.get("truncated") != expected["truncated"]
            or actual_result.get("server_statement_status") != (
                "unknown" if expected["truncated"] else "completed"
            )
            or actual_result.get("truncation_reason") != (
                "row_limit" if expected["truncated"] else None
            )
        ):
            failures.append("data_error")
    return sorted(set(failures))


def _trace(sql: str, report: dict) -> dict:
    result = report.get("result")
    return {
        # Explicit evaluation artifacts concern only this public synthetic fixture;
        # product RunRecord still never stores SQL or rows.
        "sql": sql,
        "sql_sha256": _digest(sql.encode()),
        "sql_fingerprint": report.get("sql_fingerprint"),
        "query_id": report.get("query_id"),
        "decision": report.get("decision"),
        "status": report.get("status"),
        "execution_status": report.get("execution_status"),
        "server_version": report.get("server_version"),
        "error_code": (report.get("error") or {}).get("code"),
        "rule_ids": [item["rule_id"] for item in report.get("findings", [])],
        "result": result,
    }


def _failure(exc: Exception) -> tuple[str, str]:
    from db_agent.agent import (
        AgentResponseError,
        GraphRecursionError,
        ModelCallLimitExceededError,
        ToolCallLimitExceededError,
    )

    if isinstance(exc, AgentResponseError):
        if exc.code in {"MODEL_CALL_LIMIT", "TOOL_CALL_LIMIT", "GRAPH_RECURSION_LIMIT", "TIMEOUT"}:
            return "budget", exc.code
        message = str(exc)
        if message == "模型输出达到 token 上限，请缩小问题或调整输出预算":
            return "budget", "TOKEN_LIMIT"
        if message == "模型或工具调用次数达到预算，已停止运行。":
            # `raise ... from None` hides traceback text but retains __context__.
            # Inspect only known exception types; never copy their messages or fields.
            for kind, code in (
                (ModelCallLimitExceededError, "MODEL_CALL_LIMIT"),
                (ToolCallLimitExceededError, "TOOL_CALL_LIMIT"),
                (GraphRecursionError, "GRAPH_RECURSION_LIMIT"),
            ):
                if isinstance(exc.__context__, kind):
                    return "budget", code
            return "budget", "AGENT_CALL_LIMIT"
    return "environment", "EVALUATION_CALL_FAILED"


async def run_case(
    case: dict, *, mode: str, connector: MetadataConnector,
    analysis: AnalysisSettings, query: QuerySettings, model: Settings | None,
) -> dict:
    started = time.monotonic()
    outcome = {
        "case_id": case["id"], "passed": False, "categories": [], "queries": [],
        "model_calls": 0 if mode == "sql" else None,
        "tool_calls": [] if mode == "sql" else None, "answer_sha256": None,
        "failure_code": None, "run_id": None,
        "input_sha256": _digest((case["sql"] if mode == "sql" else _prompt(case)).encode()),
        "explanation_scope": "not_applicable" if mode == "sql" else "not_observed",
        "free_semantics": "not_evaluated", "expected": case["expected"],
    }
    # The truncation exercise may tighten the result cap, never loosen configured limits.
    limits = query.model_copy(update={"max_rows": min(query.max_rows, case.get("max_rows", 100))})
    outcome["effective_query_limits"] = limits.model_dump()
    try:
        with RunRecord() as record:
            outcome["run_id"] = record.run_id
            if mode == "sql":
                report = await QueryService(connector, analysis, limits, record).execute(
                    case["sql"]
                )
                outcome["queries"] = [_trace(case["sql"], report)]
                outcome["categories"] = assess_report(case, report)
            else:
                from db_agent.agent import run_agent_observed
                from db_agent.presentation import render_queries

                if model is None:
                    raise EvaluationError("Agent 模式需要明确的模型配置。")
                # Only the business prompt goes to the real agent. Reference SQL and
                # oracle answers are never injected as a substitute for model reasoning.
                prompt = _prompt(case)
                outcome["prompt_sha256"] = _digest(prompt.encode())
                observed = await run_agent_observed(
                    prompt, model, connector, record, analysis, limits,
                )
                outcome.update(
                    queries=[_trace(item.sql, item.report) for item in observed.queries],
                    model_calls=observed.model_calls, tool_calls=observed.tool_calls,
                    answer_sha256=_digest(observed.answer.encode()),
                    semantic_reviews=observed.semantic_reviews,
                    query_intents=observed.query_intents,
                )
                if len(observed.queries) != 1:
                    outcome["categories"].append("data_error")
                else:
                    outcome["categories"].extend(assess_report(case, observed.queries[0].report))
                    if case["expected"]["status"] == "rejected" and (
                        observed.queries[0].sql.strip().removesuffix(";").rstrip()
                        != case["sql"].strip().removesuffix(";").rstrip()
                    ):
                        outcome["categories"].append("data_error")
                        outcome["failure_code"] = "REJECTION_INPUT_MISMATCH"
                if case["expected"]["status"] == "rejected" and any(
                    item.report.get("result") is not None
                    or item.report.get("execution_status") != "not_started"
                    for item in observed.queries
                ):
                    outcome["categories"].append("unsafe_execution")
                missing = max(
                    0, observed.tool_calls.count("execute_query") - len(observed.queries),
                )
                outcome["missing_query_reports"] = missing
                if missing or observed.tool_calls.count("execute_query") != 1:
                    outcome["categories"].append("data_error")
                if observed.answer != render_queries(observed.queries, missing_reports=missing):
                    outcome["categories"].append("explanation_error")
                else:
                    outcome["explanation_scope"] = "deterministic_query_rendering_only"
                if (
                    observed.model_calls > model.max_model_calls
                    or len(observed.tool_calls) > model.max_tool_calls
                ):
                    outcome["categories"].append("budget")
    except TimeoutError:
        outcome["categories"].append("budget")
        outcome["failure_code"] = "TIMEOUT"
    except Exception as exc:
        from db_agent.agent import AgentResponseError
        from db_agent.presentation import AgentRunResult

        category, code = _failure(exc)
        outcome["categories"].append(category)
        outcome["failure_code"] = code
        if isinstance(exc, AgentResponseError) and isinstance(exc.observation, AgentRunResult):
            observed = exc.observation
            outcome.update(
                queries=[_trace(item.sql, item.report) for item in observed.queries],
                model_calls=observed.model_calls, tool_calls=observed.tool_calls,
                semantic_reviews=observed.semantic_reviews, partial_observation=True,
                query_intents=observed.query_intents,
                missing_query_reports=max(
                    0, observed.tool_calls.count("execute_query") - len(observed.queries),
                ),
            )
    outcome["categories"] = sorted(set(outcome["categories"]))
    outcome["passed"] = not outcome["categories"]
    outcome["duration_ms"] = round((time.monotonic() - started) * 1000)
    return outcome


def source_identity() -> dict:
    paths = [*sorted((PROJECT_ROOT / "src/db_agent").glob("*.py")),
             PROJECT_ROOT / "uv.lock", PROJECT_ROOT / "scripts/evaluate_ecommerce.py"]
    hashes = {str(path.relative_to(PROJECT_ROOT)): _digest(path.read_bytes()) for path in paths}
    return {"files": hashes, "sha256": _digest(json.dumps(hashes, sort_keys=True).encode())}


async def evaluate(
    *, mode: str, split: str, repeat: int, database: DatabaseSettings,
    analysis: AnalysisSettings, query: QuerySettings, model: Settings | None = None,
    suite: str = "v1",
) -> dict:
    if mode not in {"sql", "agent"} or type(repeat) is not int or not 1 <= repeat <= 20:
        raise EvaluationError("模式必须为 sql/agent，repeat 必须在 1–20 范围内。")
    if mode == "agent" and model is None:
        raise EvaluationError("Agent 模式需要明确的模型配置。")
    manifest, cases = load_cases(split, suite=suite)
    scoped = validate_environment(database, analysis, cases)
    connector = MetadataConnector(scoped)
    report = {
        "evaluation_id": uuid4().hex, "started_at": datetime.now(UTC).isoformat(),
        "mode": mode, "split": split, "repeat": repeat, "suite": suite,
        "suite_version": manifest["suite_version"], "split_role": manifest["split_role"],
        "task_count": len(cases), "attempt_count": len(cases) * repeat,
        "case_manifest": manifest, "source": source_identity(),
        "environment": {"database": "db_agent", "host": "127.0.0.1", "port": 13306,
                        "allowed_tables": list(scoped.allowed_tables)},
        "analysis_limits": analysis.model_dump(), "query_limits": query.model_dump(),
        "model": None if mode == "sql" else {
            "configured_model": model.model,
            "endpoint_sha256": _digest(model.openai_base_url.encode()),
            "provider_reported_version": None,
            "max_model_calls": model.max_model_calls, "max_tool_calls": model.max_tool_calls,
            "max_output_tokens": model.max_output_tokens,
            "request_timeout_seconds": model.request_timeout_seconds,
            "run_timeout_seconds": model.run_timeout_seconds,
        },
        "packages": {name: version(name) for name in ("db-agent", "sqlglot", "langchain-openai")},
        "limitations": [
            "仅评测公开的本地合成电商任务，不代表生产正确率或性能。",
            "数据 oracle 验证本次返回值，不构成任意 SQL 的通用等价证明。",
            "回答检查只覆盖可信查询报告的程序渲染路径，不评估开放式解释语义。",
            "模型名称为配置别名，提供方未暴露的实际模型版本无法核验。",
            "案例要求默认百万订单数据；本报告不单独证明全部数据行数或导入完整性。",
        ],
        "attempts": [],
    }
    for iteration in range(1, repeat + 1):
        for case in cases:
            attempt = await run_case(
                case, mode=mode, connector=connector, analysis=analysis, query=query, model=model,
            )
            attempt["iteration"] = iteration
            report["attempts"].append(attempt)
            state = "PASS" if attempt["passed"] else ",".join(attempt["categories"])
            print(f"{suite}/{mode}/{split} {iteration}/{repeat} {case['id']}: {state}", flush=True)
    report["passed_count"] = sum(item["passed"] for item in report["attempts"])
    report["failed_count"] = report["attempt_count"] - report["passed_count"]
    report["category_counts"] = {
        name: sum(name in item["categories"] for item in report["attempts"]) for name in CATEGORIES
    }
    report["completed_at"] = datetime.now(UTC).isoformat()
    return report


def write_report(report: dict) -> Path:
    directory = PROJECT_ROOT / "outputs" / "evals"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{report['evaluation_id']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="显式运行固定本地电商业务评测")
    parser.add_argument("--suite", choices=tuple(SUITE_FILES), default="v1")
    parser.add_argument("--mode", choices=("sql", "agent"), required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--repeat", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(evaluate(
            mode=args.mode, split=args.split, repeat=args.repeat, suite=args.suite,
            database=load_database_settings(), analysis=load_analysis_settings(),
            query=load_query_settings(), model=load_settings() if args.mode == "agent" else None,
        ))
        path = write_report(report)
    except (EvaluationError, ConfigurationError) as exc:
        print(str(exc))
        return 2
    except KeyboardInterrupt:
        print("评测已中断；不把未完成尝试计为通过。")
        return 130
    except Exception:
        print("评测或报告写入失败；未输出原始异常。")
        return 1
    print(f"{args.mode}/{args.split}: {report['passed_count']}/{report['attempt_count']} 通过")
    print(f"报告：{path}")
    return 1 if report["failed_count"] else 0
