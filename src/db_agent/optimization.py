"""Result comparison over checked snapshots; no model, writes or reusable grants."""

import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime
from statistics import median
from uuid import uuid4

import sqlglot
from pydantic import BaseModel, ConfigDict, StrictStr

from db_agent.config import AnalysisSettings, QuerySettings
from db_agent.db import MetadataConnector
from db_agent.records import RunRecord
from db_agent.result_delivery import NUMERIC_TYPES, number, validate_result


class ComparisonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original: StrictStr
    candidate: StrictStr


def _statement_report(item: dict) -> dict:
    assessment = item["assessment"]
    return {
        "status": "error" if item["error"] else "ok" if item["result"] is not None else "rejected",
        "decision": item["decision"], "execution_status": item["execution_status"],
        "error": item["error"], "result": item["result"],
        "sql_fingerprint": item["check"].sql_fingerprint,
        "findings": [*item["check"].findings, *(assessment.findings if assessment else ())],
        "plan_summary": assessment.summary if assessment else None,
        "server_version": item["server_version"],
        "select_duration_ms": item["select_duration_ms"],
    }


def _rows_key(result: dict, ordered: bool):
    rows = []
    for row in result["rows"]:
        cells = []
        for column, value in zip(result["columns"], row, strict=True):
            if value is None:
                cells.append(("null", None))
            elif column["type"] in NUMERIC_TYPES:
                # Decimal construction/equality is exact, independent of decimal
                # arithmetic context precision. No rounding or tolerance is used.
                cells.append((column["type"], number(value)))
            else:
                cells.append((type(value).__name__, value))
        rows.append(tuple(cells))
    return rows if ordered else Counter(rows)


def compare_results(original: dict, candidate: dict, *, ordered: bool) -> str:
    """Compare complete typed results only; this function alone proves no snapshot."""
    left, right = validate_result(original), validate_result(candidate)
    if left["truncated"] or right["truncated"]:
        raise ValueError("Incomplete result")
    if left["columns"] != right["columns"]:
        return "column_contract_differs"
    return "rows_match" if _rows_key(left, ordered) == _rows_key(right, ordered) else "rows_differ"


def _structure(original: str, candidate: str) -> dict:
    left, right = (sqlglot.parse_one(sql, read="mysql") for sql in (original, candidate))
    orders = [tree.args.get("order") is not None for tree in (left, right)]
    return {
        "ast_identical": left == right,
        "row_mode": "ordered" if any(orders) else "multiset",
        "order_present": {"original": orders[0], "candidate": orders[1]},
        "order_contract_changed": orders[0] != orders[1],
        "limit_present": {name: tree.args.get("limit") is not None
                          for name, tree in (("original", left), ("candidate", right))},
        "general_equivalence_proven": False,
    }


class OptimizationService:
    def __init__(
        self, connector: MetadataConnector, analysis_limits: AnalysisSettings,
        query_limits: QuerySettings, record: RunRecord | None = None, *, before_select=None,
    ):
        self.connector = connector
        self.analysis_limits = analysis_limits.model_copy(deep=True)
        self.query_limits = query_limits.model_copy(deep=True)
        self.record = record
        self.before_select = before_select

    async def compare(self, original: str, candidate: str, *, repeat: int = 1) -> dict:
        if type(repeat) is not int or not 1 <= repeat <= 3:
            raise ValueError("比较次数须为 1–3。")
        request = ComparisonInput(original=original, candidate=candidate)
        started = time.monotonic()
        report = {
            "comparison_id": uuid4().hex, "checked_at": datetime.now(UTC).isoformat(),
            "database": self.connector.database, "source_scope": self.connector.knowledge_scope,
            "statements": None,
            "outcome": "inconclusive", "reason": "incomplete_evidence",
            "general_equivalence_proven": False, "independent_oracle_checked": False,
            "structure": None, "requested_trials": repeat, "completed_trials": 0,
            "trials": [], "performance": None, "error": None,
            "limits": {"analysis": self.analysis_limits.model_dump(),
                       "query": self.query_limits.model_dump()},
            "limitations": [
                "observed_equal 仅表示本次只读快照内完整 SQL 结果一致，不是通用等价证明。",
                "原 SQL 是用户给定基准，不自动成为独立业务 oracle；结构相同也不是执行授权。",
                "列标签、类型和位置均须一致；无 ORDER BY 比较多重集，保留重复行次数。",
                "有 ORDER BY 比较返回序列；并列键及 LIMIT 的选择可能不确定。"
                "添加或删除 ORDER BY 时，即使行相同也不确认顺序合同一致。",
                "NULL 独立于零和文本；数值精确比较，不忽略浮点差异；文本不模拟 MySQL collation。",
                "快照只覆盖已检查的 InnoDB 非锁定读取。后续提交不可见，重复试验间是不同快照；"
                "发现跨次结果变化时结论不确定。未返回服务器 warning。",
                "EXPLAIN 是估算；select_duration_ms 是客户端派发至 EOF/游标清理，"
                "包含网络和结果编码，不是服务器纯执行耗时或实测扫描行数。",
                "按 AB/BA 交替顺序观测，不清缓存；少量本地运行不能证明一般性能提升。"
                "每对共用原查询总预算，每侧保留分析及执行预算。",
                "SQL 的 WHERE/LIMIT 范围不等于全库；截断、失败或缺证据不能比较为相等。"
                "业务行不会发送给模型。",
            ],
        }
        baseline = None
        try:
            # Do not parse rejected SQL with a separate, potentially looser parser.
            checks = [self.connector.check_sql(sql, self.analysis_limits)
                      for sql in (request.original, request.candidate)]
            if all(checked.decision == "ALLOW" for checked in checks):
                report["structure"] = _structure(original, candidate)
                report["statements"] = {"original": original, "candidate": candidate}
            for iteration in range(repeat):
                names = (["original", "candidate"] if iteration % 2 == 0 else
                         ["candidate", "original"])
                sqls = {"original": original, "candidate": candidate}
                trial_started = time.monotonic()
                outcomes = await self.connector.compare_checked(
                    *(sqls[name] for name in names), self.analysis_limits, self.query_limits,
                    **({"before_select": self.before_select} if self.before_select else {}),
                )
                sides = {name: _statement_report(item)
                         for name, item in zip(names, outcomes, strict=True)}
                trial = {"execution_order": names, **sides, "snapshot": "unconfirmed",
                         "outcome": "inconclusive", "reason": "incomplete_evidence",
                         "duration_ms": round((time.monotonic() - trial_started) * 1000, 3)}
                report["trials"].append(trial)
                report.update(outcome="inconclusive", reason="incomplete_evidence")
                if report["structure"] is None:
                    trial["reason"] = report["reason"] = "policy_rejected"
                    break
                ordered = report["structure"]["row_mode"] == "ordered"
                try:
                    reason = compare_results(sides["original"], sides["candidate"], ordered=ordered)
                except ValueError:
                    break
                trial["snapshot"] = "same_readonly_innodb_snapshot"
                report["completed_trials"] += 1
                if report["structure"]["order_contract_changed"] and reason == "rows_match":
                    trial["reason"] = report["reason"] = "order_contract_changed"
                    break
                trial.update(reason=reason, outcome="observed_equal" if reason == "rows_match" else
                             "different")
                if baseline is not None and any(compare_results(
                    baseline[name], sides[name], ordered=ordered,
                ) != "rows_match" for name in ("original", "candidate")):
                    report.update(outcome="inconclusive", reason="results_changed_between_trials")
                    break
                baseline = sides
                report.update(outcome=trial["outcome"], reason=reason)
                if reason != "rows_match":
                    break
            if len(report["trials"]) < repeat or report["completed_trials"] < repeat:
                if report["outcome"] != "different":
                    report["outcome"] = "inconclusive"
            if report["outcome"] == "observed_equal":
                samples = {name: [trial[name]["select_duration_ms"] for trial in report["trials"]]
                           for name in ("original", "candidate")}
                report["performance"] = {
                    "select_duration_ms_samples": samples,
                    "median_select_duration_ms": {name: median(values)
                                                  for name, values in samples.items()},
                    "candidate_minus_original_median_ms": round(
                        median(samples["candidate"]) - median(samples["original"]), 3,
                    ),
                    "general_speedup_proven": False,
                }
        except asyncio.CancelledError:
            if self.record:
                self.record.emit("comparison_finished", status="error", code="CANCELLED",
                                 operation="compare_sql", report_id=report["comparison_id"])
            raise
        except Exception:
            report.update(outcome="inconclusive", reason="comparison_error", performance=None,
                          error={"code": "COMPARISON_ERROR",
                                 "message": "比较未取得完整可确认的证据。"})
        report["duration_ms"] = round((time.monotonic() - started) * 1000)
        budget = 2 * repeat * (
            self.query_limits.max_result_bytes + self.analysis_limits.max_plan_bytes + 8192
        ) + 2 * self.analysis_limits.max_sql_bytes + 8192
        if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode()) > budget:
            report.update(outcome="inconclusive", reason="response_limit", trials=[],
                          performance=None, completed_trials=0,
                          error={"code": "RESPONSE_LIMIT", "message": "比较响应超过输出预算。"})
        if self.record:
            self.record.emit("comparison_finished", status=report["outcome"],
                             operation="compare_sql",
                             report_id=report["comparison_id"], duration_ms=report["duration_ms"])
        return report
