"""Explicit local synthetic acceptance; real model runs never execute in default CI."""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from db_agent.agent import AgentResponseError, run_agent_observed
from db_agent.config import load_database_settings, load_settings
from db_agent.db import MetadataConnector


def command(*args, document=None):
    process = subprocess.run(
        [sys.executable, "-m", "db_agent", "knowledge", *args],
        input=json.dumps(document, ensure_ascii=False) if document is not None else None,
        text=True, capture_output=True, timeout=30, check=False,
    )
    if process.returncode:
        raise RuntimeError("knowledge management command failed; no automatic retry")
    return json.loads(process.stdout)


def worker(prompt):
    try:
        result = asyncio.run(run_agent_observed(
            prompt, load_settings(), MetadataConnector(load_database_settings()),
        ))
        return asdict(result)
    except AgentResponseError as exc:
        return {"error": {"code": exc.code, "message": str(exc)},
                "observation": asdict(exc.observation) if exc.observation else None}


def run_case(prompt, expected, expected_knowledge):
    # New process and new Agent context: no requests/history/answers are inherited.
    completed = subprocess.run(
        [sys.executable, __file__, "--worker", prompt],
        capture_output=True, text=True, timeout=90, check=False,
    )
    result = json.loads(completed.stdout) if completed.returncode == 0 else {"worker_failed": True}
    queries = result.get("queries", [])
    ok = len(queries) == 1 and queries[0]["report"].get("status") == "ok"
    if ok:
        report = queries[0]["report"]
        ok = (report.get("decision") == "ALLOW" and report.get("execution_status") == "completed"
              and report.get("result", {}).get("truncated") is False
              and report.get("result", {}).get("rows") == expected
              and {x["id"]: x["digest"] for x in report.get("business_knowledge", [])}
              == expected_knowledge
              and result["model_calls"] <= 4 and len(result["tool_calls"]) <= 6)
    return {"prompt": prompt, "expected_rows": expected, "passed": ok, "result": result}


def source_hashes():
    paths = [*Path("src/db_agent").glob("*.py"), Path(__file__),
             Path("tests/fixtures/mysql_business.sql")]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="authorize real local synthetic/model calls")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    settings = load_database_settings()
    if (settings.host, settings.port, settings.database, settings.user) != (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    ) or not {"orders", "customers"} <= set(settings.allowed_tables):
        parser.error("requires the explicitly configured local reader and public fixture")
    if args.worker:
        print(json.dumps(worker(args.worker), ensure_ascii=False))
        return 0
    if not args.run:
        parser.error("pass --run for real model calls and local knowledge writes")
    run_id = uuid4().hex
    frozen = source_hashes()
    base = {
        "definition": "支付成交额为status='paid'订单total_amount之和，笔数为COUNT(*)；"
                      "按paid_at统计日期，时间区间左闭右开，不含取消和已退款订单。",
        "source": "tests/fixtures/mysql_business.sql",
        "source_version": "public-small-fixture-v1",
        "invalidation_condition": "表结构、状态语义或指标口径变化时撤销；"
                                  "来源变更须人工创建新版本。",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "tables": ["orders"], "kind": "metric", "title": "支付成交额",
    }
    documents = [
        base,
        base | {"kind": "sql_template", "title": "支付成交额 SQL 模板",
                "sql": "SELECT COUNT(*) AS n, SUM(total_amount) AS amount FROM orders "
                       "WHERE status = 'paid' AND paid_at >= '2026-02-01' "
                       "AND paid_at < '2026-03-01'"},
        base | {"kind": "relationship", "title": "订单归属客户关系",
                "definition": "orders.customer_id对应customers.id；按当前完整外键列对关联。",
                "tables": ["orders", "customers"],
                "relationship": {"table": "orders", "columns": ["customer_id"],
                                 "referenced_table": "customers", "referenced_columns": ["id"]}},
    ]
    confirmed = []
    for document in documents:
        draft = command("create", "--stdin", document=document)
        # This script's synthetic definitions are explicitly fixed and reviewed here;
        # production knowledge is reviewed by a human using show then exact digest.
        shown = command("show", draft["id"])
        assert shown["digest"] == draft["digest"] and shown["state"] == "draft"
        confirmed.append(command("confirm", draft["id"], "--digest", draft["digest"]))
    metric, template, relation = [f"[[knowledge:{item['id']}]]" for item in confirmed]
    cases = [
        (f"{metric} 统计2026年2月支付成交额，只返回笔数n和金额amount。", [[3, "130.00"]]),
        (f"{metric} 本次明确改按created_at统计2026年2月，其他成交口径不变，"
         "只返回笔数n和金额amount。", [[2, "30.00"]]),
        (f"{template} 使用此模板统计2026年1月，日期条件完整改成1月，"
         "只返回笔数n和金额amount。", [[0, None]]),
        (f"{metric} {relation} 统计2026年2月east地区客户的支付成交额，"
         "客户地区使用customers.region，只返回笔数n和金额amount。", [[2, "130.00"]]),
    ]
    results = []
    for prompt, expected in cases:
        if source_hashes() != frozen:
            raise RuntimeError("source changed during acceptance; stop without reusing results")
        expected_knowledge = {item["id"]: item["digest"] for item in confirmed
                              if f"[[knowledge:{item['id']}]]" in prompt}
        results.append(run_case(prompt, expected, expected_knowledge))
    source_unchanged = source_hashes() == frozen
    command("revoke", confirmed[0]["id"], "--reason", "synthetic acceptance lifecycle test")
    revoked = worker(cases[0][0])
    revoked_ok = revoked.get("error", {}).get("code") == "KNOWLEDGE_UNAVAILABLE"
    report = {
        "run_id": run_id, "timestamp": datetime.now(UTC).isoformat(),
        "evidence": "real configured model + local MySQL fixture; 4 fresh worker processes",
        "role": "exposed fixed acceptance/regression; not unbiased accuracy",
        "source_sha256": frozen, "source_unchanged": source_unchanged,
        "confirmed": confirmed, "cases": results,
        "passed": sum(case["passed"] for case in results), "planned": len(cases),
        "revoked_reference": {"passed": revoked_ok, "result": revoked},
    }
    path = Path("outputs/evals") / f"knowledge-{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(path), "passed": report["passed"], "planned": 4,
                      "revoked_reference_passed": revoked_ok}))
    return 0 if report["passed"] == 4 and revoked_ok and source_unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
