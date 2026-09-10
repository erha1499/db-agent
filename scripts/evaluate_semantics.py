"""Explicit semantic regression: controlled schemas and real reviews, never business SQL."""

import argparse
import asyncio
import hashlib
import json
import re
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient

from db_agent.agent import AgentResponseError, RuntimeMiddleware
from db_agent.config import (
    AnalysisSettings,
    ConfigurationError,
    DatabaseSettings,
    Settings,
    load_analysis_settings,
    load_database_settings,
    load_settings,
)
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.evaluation import (
    PROJECT_ROOT,
    TABLES,
    EvaluationError,
    source_identity,
    validate_environment,
    write_report,
)
from db_agent.policy import check_sql
from db_agent.records import RunRecord
from db_agent.semantics import review_messages

CASE_FILE = PROJECT_ROOT / "evals" / "semantic-regression.json"
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_digest(value: object) -> str:
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def load_cases(analysis: AnalysisSettings) -> tuple[dict, list[dict]]:
    """Load only the fixed public development suite; derive tables without database access."""
    try:
        raw = CASE_FILE.read_bytes()
        if len(raw) > 262144:
            raise ValueError
        document = json.loads(raw)
        cases = document["cases"]
        if (
            document["version"] != "semantic-regression-v1"
            or not isinstance(cases, list)
            or not 1 <= len(cases) <= 100
        ):
            raise ValueError
        ids = set()
        selected = []
        for case in cases:
            if (
                set(case) != {"id", "prompt", "sql", "expected_verdict", "dimension"}
                or not isinstance(case["id"], str)
                or not _LABEL.fullmatch(case["id"])
                or case["id"] in ids
                or not isinstance(case["dimension"], str)
                or not _LABEL.fullmatch(case["dimension"])
                or case["expected_verdict"] not in {"match", "mismatch", "uncertain"}
                or not isinstance(case["prompt"], str)
                or not 1 <= len(case["prompt"].strip()) <= 8192
                or not isinstance(case["sql"], str)
            ):
                raise ValueError
            # This only establishes the fixed case's referenced tables. Actual
            # authorization is intersected with configured grants before any I/O.
            checked = check_sql(case["sql"], "db_agent", tuple(sorted(TABLES)), analysis)
            if checked.decision != "ALLOW":
                raise ValueError
            ids.add(case["id"])
            selected.append({**case, "tables": list(checked.tables)})
    except (OSError, ValueError, KeyError, TypeError):
        raise EvaluationError("固定语义开发集无效或不符合当前静态检查范围。") from None
    return {
        "version": document["version"],
        "role": "exposed_development_regression",
        "case_file": "evals/semantic-regression.json",
        "case_file_sha256": _digest(raw),
        "cases_sha256": _json_digest(cases),
        "inputs_sha256": _json_digest(
            [
                {"id": case["id"], "user_request": case["prompt"], "candidate_sql": case["sql"]}
                for case in cases
            ]
        ),
    }, selected


def _source() -> dict:
    identity = source_identity()
    files = {
        **identity["files"],
        "scripts/evaluate_semantics.py": _digest(
            Path(__file__).read_bytes(),
        ),
    }
    return {"files": files, "sha256": _json_digest(files)}


def _input(sql: str, prompt: str, schemas: list[dict]) -> str:
    return _json_digest(
        [
            {"type": message.type, "content": message.content}
            for message in review_messages(prompt, sql, schemas)
        ]
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "RUN_TIMEOUT"
    if isinstance(exc, AgentResponseError):
        return (
            exc.code
            if isinstance(exc.code, str) and exc.code in {
                "TIMEOUT", "MODEL_CALL_LIMIT", "TOOL_CALL_LIMIT", "GRAPH_RECURSION_LIMIT",
            }
            else "AGENT_RESPONSE_ERROR"
        )
    if isinstance(exc, DatabaseError):
        return (
            exc.code
            if exc.code
            in {
                "SEMANTIC_REVIEW_INVALID",
                "SEMANTIC_REVIEW_FAILED",
                "TIMEOUT",
                "RESPONSE_LIMIT",
            }
            else "DATABASE_ERROR"
        )
    return "EVALUATION_ERROR"


async def run_case(
    case: dict,
    *,
    connector: MetadataConnector,
    analysis: AnalysisSettings,
    settings: Settings,
) -> dict:
    """Review the original once and at most one proposed repair, without executing either."""
    started = time.monotonic()
    record = RunRecord()
    runtime = None
    outcome = {
        "case_id": case["id"],
        "dimension": case["dimension"],
        "run_id": record.run_id,
        "user_request": case["prompt"],
        "candidate_sql": case["sql"],
        "expected_verdict": case["expected_verdict"],
        "actual_verdict": None,
        "review": None,
        "input_sha256": None,
        "schemas_sha256": None,
        "detection_passed": False,
        "error_code": None,
        "repair": {
            "suggested": False,
            "attempted": False,
            "static_decision": None,
            "review": None,
            "input_sha256": None,
            "passed": None,
            "error_code": None,
        },
    }
    phase = "detection"
    try:
        with record:
            async with asyncio.timeout(settings.run_timeout_seconds):
                async with AsyncExitStack() as clients:
                    clients.enter_context(tracing_context(enabled=False))
                    http = clients.enter_context(DefaultHttpxClient())
                    http_async = await clients.enter_async_context(DefaultAsyncHttpxClient())
                    model = ChatOpenAI(
                        model=settings.model,
                        api_key=settings.api_key,
                        base_url=settings.openai_base_url,
                        timeout=settings.request_timeout_seconds,
                        max_retries=0,
                        max_tokens=settings.max_output_tokens,
                        streaming=False,
                        use_responses_api=False,
                        http_client=http,
                        http_async_client=http_async,
                    )
                    runtime = RuntimeMiddleware(
                        record,
                        {"describe_table"},
                        settings=settings,
                        model=model,
                        prompt=case["prompt"],
                        connector=connector,
                        analysis_limits=analysis,
                        executions=[],
                    )
                    checked = connector.check_sql(case["sql"], analysis)
                    if checked.decision != "ALLOW":
                        raise DatabaseError("STATIC_REJECTED", "候选未通过静态检查。")
                    schemas = await runtime._query_schemas(checked.tables)
                    outcome["schemas_sha256"] = _json_digest(schemas)
                    outcome["input_sha256"] = _input(case["sql"], case["prompt"], schemas)
                    review = await runtime.review_sql(case["sql"], schemas)
                    outcome.update(
                        review=review.model_dump(),
                        actual_verdict=review.verdict,
                        detection_passed=review.verdict == case["expected_verdict"],
                    )
                    replacement = review.replacement_sql
                    if review.verdict == "mismatch" and replacement is not None:
                        phase = "repair"
                        repair = outcome["repair"]
                        repair.update(suggested=True, passed=False)
                        checked = connector.check_sql(replacement, analysis)
                        repair["static_decision"] = checked.decision
                        if checked.decision == "ALLOW" and replacement != case["sql"]:
                            schemas = await runtime._query_schemas(checked.tables)
                            repair["input_sha256"] = _input(replacement, case["prompt"], schemas)
                            repair["attempted"] = True
                            reviewed = await runtime.review_sql(replacement, schemas)
                            repair.update(
                                review=reviewed.model_dump(), passed=reviewed.verdict == "match"
                            )
    except Exception as exc:
        if phase == "repair":
            outcome["repair"]["passed"] = False
            outcome["repair"]["error_code"] = _error_code(exc)
        else:
            outcome["detection_passed"] = False
            outcome["error_code"] = _error_code(exc)
    outcome.update(
        model_calls=runtime.model_calls if runtime else 0,
        tool_calls=runtime.tool_calls.copy() if runtime else [],
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return outcome


async def evaluate(
    *,
    repeat: int,
    database: DatabaseSettings,
    analysis: AnalysisSettings,
    settings: Settings,
) -> dict:
    if type(repeat) is not int or not 1 <= repeat <= 20:
        raise EvaluationError("repeat 必须在 1–20 范围内。")
    if (settings.max_model_calls, settings.max_tool_calls, settings.run_timeout_seconds) != (
        4,
        6,
        60,
    ):
        raise EvaluationError("语义开发评测要求模型/工具/总秒数预算为 4/6/60。")
    manifest, cases = load_cases(analysis)
    scoped = validate_environment(database, analysis, cases)
    connector = MetadataConnector(scoped)
    report = {
        "evaluation_id": uuid4().hex,
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "semantic_review",
        "repeat": repeat,
        "task_count": len(cases),
        "attempt_count": len(cases) * repeat,
        "case_manifest": manifest,
        "source": _source(),
        "environment": {
            "host": "127.0.0.1",
            "port": 13306,
            "database": "db_agent",
            "user": "db_agent_reader",
            "allowed_tables": list(scoped.allowed_tables),
        },
        "analysis_limits": analysis.model_dump(),
        "metadata_limits": {
            name: getattr(scoped, name)
            for name in (
                "connect_timeout_seconds",
                "metadata_timeout_seconds",
                "max_metadata_rows",
                "max_metadata_bytes",
            )
        },
        "model": {
            "configured_model": settings.model,
            "endpoint_sha256": _digest(settings.openai_base_url.encode()),
            "provider_reported_version": None,
            **{
                name: getattr(settings, name)
                for name in (
                    "max_model_calls",
                    "max_tool_calls",
                    "max_output_tokens",
                    "request_timeout_seconds",
                    "run_timeout_seconds",
                )
            },
        },
        "limitations": [
            "固定公开开发集的语义审查回归，不是未见题的泛化正确率。",
            "只读取受控元数据，不执行候选 SQL、EXPLAIN 或业务查询，不证明实际业务结果。",
            "修正通过仅表示静态检查及第二次语义审查通过，不是独立 SQL 等价证明或执行授权。",
            "检测只比较原始审查与标注；没有修正建议不会把正确的 mismatch 检测计为失败。",
            "模型名称是配置别名，未核验提供方实际版本；原始内容仅适用于公开合成案例。",
        ],
        "attempts": [],
    }
    for iteration in range(1, repeat + 1):
        for case in cases:
            outcome = await run_case(
                case, connector=connector, analysis=analysis, settings=settings
            )
            outcome["iteration"] = iteration
            report["attempts"].append(outcome)
            state = "PASS" if outcome["detection_passed"] else "FAIL"
            print(f"semantic {iteration}/{repeat} {case['id']}: {state}", flush=True)
    attempts = report["attempts"]
    report.update(
        completed_at=datetime.now(UTC).isoformat(),
        completed_attempt_count=len(attempts),
        detection_passed_count=sum(item["detection_passed"] for item in attempts),
        detection_failed_count=sum(not item["detection_passed"] for item in attempts),
        repair_suggested_count=sum(item["repair"]["suggested"] for item in attempts),
        repair_review_attempt_count=sum(item["repair"]["attempted"] for item in attempts),
        repair_passed_count=sum(item["repair"]["passed"] is True for item in attempts),
        repair_failed_count=sum(item["repair"]["passed"] is False for item in attempts),
        detection_error_count=sum(item["error_code"] is not None for item in attempts),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定本地数据源的语义开发回归；不执行业务 SQL")
    parser.add_argument("--repeat", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            evaluate(
                repeat=args.repeat,
                database=load_database_settings(),
                analysis=load_analysis_settings(),
                settings=load_settings(),
            )
        )
        path = write_report(report)
    except (ConfigurationError, EvaluationError):
        print("评测配置或固定用例无效；请核对本地 reader、授权表及 4/6/60 预算。")
        return 2
    except KeyboardInterrupt:
        print("评测已中断；未完成的评测不能记为通过。")
        return 130
    except Exception:
        print("评测或报告写入失败；未输出原始异常。")
        return 1
    print(f"检测：{report['detection_passed_count']}/{report['attempt_count']} 通过")
    print(f"修正：{report['repair_passed_count']}/{report['repair_suggested_count']} 建议复审通过")
    print(f"报告：{path}")
    return int(bool(report["detection_failed_count"] or report["repair_failed_count"]))


if __name__ == "__main__":
    raise SystemExit(main())
