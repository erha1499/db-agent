"""Framework-independent SQL assessment. No model calls or business SQL execution."""

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from db_agent.config import AnalysisSettings
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.plans import analyze_plan
from db_agent.records import RunRecord

POLICY_VERSION = "mysql-select-v2"
LIMITATIONS = [
    "仅静态检查与普通 EXPLAIN 估算；未执行业务 SQL。",
    "未验证实际耗时、结果等价或建议效果；成本不是秒数。",
    "ALLOW 不构成执行授权；SQL、参数或目标变化后必须重新检查。",
]


class SqlAnalysisService:
    def __init__(
        self,
        connector: MetadataConnector,
        limits: AnalysisSettings,
        record: RunRecord | None = None,
    ):
        self.connector = connector
        self.limits = limits.model_copy(deep=True)
        self.record = record

    async def analyze(self, sql: str) -> dict:
        started = time.monotonic()
        check = self.connector.check_sql(sql, self.limits)
        report = {
            "report_id": uuid4().hex,
            "checked_at": datetime.now(UTC).isoformat(),
            "policy_version": getattr(self.connector, "policy_version", POLICY_VERSION),
            "database": self.connector.database,
            "decision": check.decision,
            "sql_fingerprint": check.sql_fingerprint,
            "tables": list(check.tables),
            "findings": list(check.findings),
            "evidence_source": "static_only",
            "server_version": None,
            "plan_summary": None,
            "limitations": LIMITATIONS.copy(),
        }
        if check.decision == "ALLOW":
            try:
                async with asyncio.timeout(self.limits.timeout_seconds):
                    # The connector rechecks the SQL at its own database entry point.
                    evidence = await self.connector.explain_checked(sql, self.limits)
                    check = evidence["check"]
                    report.update(
                        decision=check.decision,
                        sql_fingerprint=check.sql_fingerprint,
                        tables=list(check.tables),
                        findings=list(check.findings),
                    )
                    if check.decision == "ALLOW":
                        assessment = evidence.get("assessment") or analyze_plan(
                            evidence["plan"], self.limits, check.aliases,
                        )
                        report.update(
                            decision=assessment.decision,
                            findings=[*check.findings, *assessment.findings],
                            evidence_source=getattr(
                                self.connector, "evidence_source", "mysql_explain_json",
                            ),
                            server_version=evidence["server_version"],
                            plan_summary=assessment.summary,
                        )
            except DatabaseError as exc:
                report["decision"] = "BLOCK" if exc.code == "PERMISSION_DENIED" else "UNKNOWN"
                report["findings"].append({"rule_id": exc.code, "message": exc.message})
            except TimeoutError:
                report["decision"] = "UNKNOWN"
                report["findings"].append(
                    {
                        "rule_id": "ANALYSIS_TIMEOUT",
                        "message": "诊断超过时间预算，已停止等待并关闭连接；未确认服务器取消状态。",
                    }
                )
            except Exception:
                # Driver/parser failures may carry literals or account information.
                report["decision"] = "UNKNOWN"
                report["findings"].append(
                    {
                        "rule_id": "ANALYSIS_ERROR",
                        "message": "诊断证据获取或解析失败。",
                    }
                )
        report["duration_ms"] = round((time.monotonic() - started) * 1000)
        # The metadata envelope has a separate, finite 8 KiB allowance.
        if len(json.dumps(report, ensure_ascii=False).encode()) > self.limits.max_plan_bytes + 8192:
            report.update(
                decision="UNKNOWN",
                plan_summary=None,
                findings=[{"rule_id": "REPORT_LIMIT", "message": "诊断报告超过输出预算。"}],
            )
        if self.record:
            self.record.emit(
                "analysis_finished",
                status="ok",
                operation="analyze_sql",
                report_id=report["report_id"],
                decision=report["decision"],
                policy_version=report["policy_version"],
                sql_fingerprint=report["sql_fingerprint"],
                rule_ids=[finding["rule_id"] for finding in report["findings"]],
                duration_ms=report["duration_ms"],
            )
        return report
