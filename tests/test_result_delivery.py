"""Deterministic report tests use synthetic values, independent of the model/DB."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from db_agent.result_delivery import (
    AnalysisInput,
    ExportInput,
    analyze_result,
    html_report,
    validate_result,
)


def data(rows=None, *, types=("varchar", "decimal")):
    return {
        "columns": [{"name": "value", "type": t} for t in types],
        "rows": rows or [], "row_count": len(rows or []), "truncated": False,
        "truncation_reason": None, "server_statement_status": "completed",
    }


def report(result):
    return {"status": "ok", "decision": "ALLOW", "execution_status": "completed",
            "result": result, "error": None}


def analyze(result, kind="comparison", dimension=0, measure=1):
    return analyze_result(validate_result(report(result)), AnalysisInput(
        dimension=dimension, measure=measure, kind=kind,
    ))


def test_exact_sums_large_integer_decimal_cancellation_and_duplicate_names():
    result = data([
        ["decimal", "0.1"], ["decimal", "0.2"],
        ["large", "9007199254740993"], ["large", "1"],
        ["wide", "1234567890123456789012345678901234567890.12"], ["wide", "0.01"],
        ["cancel", "-99999999999999999999999999999.99"],
        ["cancel", "100000000000000000000000000000.00"],
    ])
    before = deepcopy(result)
    analysis = analyze(result)
    assert [p["sum"] for p in analysis["points"]] == [
        "0.3", "9007199254740994", "1234567890123456789012345678901234567890.13", "0.01",
    ]
    assert analysis["dimension_label"] == "1. value"
    assert analysis["measure_label"] == "2. value"
    assert result == before
    assert all(0 <= p["position"] <= 1 for p in analysis["points"])


def test_negative_zero_null_groups_empty_and_type_distinctions():
    analysis = analyze(data([[None, None], ["NULL", "-3.00"], ["", "0.00"], [None, None]]))
    assert [(p["label"], p["sum"], p["row_count"], p["non_null_count"])
            for p in analysis["points"]] == [
        ("NULL", None, 2, 0), ('"NULL"', "-3.00", 1, 1), ('""', "0.00", 1, 1),
    ]
    assert analysis["zero_position"] == 1
    assert analysis["minimum"] == "-3.00"
    assert analyze(data())["minimum"] is None
    assert analyze(data([["all null", None]]))["points"][0]["position"] is None


def test_trend_sorts_without_filling_gaps_and_preserves_null_gap():
    analysis = analyze(data([
        ["2026-03-03", "20.50"], ["2026-01-01", "10.10"],
        ["2026-02-01", None], ["2026-03-03", "0.01"],
    ], types=("date", "decimal")), "trend")
    assert [p["dimension"] for p in analysis["points"]] == [
        "2026-01-01", "2026-02-01", "2026-03-03",
    ]
    assert analysis["points"][1]["sum"] is None
    assert analysis["first_to_last_difference"] == "10.41"
    assert "等距" in analysis["notes"][2]


@pytest.mark.parametrize("value", [None, "20260101", "2026-13-01", "2026-01-01T00:00:00"])
def test_invalid_date_is_not_dropped_or_reinterpreted(value):
    with pytest.raises(ValueError, match="趋势时间"):
        analyze(data([[value, "1"]], types=("date", "decimal")), "trend")


@pytest.mark.parametrize("types,kind", [(("varchar", "varchar"), "comparison"),
                                       (("varchar", "decimal"), "trend")])
def test_column_types_not_guessed_from_values(types, kind):
    with pytest.raises(ValueError):
        analyze(data([["2026-01-01", "123"]], types=types), kind)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e99999", "1" * 101, True, "words"])
def test_unsupported_numeric_value_rejected(value):
    with pytest.raises(ValueError):
        analyze(data([["a", value]]))


def test_group_budget_rejects_whole_analysis_without_silent_truncation():
    assert len(analyze(data([[str(i), 1] for i in range(100)]))["points"]) == 100
    with pytest.raises(ValueError, match="100"):
        analyze(data([[str(i), 1] for i in range(101)]))


@pytest.mark.parametrize("kwargs", [
    {"dimension": True, "measure": 1}, {"dimension": -1, "measure": 1},
    {"dimension": 0, "measure": 256}, {"dimension": 0, "measure": 1, "approved": True},
])
def test_analysis_selection_schema(kwargs):
    with pytest.raises(ValidationError):
        AnalysisInput(**kwargs)


def test_invalid_or_same_column_positions_and_export_inputs():
    for dimension, measure in [(0, 0), (0, 2), (2, 1)]:
        with pytest.raises(ValueError, match="不同且存在"):
            analyze(data(), dimension=dimension, measure=measure)
    for kwargs in [{"format": "csv"}, {"format": "json", "path": "/tmp/result"}]:
        with pytest.raises(ValidationError):
            ExportInput(**kwargs)


@pytest.mark.parametrize("change", [
    {"status": "error"}, {"decision": "BLOCK"}, {"error": {"code": "error"}},
    {"execution_status": "truncated"},
])
def test_inconsistent_report_rejected(change):
    candidate = report(data([["a", "1"]]))
    candidate.update(change)
    with pytest.raises(ValueError):
        validate_result(candidate)


@pytest.mark.parametrize("change", [
    {"row_count": True}, {"row_count": 3}, {"rows": [[1]]}, {"truncated": 1},
    {"server_statement_status": "unknown"}, {"columns": [{"name": "missing type"}]},
    {"rows": [["a", 9007199254740993]]}, {"rows": [["a", float("inf")]]},
])
def test_incomplete_snapshot_rejected(change):
    candidate = data([["a", "1"]])
    candidate.update(change)
    with pytest.raises(ValueError):
        validate_result(report(candidate))


def test_truncated_result_is_analyzable_but_carries_scope_and_does_not_mutate():
    result = data([["a", "10.00"]])
    result.update(truncated=True, truncation_reason="row_limit", server_statement_status="unknown")
    candidate = report(result)
    candidate["execution_status"] = "truncated"
    assert validate_result(candidate) is result
    value = analyze_result(result, AnalysisInput(dimension=0, measure=1))
    assert value["points"][0]["sum"] == "10.00"
    assert "WHERE / LIMIT" in value["notes"][0]


def test_html_report_escapes_every_data_surface_and_json_retains_raw_values():
    attack = '</script><img src=x onerror="alert(1)"><svg onload="alert(2)"> &'
    result = data([[attack, "9007199254740993.01"], ["NULL", None]])
    result["columns"][0]["name"] = attack
    snapshot = {
        "report": report(result), "analysis": analyze(result), "prompt": attack,
        "sql": attack, "result_id": "a" * 32, "finished_at": "2026-09-11",
        "conversation_id": "b" * 32, "run_id": "c" * 32, "notes": [attack],
    }
    html = html_report(snapshot)
    assert '<script' not in html and '<img' not in html and '<svg onload' not in html
    assert '&lt;img' in html and '&amp;' in html
    assert '9007199254740993.01' in html
    assert "default-src 'none'" in html
    assert 'href=' not in html and 'src=' not in html.replace('src=x', '')
    assert json.loads(json.dumps(snapshot))["report"]["result"]["rows"] == result["rows"]


def test_visible_control_text_does_not_change_raw_snapshot():
    raw = 'label\u202espoof\u202c'
    result = data([[raw, '1.00']])
    output = analyze(result)
    assert output['points'][0]['label'] == '"label\\u202espoof\\u202c"'
    assert result['rows'][0][0] == raw
