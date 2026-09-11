"""Explicit real-model acceptance on the fixed local PostgreSQL public fixture."""

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import sqlglot
from sqlglot import exp

from db_agent.agent import AgentResponseError, run_agent_observed
from db_agent.config import (
    AnalysisSettings,
    PostgreSQLSettings,
    QuerySettings,
    load_database_settings,
    load_settings,
)
from db_agent.connectors import create_connector
from db_agent.presentation import render_queries
from db_agent.query import QueryService

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/postgres_business.sql"
VERSION = "postgres-case-v1"
TABLES = {"customers", "orders", "order_items"}
MODEL_BUDGETS = {
    "request_timeout_seconds": 30, "run_timeout_seconds": 60,
    "max_output_tokens": 1024, "max_model_calls": 4, "max_tool_calls": 6,
}
DATABASE_BUDGETS = {
    "connect_timeout_seconds": 3, "metadata_timeout_seconds": 5,
    "max_metadata_rows": 200, "max_metadata_bytes": 32768,
}
BUSINESS = (
    "请实际查询当前授权的 PostgreSQL 公开合成业务表，只执行一条完整业务查询。"
    "orders 是订单，customers 是客户，order_items 是订单明细；使用工具取得真实结构。"
    "orders.status 的 paid 表示当前已支付，cancelled 表示取消，refunded 表示已退款，"
    "pending 表示待支付；total_amount 是订单原始金额，取消与已退款金额不是成交收入。"
    "本题使用 searched CASE WHEN 条件聚合，不使用简单 CASE 或 IF、子查询、CTE、窗口。"
    "所有字段和别名使用英文 ASCII。"
)

# Human-derived expectations from the public fixture, never passed to the Agent.
# A different valid SQL is accepted if it uses CASE and returns this exact contract.
CASES = [
    {
        "id": "status_counts_and_amounts", "minimum_cases": 6,
        "question": "统计全部 orders，不加日期限制。分别统计 paid、cancelled、refunded 的"
                    "订单数及其原始金额之和；不匹配的订单不计数，金额分支用 ELSE 0。"
                    "按顺序返回 paid_count、paid_sum、cancelled_count、cancelled_sum、"
                    "refunded_count、refunded_sum 六列，全部状态放在同一结果行。",
        "columns": ["paid_count", "paid_sum", "cancelled_count", "cancelled_sum",
                    "refunded_count", "refunded_sum"],
        "kinds": ["integer", "decimal"] * 3,
        "expected": [[3, "130.00", 1, "50.00", 1, "50.00"]],
    },
    {
        "id": "customers_including_no_orders", "minimum_cases": 2,
        "question": "以全部 customers 为范围，LEFT JOIN orders 并保留没有订单的客户；"
                    "按客户统计 status='paid' 的订单数和 total_amount 之和，不加日期限制。"
                    "数量的 CASE 不匹配时为 NULL，金额的 CASE 不匹配时为 0。"
                    "按顺序返回 customer_id、paid_count、paid_sum，按 customer_id 升序。",
        "columns": ["customer_id", "paid_count", "paid_sum"],
        "kinds": ["integer", "integer", "decimal"],
        "expected": [[1, 2, "130.00"], [2, 0, "0.00"], [3, 1, "0.00"],
                     [4, 0, "0.00"], [5, 0, "0.00"]],
    },
    {
        "id": "no_match_without_else", "minimum_cases": 2, "require_no_else": True,
        "question": "在全部 orders 上统计 status='paid' 且 total_amount>1000 的订单数"
                    "和 total_amount 之和。COUNT 与 SUM 都使用 CASE，且两个 CASE 均不写 ELSE；"
                    "无匹配时金额必须保留 NULL，不改成 0。按顺序返回 matched_count、matched_sum。",
        "columns": ["matched_count", "matched_sum"], "kinds": ["integer", "decimal"],
        "expected": [[0, None]],
    },
    {
        "id": "created_february", "minimum_cases": 2,
        "question": "按 orders.created_at 的业务日历统计2026年2月：时间范围从"
                    "2026-02-01 00:00:00（含）到2026-03-01 00:00:00（不含）。"
                    "created_at 是不带时区的创建时间，不改按 paid_at。仅统计当前 paid 订单，"
                    "用 CASE 分别计算订单数和 total_amount 之和，金额不匹配分支用 ELSE 0。"
                    "按顺序返回 paid_count、paid_sum。",
        "columns": ["paid_count", "paid_sum"], "kinds": ["integer", "decimal"],
        "expected": [[2, "30.00"]],
    },
    {
        "id": "paid_february_utc", "minimum_cases": 2,
        "question": "按 orders.paid_at 的 UTC 支付时间统计2026年2月：时间范围从"
                    "2026-02-01 00:00:00+00:00（含）到2026-03-01 00:00:00+00:00（不含）。"
                    "paid_at 是带时区的实际支付时刻，不改按 created_at。仅统计当前 paid 订单，"
                    "用 CASE 分别计算订单数和 total_amount 之和，金额不匹配分支用 ELSE 0。"
                    "按顺序返回 paid_count、paid_sum。",
        "columns": ["paid_count", "paid_sum"], "kinds": ["integer", "decimal"],
        "expected": [[3, "130.00"]],
    },
    {
        "id": "paid_item_quantity_and_net", "minimum_cases": 2,
        "question": "关联 orders 与 order_items，统计当前 status='paid' 订单的已支付商品"
                    "总件数和明细净金额，不加日期限制。件数为 quantity 之和，"
                    "每行净金额为 quantity*unit_price-discount_amount，折扣作用于整行；"
                    "不要重复累计 orders.total_amount。两项都使用 CASE，非 paid 分支为 0。"
                    "按顺序返回 paid_quantity、paid_net。",
        "columns": ["paid_quantity", "paid_net"], "kinds": ["integer", "decimal"],
        "expected": [[5, "130.00"]],
    },
]

# Full relevant fixture columns, including the extra pending row and month boundary.
# These probes use the same guarded reader service, are never model input, and are
# separate from case scoring. No admin, fixture import, permissions or writes here.
FIXTURE_PROBES = [
    ("customers", "SELECT id, region FROM customers ORDER BY id",
     [[1, "east"], [2, "west"], [3, None], [4, "east"], [5, "north"]]),
    ("orders", "SELECT id, customer_id, status, total_amount, created_at, paid_at "
     "FROM orders ORDER BY id", [
         [1001, 1, "paid", "100.00", "2026-01-31T23:59:59", "2026-02-01T00:01:00+00:00"],
         [1002, 1, "paid", "30.00", "2026-02-01T00:00:00", "2026-02-01T00:02:00+00:00"],
         [1003, 2, "cancelled", "50.00", "2026-02-02T10:00:00", None],
         [1004, 3, "paid", "0.00", "2026-02-02T11:00:00", "2026-02-02T11:00:00+00:00"],
         [1005, 5, "pending", "0.00", "2026-02-03T12:00:00", None],
         [1006, 2, "refunded", "50.00", "2026-02-04T13:00:00", "2026-02-04T13:05:00+00:00"],
     ]),
    ("order_items", "SELECT order_id, line_no, quantity, unit_price, discount_amount "
     "FROM order_items ORDER BY order_id, line_no", [
         [1001, 1, 2, "40.00", "0.00"], [1001, 2, 1, "20.00", "0.00"],
         [1002, 1, 1, "35.00", "5.00"], [1003, 1, 1, "50.00", "0.00"],
         [1004, 1, 1, "0.00", "0.00"], [1006, 1, 1, "50.00", "0.00"],
     ]),
]


def utc_now():
    return datetime.now(UTC).isoformat()


def source_hashes():
    paths = [Path(__file__).resolve(), FIXTURE, ROOT / "compose.postgres.yaml",
             ROOT / "scripts/setup_postgres.py", ROOT / "infra/postgres/init/10-reader.sh",
             ROOT / "uv.lock", ROOT / "pyproject.toml", *(ROOT / "src/db_agent").rglob("*.py")]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def configurations():
    model, database = load_settings(), load_database_settings()
    if not isinstance(database, PostgreSQLSettings) or (
        database.host, database.port, database.database, database.schema_name, database.user,
    ) != ("127.0.0.1", 15432, "db_agent_pg", "business", "db_agent_reader") or (
        set(database.allowed_tables) != TABLES or len(database.allowed_tables) != len(TABLES)
    ):
        raise ValueError("requires the fixed local PostgreSQL reader and three-table scope")
    # Pin defaults even if inherited shell/.env budgets are later enlarged.
    model = model.model_copy(update=MODEL_BUDGETS)
    database = database.model_copy(update=DATABASE_BUDGETS)
    return model, database


def default_limits(kind):
    return kind(_env_file=None, **{
        name: field.default for name, field in kind.model_fields.items()
    })


def complete(report):
    data = report.get("result")
    return (
        report.get("status") == "ok" and report.get("decision") == "ALLOW"
        and report.get("execution_status") == "completed" and report.get("error") is None
        and isinstance(data, dict) and data.get("truncated") is False
        and isinstance(data.get("rows"), list) and isinstance(data.get("columns"), list)
        and type(data.get("row_count")) is int and data["row_count"] == len(data["rows"])
    )


async def verify_fixture(database, analysis, query):
    connector = create_connector(database)
    evidence = {"identity": await connector.check(), "probes": []}
    if evidence["identity"] != {
        "connection_ok": True, "database": "db_agent_pg", "schema": "business",
        "dialect": "postgres", "server_version": "18.6", "readonly_identity_verified": True,
    }:
        raise ValueError("fixed PostgreSQL version or reader identity unconfirmed")
    service = QueryService(connector, analysis, query)
    for name, sql, expected in FIXTURE_PROBES:
        result = await service.execute(sql)
        evidence["probes"].append({"table": name, "sql": sql, "report": result})
        if not complete(result) or result["result"]["rows"] != expected:
            return {**evidence, "verified": False}
    return {**evidence, "verified": True}


def numeric_value(value, column_type, kind):
    if value is None:
        return None
    if kind == "integer" and type(value) is int and column_type in {"smallint", "int", "bigint"}:
        return Decimal(value)
    if (column_type == "decimal" and isinstance(value, str)
            and re.fullmatch(r"-?\d+(\.\d+)?", value)):
        number = Decimal(value)
        if kind == "decimal" or number == number.to_integral_value():
            return number
    raise ValueError("unverified numeric representation")


def assess(case, observation):
    """Called only after the real run; no reference query, model judgment or tolerance."""
    reasons = []
    if not isinstance(observation, dict):
        return ["MISSING_OBSERVATION"]
    queries = observation.get("queries", [])
    calls = observation.get("tool_calls", [])
    if (type(observation.get("model_calls")) is not int
            or not 1 <= observation["model_calls"] <= 4 or len(calls) > 6
            or calls.count("execute_query") != 1 or "describe_table" not in calls
            or len(queries) != 1):
        return ["INCOMPLETE_OR_OVER_BUDGET_TOOL_EVIDENCE"]
    execution = queries[0]
    report = execution["report"]
    if not complete(report):
        return ["QUERY_NOT_COMPLETE"]
    if not observation.get("semantic_reviews") or any(
        review.get("verdict") != "match" for review in observation["semantic_reviews"]
    ) or not observation.get("query_intents"):
        reasons.append("MISSING_SEMANTIC_EVIDENCE")
    try:
        tree = sqlglot.parse_one(execution["sql"], read="postgres")
        cases = list(tree.find_all(exp.Case))
        if len(cases) < case["minimum_cases"] or any(node.this is not None for node in cases):
            reasons.append("REQUESTED_SEARCHED_CASE_NOT_USED")
        if case.get("require_no_else") and any(
            node.args.get("default") is not None for node in cases
        ):
            reasons.append("REQUESTED_NO_ELSE_NOT_PRESERVED")
        data = report["result"]
        if [column["name"] for column in data["columns"]] != case["columns"]:
            reasons.append("COLUMN_CONTRACT_MISMATCH")
        actual = [[numeric_value(value, column["type"], kind)
                   for value, column, kind in zip(row, data["columns"], case["kinds"], strict=True)]
                  for row in data["rows"]]
        expected = [[None if value is None else Decimal(value) for value in row]
                    for row in case["expected"]]
        if actual != expected:
            reasons.append("INDEPENDENT_ROWS_MISMATCH")
    except (KeyError, TypeError, ValueError, sqlglot.errors.ParseError):
        reasons.append("INVALID_RESULT_CONTRACT")
    return reasons


def save_report(path, report):
    report["passed"] = sum(case["status"] == "passed" for case in report["cases"])
    report["attempted"] = sum(case["status"] != "not_run" for case in report["cases"])
    report["failed"] = sum(case["status"] == "failed" for case in report["cases"])
    report["not_run"] = report["planned"] - report["attempted"]
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    os.replace(temporary, path)


async def evaluate(repeat):
    model, database = configurations()
    analysis, query = default_limits(AnalysisSettings), default_limits(QuerySettings)
    before = source_hashes()
    plan = [{"id": case["id"], "trial": trial, "status": "not_run", "passed": False,
             "prompt": BUSINESS + case["question"], "expected_columns": case["columns"],
             "expected_rows": case["expected"], "observation": None, "reasons": []}
            for trial in range(1, repeat + 1) for case in CASES]
    report = {
        "version": VERSION, "run_id": uuid4().hex, "started_at": utc_now(), "finished_at": None,
        "evidence": "real configured model + guarded local PostgreSQL 18.6 reader",
        "role": "exposed_fixed_acceptance_regression_not_unbiased_accuracy",
        "source_sha256": before, "source_unchanged": None, "configuration_unchanged": None,
        "model": model.model, "budgets": {"model": MODEL_BUDGETS, "metadata": DATABASE_BUDGETS,
                                          "analysis": analysis.model_dump(),
                                          "query": query.model_dump(), "model_max_retries": 0},
        "target": {"host": database.host, "port": database.port, "database": database.database,
                   "schema": database.schema_name, "user": database.user,
                   "allowed_tables": sorted(database.allowed_tables)},
        "requested_repeat": repeat, "planned": len(plan), "cases": plan,
        "preflight": None, "postflight": None, "stop_reason": None,
    }
    directory = ROOT / "outputs/evals/postgres"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{report['run_id']}.json"
    save_report(path, report)
    try:
        report["preflight"] = await verify_fixture(database, analysis, query)
        if not report["preflight"]["verified"]:
            report["stop_reason"] = "FIXTURE_UNCONFIRMED"
        else:
            for planned in plan:
                if source_hashes() != before or configurations() != (model, database):
                    report["stop_reason"] = "SOURCE_OR_CONFIGURATION_CHANGED"
                    break
                case = next(case for case in CASES if case["id"] == planned["id"])
                planned["started_at"] = utc_now()
                planned["status"] = "running"
                save_report(path, report)
                print(json.dumps({"event": "case_started", "id": case["id"],
                                  "trial": planned["trial"]}), flush=True)
                try:
                    # Only the public task is input. No expected values, fixture rows,
                    # candidate/reference SQL, previous requests or knowledge are supplied.
                    observed = await run_agent_observed(
                        planned["prompt"], model, create_connector(database),
                        analysis_settings=analysis, query_settings=query,
                    )
                    planned["observation"] = asdict(observed)
                    planned["reasons"] = assess(case, planned["observation"])
                    if observed.queries and observed.answer != render_queries(observed.queries):
                        planned["reasons"].append("ANSWER_NOT_TRUSTED_REPORT_RENDERING")
                except AgentResponseError as exc:
                    planned["observation"] = asdict(exc.observation) if exc.observation else None
                    planned["reasons"] = [exc.code or "AGENT_RESPONSE_ERROR"]
                except Exception as exc:
                    # Exception text/tracebacks may contain endpoint or provider data.
                    planned["reasons"] = ["UNEXPECTED_" + type(exc).__name__]
                planned["finished_at"] = utc_now()
                planned["passed"] = not planned["reasons"]
                planned["status"] = "passed" if planned["passed"] else "failed"
                if source_hashes() != before or configurations() != (model, database):
                    planned["passed"], planned["status"] = False, "failed"
                    planned["reasons"].append("SOURCE_OR_CONFIGURATION_CHANGED_DURING_CASE")
                    report["stop_reason"] = "SOURCE_OR_CONFIGURATION_CHANGED"
                save_report(path, report)
                print(json.dumps({"event": "case_finished", "id": case["id"],
                                  "trial": planned["trial"], "status": planned["status"],
                                  "reasons": planned["reasons"]}), flush=True)
                if report["stop_reason"]:
                    break
            if not report["stop_reason"]:
                report["postflight"] = await verify_fixture(database, analysis, query)
                if not report["postflight"]["verified"]:
                    report["stop_reason"] = "FIXTURE_CHANGED_OR_UNCONFIRMED"
    except asyncio.CancelledError:
        report["stop_reason"] = "INTERRUPTED"
        raise
    except Exception as exc:
        report["stop_reason"] = "ACCEPTANCE_STOPPED_" + type(exc).__name__
    finally:
        for planned in plan:
            if planned["status"] == "running":
                planned.update(status="failed", passed=False, reasons=["INTERRUPTED"],
                               finished_at=utc_now())
        report["finished_at"] = utc_now()
        try:
            report["source_unchanged"] = source_hashes() == before
            report["configuration_unchanged"] = configurations() == (model, database)
        except Exception:
            report["source_unchanged"] = report["configuration_unchanged"] = False
        save_report(path, report)
    summary = {key: report[key] for key in (
        "passed", "failed", "not_run", "attempted", "planned", "source_unchanged",
        "configuration_unchanged", "stop_reason",
    )}
    print(json.dumps({"report": str(path), **summary}), flush=True)
    return 0 if report["passed"] == report["planned"] and report["source_unchanged"] and (
        report["configuration_unchanged"] and report["stop_reason"] is None
    ) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True,
                        help="explicitly authorize real model calls and fixed local reader queries")
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=1,
                        help="preplanned complete trials; no automatic failed-case retries")
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("run from this repository root; configuration is the current .env")
    try:
        return asyncio.run(evaluate(args.repeat))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("PostgreSQL acceptance stopped before a valid report; no success claim.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
