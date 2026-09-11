"""Reader-only real acceptance; never creates, updates or removes database rows."""

import asyncio
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from db_agent.config import AnalysisSettings, QuerySettings, load_database_settings
from db_agent.db import MetadataConnector
from db_agent.optimization import OptimizationService

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_INTEGRATION") != "1",
    reason="requires DB_AGENT_MYSQL_INTEGRATION=1 and existing local synthetic data",
)
CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "evals/optimization_cases.json").read_text(),
)["cases"]


def service(**limits):
    settings = load_database_settings()
    assert (settings.host, settings.port, settings.database, settings.user) == (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    )
    analysis = AnalysisSettings(_env_file=None, **{
        name: field.default for name, field in AnalysisSettings.model_fields.items()
    })
    query = QuerySettings(_env_file=None, **{
        **{name: field.default for name, field in QuerySettings.model_fields.items()}, **limits,
    })
    return OptimizationService(MetadataConnector(settings), analysis, query)


def independent_rows_equal(actual, expected, ordered):
    # Independent literal oracle. No calls to production canonicalization/equality.
    if ordered:
        return actual == expected
    return Counter(json.dumps(row) for row in actual) == Counter(
        (json.dumps(row) for row in expected),
    )


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_real_rewrites_and_counterexamples_against_independent_fixture(case):
    report = asyncio.run(service().compare(case["original"], case["candidate"]))
    assert report["outcome"] == case["outcome"], report
    assert report["general_equivalence_proven"] is False
    for name in ("original", "candidate"):
        side = report["trials"][0][name]
        assert side["decision"] == "ALLOW" and side["execution_status"] == "completed", side
        assert side["result"]["truncated"] is False
        assert independent_rows_equal(side["result"]["rows"], case["expected_" + name],
                                      report["structure"]["row_mode"] == "ordered"), side
    assert report["trials"][0]["snapshot"] == "same_readonly_innodb_snapshot"


@pytest.mark.parametrize("side", ["original", "candidate"])
@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT id FROM private_table",
                                 "SELECT id FROM (SELECT id FROM orders) AS t"])
def test_real_neither_side_can_bypass_policy(side, sql):
    statements = dict(original="SELECT id FROM orders WHERE id = 1001",
                      candidate="SELECT id FROM orders WHERE id = 1001")
    statements[side] = sql
    report = asyncio.run(service().compare(**statements))
    assert report["outcome"] == "inconclusive"
    assert report["trials"][0][side]["decision"] in {"BLOCK", "UNKNOWN"}
    assert all(report["trials"][0][name]["execution_status"] == "not_started"
               for name in ("original", "candidate"))


def test_real_partial_prefix_cannot_match_or_continue_candidate():
    report = asyncio.run(service(max_rows=1).compare(
        "SELECT id FROM orders ORDER BY id", "SELECT id FROM orders ORDER BY id LIMIT 1",
    ))
    trial = report["trials"][0]
    assert report["outcome"] == "inconclusive"
    assert trial["original"]["execution_status"] == "truncated"
    assert trial["candidate"]["execution_status"] == "not_started"
    assert report["performance"] is None


def test_real_second_side_transaction_loss_cannot_claim_same_snapshot():
    compared = service()
    visits = []

    async def invalidate_readonly_transaction(connection):
        visits.append(True)
        if len(visits) == 2:
            # Reader-only rollback changes no rows and uses no admin permission.
            await compared.connector._fetch(connection, "ROLLBACK", (), [])
    compared.before_select = invalidate_readonly_transaction
    report = asyncio.run(compared.compare("SELECT id FROM orders WHERE id = 1001",
                                          "SELECT id FROM orders WHERE id = 1001"))
    assert visits == [True, True]
    assert report["outcome"] == "inconclusive"
    assert report["trials"][0]["candidate"]["error"]["code"] == "TRANSACTION_STATE"
    assert report["trials"][0]["candidate"]["execution_status"] == "not_started"


def test_real_second_plan_review_is_never_dispatched():
    compared = service()
    compared.analysis_limits = compared.analysis_limits.model_copy(update={"review_scan_rows": 1})
    report = asyncio.run(compared.compare("SELECT id FROM orders WHERE id = 1001",
                                         "SELECT id FROM orders WHERE id + 0 = 1001"))
    assert report["outcome"] == "inconclusive"
    assert report["trials"][0]["original"]["execution_status"] == "completed"
    assert report["trials"][0]["candidate"]["decision"] == "REVIEW"
    assert report["trials"][0]["candidate"]["execution_status"] == "not_started"
