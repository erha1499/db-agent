"""Offline service behavior; all plan values below are constructed fixtures."""

import asyncio
import json

import pytest

from db_agent.analysis import SqlAnalysisService
from db_agent.config import AnalysisSettings, DatabaseSettings
from db_agent.db import DatabaseError, MetadataConnector


@pytest.fixture
def connector():
    return MetadataConnector(
        DatabaseSettings(
            _env_file=None,
            password="synthetic-reader",
            allowed_tables=("orders",),
        )
    )


def plan(rows=6):
    return {
        "query_block": {
            "select_id": 1,
            "table": {
                "table_name": "orders",
                "access_type": "ALL",
                "rows_examined_per_scan": rows,
                "rows_produced_per_join": rows,
                "filtered": "100.00",
                "attached_condition": "private-literal-never-returned",
            },
        }
    }


@pytest.mark.parametrize(("rows", "decision"), [(6, "ALLOW"), (200000, "REVIEW")])
def test_assessment_uses_plan_evidence_not_limit(connector, monkeypatch, rows, decision):
    limits = AnalysisSettings(_env_file=None)
    queries = []

    async def explain(sql, settings):
        queries.append(sql)
        return {
            "check": connector.check_sql(sql, settings),
            "plan": plan(rows),
            "server_version": "8.4.11",
        }

    monkeypatch.setattr(connector, "explain_checked", explain)
    sql = "SELECT id FROM orders LIMIT 1"
    report = asyncio.run(SqlAnalysisService(connector, limits).analyze(sql))
    assert report["decision"] == decision
    assert report["evidence_source"] == "mysql_explain_json"
    assert queries == [sql]
    assert "private-literal-never-returned" not in json.dumps(report)
    assert report["sql_fingerprint"]
    assert report["report_id"]
    assert report["database"] == "db_agent"


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT * FROM private_table"])
def test_service_blocks_without_calling_plan_provider(connector, monkeypatch, sql):
    async def explain(*args):
        pytest.fail("rejected SQL reached EXPLAIN")

    monkeypatch.setattr(connector, "explain_checked", explain)
    report = asyncio.run(
        SqlAnalysisService(connector, AnalysisSettings(_env_file=None)).analyze(sql)
    )
    assert report["decision"] == "BLOCK"
    assert report["plan_summary"] is None


@pytest.mark.parametrize(
    ("error", "decision", "rule_id"),
    [
        (DatabaseError("PERMISSION_DENIED", "权限不足。"), "BLOCK", "PERMISSION_DENIED"),
        (DatabaseError("SYNTAX_ERROR", "语法无效。"), "UNKNOWN", "SYNTAX_ERROR"),
        (RuntimeError("private-exception-marker"), "UNKNOWN", "ANALYSIS_ERROR"),
    ],
)
def test_failed_evidence_never_allows_or_echoes_untrusted_error(
    connector, monkeypatch, error, decision, rule_id
):
    async def explain(*args):
        raise error

    monkeypatch.setattr(connector, "explain_checked", explain)
    report = asyncio.run(
        SqlAnalysisService(connector, AnalysisSettings(_env_file=None)).analyze(
            "SELECT * FROM orders"
        )
    )
    assert report["decision"] == decision
    assert rule_id in {finding["rule_id"] for finding in report["findings"]}
    assert "private-exception-marker" not in json.dumps(report)


def test_analysis_timeout_cancels_provider_and_reports_unknown(connector, monkeypatch):
    cancelled = []

    async def explain(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(connector, "explain_checked", explain)
    limits = AnalysisSettings(_env_file=None, timeout_seconds=0.01)
    report = asyncio.run(SqlAnalysisService(connector, limits).analyze("SELECT * FROM orders"))
    assert report["decision"] == "UNKNOWN"
    assert report["findings"][-1]["rule_id"] == "ANALYSIS_TIMEOUT"
    assert cancelled == [True]


def test_outer_cancellation_propagates(connector, monkeypatch):
    async def explain(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(connector, "explain_checked", explain)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            SqlAnalysisService(connector, AnalysisSettings(_env_file=None)).analyze(
                "SELECT * FROM orders"
            )
        )


def test_connector_recheck_cannot_be_overridden_by_service(connector, monkeypatch):
    limits = AnalysisSettings(_env_file=None)

    async def explain(sql, settings):
        denied = MetadataConnector(DatabaseSettings(_env_file=None, password="synthetic-reader"))
        return {"check": denied.check_sql(sql, settings), "plan": None, "server_version": None}

    monkeypatch.setattr(connector, "explain_checked", explain)
    report = asyncio.run(SqlAnalysisService(connector, limits).analyze("SELECT * FROM orders"))
    assert report["decision"] == "BLOCK"
    assert report["evidence_source"] == "static_only"
