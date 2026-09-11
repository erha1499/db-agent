"""Guarded SELECT application service; query rows are returned, never logged."""

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from db_agent.analysis import POLICY_VERSION
from db_agent.config import AnalysisSettings, QuerySettings
from db_agent.db import MetadataConnector
from db_agent.records import RunRecord


class QueryService:
    def __init__(
        self,
        connector: MetadataConnector,
        analysis_limits: AnalysisSettings,
        query_limits: QuerySettings,
        record: RunRecord | None = None,
        *, before_select=None,
    ):
        self.connector = connector
        self.analysis_limits = analysis_limits.model_copy(deep=True)
        self.query_limits = query_limits.model_copy(deep=True)
        self.record = record
        self.before_select = before_select

    async def execute(self, sql: str) -> dict:
        started = time.monotonic()
        response = {
            "query_id": uuid4().hex,
            "checked_at": datetime.now(UTC).isoformat(),
            "database": self.connector.database,
            "policy_version": POLICY_VERSION,
            "status": "error",
            "decision": "UNKNOWN",
            "execution_status": "unknown",
            "sql_fingerprint": "",
            "findings": [],
            "plan_summary": None,
            "server_version": None,
            "result": None,
            "error": None,
            "limitations": [
                "预检结论与查询结果分别判断；ALLOW 不代表查询成功。",
                "截断结果不能当作完整集合，返回行数不是原查询总行数。",
                "result_bytes 是结果 JSON 大小，不是数据库扫描或网络流量。",
                "金额和超大整数保留为字符串；DATETIME 无时区，TIMESTAMP 按会话 UTC 返回。",
            ],
        }
        try:
            # All authorization and plan checks live inside the connector entry point.
            evidence = await self.connector.execute_checked(
                sql, self.analysis_limits, self.query_limits,
                **({"before_select": self.before_select} if self.before_select else {}),
            )
            checked, assessment = evidence["check"], evidence["assessment"]
            result = evidence["result"]
            error = evidence["error"]
            status = "error" if error else "ok" if result is not None else "rejected"
            response.update(
                status=status,
                decision=evidence["decision"],
                execution_status=evidence["execution_status"],
                sql_fingerprint=checked.sql_fingerprint,
                findings=[*checked.findings, *(assessment.findings if assessment else ())],
                plan_summary=assessment.summary if assessment else None,
                server_version=evidence["server_version"],
                result=result,
                error=error,
            )
            if result is not None:
                response["result_id"] = uuid4().hex
                response["session_time_zone"] = "+00:00"
        except asyncio.CancelledError:
            if self.record:
                self.record.emit(
                    "query_finished", status="error", code="CANCELLED",
                    operation="execute_query", call_id=response["query_id"],
                    execution_status="unknown",
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            raise
        except Exception:
            # Never expose raw SQL, rows, driver errors or account information.
            response.update(
                status="error", result=None,
                error={"code": "QUERY_ERROR", "message": "查询未取得可确认的结果。"},
            )
            response.pop("result_id", None)
        response["duration_ms"] = round((time.monotonic() - started) * 1000)
        budget = self.query_limits.max_result_bytes + self.analysis_limits.max_plan_bytes + 8192
        if len(json.dumps(response, ensure_ascii=False).encode()) > budget:
            response.update(
                status="error", result=None, plan_summary=None, findings=[],
                error={"code": "RESPONSE_LIMIT", "message": "查询响应超过输出预算。"},
            )
            response.pop("result_id", None)
        if self.record:
            self.record.emit(
                "query_finished", status="error" if response["status"] == "error" else "ok",
                code=response["error"]["code"] if response["error"] else None,
                operation="execute_query", call_id=response["query_id"],
                result_id=response.get("result_id"), decision=response["decision"],
                execution_status=response["execution_status"], policy_version=POLICY_VERSION,
                sql_fingerprint=response["sql_fingerprint"],
                rule_ids=[finding["rule_id"] for finding in response["findings"]],
                row_count=response["result"]["row_count"] if response["result"] else None,
                truncated=response["result"]["truncated"] if response["result"] else None,
                duration_ms=response["duration_ms"],
            )
        return response
