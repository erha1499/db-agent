"""Exact result semantics, incomplete evidence and bounded product CLI contracts."""

import asyncio
import copy
import io
import json
from decimal import localcontext

import pytest
from test_db import driver as driver
from test_db import settings as settings
from test_optimization_connector import CANDIDATE, SQL, pair_connection
from test_query_connector import limits as limits

from db_agent import cli
from db_agent.db import MetadataConnector
from db_agent.optimization import OptimizationService, compare_results
from db_agent.results import _result


def report(rows, columns=None):
    return {"status": "ok", "decision": "ALLOW", "execution_status": "completed", "error": None,
            "result": _result(columns or [{"name": "x", "type": "bigint"}], rows)}


@pytest.mark.parametrize(("left", "right", "ordered", "expected"), [
    ([[1], [2], [1]], [[2], [1], [1]], False, "rows_match"),
    ([[1], [2], [1]], [[2], [1], [1]], True, "rows_differ"),
    ([[1], [1], [2]], [[1], [2], [2]], False, "rows_differ"),
    ([[None]], [[0]], False, "rows_differ"),
    ([], [], True, "rows_match"),
    ([["9007199254740992"]], [["9007199254740993"]], False, "rows_differ"),
])
def test_exact_rows_and_multiplicity(left, right, ordered, expected):
    assert compare_results(report(left), report(right), ordered=ordered) == expected


def test_decimal_comparison_is_exact_even_under_low_arithmetic_precision():
    columns = [{"name": "amount", "type": "decimal"}]
    with localcontext() as context:
        context.prec = 2
        a = report([["9007199254740993.0100"]], columns)
        assert compare_results(a, report([["9007199254740993.01"]], columns), ordered=False) == (
            "rows_match"
        )
        assert compare_results(a, report([["9007199254740993.02"]], columns), ordered=False) == (
            "rows_differ"
        )


def test_duplicate_column_labels_preserve_position_and_type_contract():
    columns = [{"name": "id", "type": "bigint"}] * 2
    assert compare_results(report([[1, 2]], columns), report([[2, 1]], columns), ordered=False) == (
        "rows_differ"
    )
    assert compare_results(report([[1]]), report([[1]], [{"name": "x", "type": "int"}]),
                           ordered=False) == "column_contract_differs"


@pytest.mark.parametrize(("rows", "other", "kind"), [
    ([[None]], [["NULL"]], "varchar"),
    ([["A"]], [["a"]], "varchar"),
    ([["a"]], [["a "]], "char"),
    ([["2026-02-01T00:00:00"]], [["2026-01-31T23:59:59"]], "datetime"),
    ([[0.3]], [[0.30000000000000004]], "double"),
])
def test_no_collation_timestamp_or_float_guessing(rows, other, kind):
    columns = [{"name": "v", "type": kind}]
    assert compare_results(report(rows, columns), report(other, columns), ordered=False) == (
        "rows_differ"
    )


@pytest.mark.parametrize("failure", ["truncated", "unknown", "allow_error", "count", "width",
                                     "missing_result", "eof", "null_truncation", "nan"])
def test_incomplete_result_is_never_compared_equal(failure):
    original = report([[1]])
    candidate = copy.deepcopy(original)
    if failure == "truncated":
        candidate["execution_status"] = "truncated"
        candidate["result"] = _result(candidate["result"]["columns"], [[1]], "row_limit")
    elif failure == "unknown":
        candidate["decision"] = "UNKNOWN"
    elif failure == "allow_error":
        candidate["error"] = {"code": "TIMEOUT"}
    elif failure == "count":
        candidate["result"]["row_count"] = 2
    elif failure == "width":
        candidate["result"]["rows"] = [[1, 2]]
    elif failure == "missing_result":
        candidate["result"] = None
    elif failure == "eof":
        candidate["result"]["server_statement_status"] = "unknown"
    elif failure == "null_truncation":
        candidate["result"]["truncated"] = None
    else:
        candidate["result"]["rows"] = [[float("nan")]]
    with pytest.raises(ValueError):
        compare_results(original, candidate, ordered=False)


def test_service_alternates_trials_and_keeps_snapshot_equality_separate(settings, limits, driver):
    driver[0].extend([pair_connection() for _ in range(3)])
    result = asyncio.run(OptimizationService(MetadataConnector(settings), *limits).compare(
        SQL, CANDIDATE, repeat=3,
    ))
    assert result["outcome"] == "observed_equal"
    assert result["completed_trials"] == result["requested_trials"] == 3
    assert result["general_equivalence_proven"] is False
    assert result["independent_oracle_checked"] is False
    assert [t["execution_order"] for t in result["trials"]] == [
        ["original", "candidate"], ["candidate", "original"], ["original", "candidate"],
    ]
    assert result["performance"]["general_speedup_proven"] is False
    assert result["structure"]["ast_identical"] is False
    assert all(t["snapshot"] == "same_readonly_innodb_snapshot" for t in result["trials"])


def test_changed_results_between_trials_are_inconclusive(settings, limits, driver):
    first, second = pair_connection(), pair_connection()
    second.responses[6] = [(9,)]
    second.responses[13] = [(9,)]
    driver[0].extend([first, second])
    result = asyncio.run(OptimizationService(MetadataConnector(settings), *limits).compare(
        SQL, CANDIDATE, repeat=3,
    ))
    assert result["outcome"] == "inconclusive"
    assert result["reason"] == "results_changed_between_trials"
    assert result["performance"] is None
    assert len(result["trials"]) == 2


def test_later_missing_evidence_does_not_inherit_earlier_success(settings, limits, driver):
    first, second = pair_connection(), pair_connection()
    second.responses[0] = []
    driver[0].extend([first, second])
    result = asyncio.run(OptimizationService(MetadataConnector(settings), *limits).compare(
        SQL, CANDIDATE, repeat=2,
    ))
    assert result["outcome"] == "inconclusive" and result["reason"] == "incomplete_evidence"
    assert result["performance"] is None and result["completed_trials"] == 1


def test_order_contract_change_is_not_ignored_even_when_sequences_match(settings, limits, driver):
    driver[0].append(pair_connection())
    result = asyncio.run(OptimizationService(MetadataConnector(settings), *limits).compare(
        SQL + " ORDER BY id", CANDIDATE,
    ))
    assert result["outcome"] == "inconclusive" and result["reason"] == "order_contract_changed"
    assert result["structure"]["row_mode"] == "ordered"


@pytest.mark.parametrize("repeat", [0, 4, True, "1"])
def test_repeat_is_strict_and_bounded(settings, limits, driver, repeat):
    with pytest.raises(ValueError):
        asyncio.run(OptimizationService(MetadataConnector(settings), *limits).compare(
            SQL, CANDIDATE, repeat=repeat,
        ))
    assert not driver[1]


def test_cli_compare_stdin_and_exit_codes(settings, limits, driver, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_database_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_analysis_settings", lambda: limits[0])
    monkeypatch.setattr(cli, "load_query_settings", lambda: limits[1])
    for rows, expected_code, expected_outcome in ([(1,), (2,)], 0, "observed_equal"), (
        [(999,)], 4, "different",
    ):
        connection = pair_connection()
        connection.responses[13] = rows
        driver[0].append(connection)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({
            "original": SQL, "candidate": CANDIDATE,
        }).encode())))
        assert cli.main(["db", "compare", "--stdin"]) == expected_code
        result = json.loads(capsys.readouterr().out)
        assert result["outcome"] == expected_outcome
    logs = "".join(p.read_text() for p in (tmp_path / "outputs/runs").glob("*.jsonl"))
    assert "paid" not in logs and CANDIDATE not in logs


@pytest.mark.parametrize("raw", [b"no json", b'{"original":true,"candidate":"x"}',
                                 b'{"original":"x","candidate":"x","approved":true}',
                                 b"x" * 50000, b"\xff"])
def test_cli_rejects_malformed_and_privileged_input(raw, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert cli.main(["db", "compare", "--stdin"]) == 2
    assert "approved" not in capsys.readouterr().err


def test_acceptance_budgets_cannot_be_relaxed_by_shell(monkeypatch):
    import runpy
    from pathlib import Path

    monkeypatch.setenv("DB_AGENT_ANALYSIS_REVIEW_SCAN_ROWS", "1000000000")
    monkeypatch.setenv("DB_AGENT_QUERY_OPERATION_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("DB_AGENT_QUERY_MAX_ROWS", "1000")
    evaluation = runpy.run_path(str(
        Path(__file__).resolve().parents[1] / "scripts/evaluate_optimization.py",
    ))
    analysis, query = evaluation["fixed_limits"]()
    assert analysis.review_scan_rows == 100000
    assert query.operation_timeout_seconds == 15 and query.max_rows == 100
