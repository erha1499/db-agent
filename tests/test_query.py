"""Offline query service behavior with constructed connector outcomes, never real SQL."""

import asyncio
import json
import os

import pytest

from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings
from db_agent.db import MetadataConnector
from db_agent.plans import PlanAnalysis
from db_agent.query import QueryService
from db_agent.records import RunRecord

SQL = "SELECT total_amount FROM orders WHERE status = 'private-sql-marker'"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.upper().startswith("DB_AGENT_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def service():
    return QueryService(
        MetadataConnector(
            DatabaseSettings(
                _env_file=None,
                password="synthetic-reader",
                allowed_tables=("orders",),
            )
        ),
        AnalysisSettings(_env_file=None),
        QuerySettings(_env_file=None),
    )


def result(rows=None, truncated=False):
    return {
        "columns": [{"name": "total_amount", "type": "decimal"}],
        "rows": [["12345678901234567890.01"]] if rows is None else rows,
        "row_count": 1 if rows is None else len(rows),
        "truncated": truncated,
        "truncation_reason": "row_limit" if truncated else None,
        "result_bytes": 300,
        "server_statement_status": "unknown" if truncated else "completed",
    }


def outcome(service, *, decision="ALLOW", execution_status="not_started", rows=None, error=None):
    return {
        "check": service.connector.check_sql(SQL, service.analysis_limits),
        "assessment": PlanAnalysis(decision, (), {"tables": [], "operations": []}),
        "decision": decision,
        "execution_status": execution_status,
        "result": rows,
        "error": error,
        "server_version": "8.4.11",
    }


def stub_outcome(monkeypatch, service, evidence):
    calls = []

    async def execute_checked(sql, analysis_limits, query_limits):
        calls.append((sql, analysis_limits, query_limits))
        return evidence

    monkeypatch.setattr(service.connector, "execute_checked", execute_checked)
    return calls


@pytest.mark.parametrize(
    ("sql", "decision"),
    [
        ("DELETE FROM orders", "BLOCK"),
        ("SELECT id FROM orders UNION SELECT id FROM orders", "UNKNOWN"),
    ],
)
def test_direct_service_rejection_never_connects_or_claims_execution(
    service, monkeypatch, sql, decision
):
    from db_agent import db

    async def connect(**kwargs):
        pytest.fail("rejected query must not connect")

    monkeypatch.setattr(db.aiomysql, "connect", connect)
    response = asyncio.run(service.execute(sql))
    assert response["status"] == "rejected"
    assert response["decision"] == decision
    assert response["execution_status"] == "not_started"
    assert response["result"] is None
    assert response["error"] is None
    assert "result_id" not in response


def test_plan_review_is_a_business_rejection_with_its_evidence(service, monkeypatch):
    evidence = outcome(service, decision="REVIEW")
    evidence["assessment"] = PlanAnalysis(
        "REVIEW",
        (
            {
                "rule_id": "PLAN_LARGE_SCAN",
                "message": "估算超限",
                "evidence": {"estimated_rows": 200000},
            },
        ),
        {"tables": [{"table": "orders", "rows_examined_per_scan": 200000}]},
    )
    calls = stub_outcome(monkeypatch, service, evidence)
    response = asyncio.run(service.execute(SQL))
    assert response["status"] == "rejected"
    assert response["decision"] == "REVIEW"
    assert response["execution_status"] == "not_started"
    assert response["findings"][-1]["rule_id"] == "PLAN_LARGE_SCAN"
    assert response["plan_summary"]["tables"][0]["rows_examined_per_scan"] == 200000
    assert response["result"] is None
    assert len(calls) == 1 and calls[0][0] == SQL


@pytest.mark.parametrize(
    ("rows", "execution_status"),
    [
        (result(), "completed"),
        (result([]), "completed"),
        (result(truncated=True), "truncated"),
    ],
)
def test_allow_keeps_empty_completed_and_truncated_results_distinct(
    service,
    monkeypatch,
    rows,
    execution_status,
):
    stub_outcome(
        monkeypatch, service, outcome(service, rows=rows, execution_status=execution_status)
    )
    response = asyncio.run(service.execute(SQL))
    assert response["status"] == "ok"
    assert response["decision"] == "ALLOW"
    assert response["execution_status"] == execution_status
    assert response["result"] == rows
    assert response["result_id"] and response["query_id"]
    assert response["session_time_zone"] == "+00:00"
    assert response["error"] is None
    assert "private-sql-marker" not in json.dumps(response)


@pytest.mark.parametrize(
    ("decision", "execution_status"),
    [
        ("ALLOW", "unknown"),
        ("UNKNOWN", "not_started"),
        ("BLOCK", "not_started"),
    ],
)
def test_failed_execution_does_not_turn_precheck_allow_into_query_success(
    service,
    monkeypatch,
    decision,
    execution_status,
):
    error = {"code": "TIMEOUT", "message": "已停止等待，结果未确认。"}
    stub_outcome(
        monkeypatch,
        service,
        outcome(
            service,
            decision=decision,
            execution_status=execution_status,
            error=error,
        ),
    )
    response = asyncio.run(service.execute(SQL))
    assert response["status"] == "error"
    assert response["decision"] == decision
    assert response["execution_status"] == execution_status
    assert response["error"] == error
    assert response["result"] is None
    assert "result_id" not in response


def test_unexpected_connector_exception_is_sanitized_in_response_and_logs(service, monkeypatch):
    async def execute_checked(*args):
        raise RuntimeError("private-exception-marker private-sql-marker synthetic-reader")

    monkeypatch.setattr(service.connector, "execute_checked", execute_checked)
    record = service.record = RunRecord()
    response = asyncio.run(service.execute(SQL))
    assert response["status"] == "error"
    assert response["decision"] == "UNKNOWN"
    assert response["execution_status"] == "unknown"
    assert response["result"] is None
    assert response["error"]["code"] == "QUERY_ERROR"
    for value in ("private-exception-marker", "private-sql-marker", "synthetic-reader"):
        assert value not in json.dumps(response) + record.path.read_text()


def test_external_cancellation_propagates_and_records_no_query_or_rows(service, monkeypatch):
    async def execute_checked(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(service.connector, "execute_checked", execute_checked)
    record = service.record = RunRecord()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.execute(SQL))
    event = json.loads(record.path.read_text())
    assert event["event"] == "query_finished"
    assert event["status"] == "error"
    assert event["code"] == "CANCELLED"
    assert event["execution_status"] == "unknown"
    assert "private-sql-marker" not in record.path.read_text()


def test_query_log_links_result_without_storing_sql_columns_or_row_values(service, monkeypatch):
    rows = result([["private-row-marker"]], truncated=True)
    rows["columns"][0]["name"] = "private-column-marker"
    stub_outcome(monkeypatch, service, outcome(service, rows=rows, execution_status="truncated"))
    record = service.record = RunRecord()
    response = asyncio.run(service.execute(SQL))
    text = record.path.read_text()
    event = json.loads(text)
    assert event["event"] == "query_finished"
    assert event["decision"] == "ALLOW"
    assert event["execution_status"] == "truncated"
    assert event["result_id"] == response["result_id"]
    assert event["call_id"] == response["query_id"]
    assert event["row_count"] == 1 and event["truncated"] is True
    assert event["sql_fingerprint"] == response["sql_fingerprint"]
    for value in (SQL, "private-sql-marker", "private-row-marker", "private-column-marker"):
        assert value not in text


def test_log_io_failure_does_not_discard_confirmed_result(service, monkeypatch, tmp_path, capsys):
    stub_outcome(
        monkeypatch, service, outcome(service, rows=result(), execution_status="completed")
    )
    (tmp_path / "outputs").write_text("blocking-file")
    service.record = RunRecord()
    response = asyncio.run(service.execute(SQL))
    assert response["status"] == "ok" and response["result"] is not None
    assert capsys.readouterr().err.count("运行记录写入失败") == 1


def test_service_copies_limits_and_passes_the_current_sql_once(service, monkeypatch):
    analysis = AnalysisSettings(_env_file=None, review_scan_rows=123)
    query = QuerySettings(_env_file=None, max_rows=7)
    service = QueryService(service.connector, analysis, query)
    analysis.review_scan_rows = 999
    query.max_rows = 999
    calls = stub_outcome(monkeypatch, service, outcome(service))
    asyncio.run(service.execute(SQL))
    assert len(calls) == 1 and calls[0][0] == SQL
    assert calls[0][1].review_scan_rows == 123
    assert calls[0][2].max_rows == 7


def test_oversized_findings_fallback_is_itself_bounded(service, monkeypatch):
    service.query_limits.max_result_bytes = 1024
    service.analysis_limits.max_plan_bytes = 1024
    evidence = outcome(service, decision="UNKNOWN")
    # Constructed repeated JSON-path evidence, not arbitrary driver error text.
    evidence["assessment"] = PlanAnalysis(
        "UNKNOWN",
        tuple(
            {
                "rule_id": "PLAN_UNKNOWN",
                "message": "结构不支持",
                "evidence": {
                    "jsonpath": "$.query_block" + ".ordering_operation" * 32,
                },
            }
            for _ in range(64)
        ),
        {"tables": [], "operations": []},
    )
    stub_outcome(monkeypatch, service, evidence)
    response = asyncio.run(service.execute(SQL))
    assert response["status"] == "error"
    assert response["decision"] == "UNKNOWN"
    assert response["execution_status"] == "not_started"
    assert response["error"]["code"] == "RESPONSE_LIMIT"
    assert response["result"] is None and response["plan_summary"] is None
    assert len(json.dumps(response, ensure_ascii=False).encode()) <= 1024 + 1024 + 8192
