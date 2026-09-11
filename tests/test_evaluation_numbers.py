"""Exact numeric assessment of synthetic reports; no model or database calls."""

from copy import deepcopy

import pytest

from db_agent.evaluation import assess_report


def assessment(expected_rows, actual_rows, *, column_types=("decimal",), ordered=True):
    case = {"expected": {
        "status": "ok", "decision": "ALLOW", "execution_status": "completed",
        "rows": deepcopy(expected_rows), "ordered": ordered, "truncated": False,
    }}
    report = {
        "status": "ok", "decision": "ALLOW", "execution_status": "completed",
        "error": None,
        "result": {
            "columns": [{"name": f"column_{n}", "type": kind}
                        for n, kind in enumerate(column_types)],
            "rows": deepcopy(actual_rows), "row_count": len(actual_rows),
            "truncated": False, "truncation_reason": None,
            "server_statement_status": "completed",
        },
    }
    return case, report


@pytest.mark.parametrize("actual", ["1", "1.0", "1.000000000000000000000000000000"])
def test_decimal_integer_representation_is_exactly_equivalent(actual):
    case, report = assessment([[1]], [[actual]])
    before = deepcopy((case, report))
    assert assess_report(case, report) == []
    assert (case, report) == before


@pytest.mark.parametrize("expected,actual", [
    (0, "-0.00"), (-12, "-12.000"), (9007199254740993, "9007199254740993.00"),
    (10**64 + 1, str(10**64 + 1)),
])
def test_exact_integer_equivalence_does_not_use_float_or_decimal_context(expected, actual):
    case, report = assessment([[expected]], [[actual]])
    assert assess_report(case, report) == []


@pytest.mark.parametrize("expected,actual", [
    (1, "1.1"), (1, "1.000000000000000000000000000001"),
    (9007199254740993, "9007199254740992"), (9007199254740992, "9007199254740993"),
    (1, True), (0, False), (1, 1.0), (1, "NaN"), (1, "Infinity"),
    (1, "1e1000000000"), (1, "1e-1000000000"), (1, "1E0"),
    (1, " 1"), (1, "1\n"), (1, "+1"), (1, "01"), (1, "１"),
    (1, "1."), (1, ".1"), (0, "0" * 100000),
])
def test_unequal_wrong_type_or_non_mysql_decimal_form_is_rejected(expected, actual):
    case, report = assessment([[expected]], [[actual]])
    assert assess_report(case, report) == ["data_error"]


@pytest.mark.parametrize("kind", ["varchar", "char", "bigint", "double", "float"])
def test_numeric_text_needs_decimal_column_metadata(kind):
    case, report = assessment([[1]], [["1"]], column_types=(kind,))
    assert assess_report(case, report) == ["data_error"]


@pytest.mark.parametrize("columns", [None, [], [{}], [{"type": "decimal"}],
                                     [{"name": "n"}], ["decimal"],
                                     [{"name": "n", "type": None}]])
def test_numeric_equivalence_requires_complete_column_metadata(columns):
    case, report = assessment([[1]], [["1"]])
    report["result"]["columns"] = columns
    assert assess_report(case, report) == ["data_error"]


def test_missing_column_metadata_does_not_pass_even_for_identical_integer_rows():
    case, report = assessment([[1]], [[1]])
    del report["result"]["columns"]
    assert assess_report(case, report) == ["data_error"]


def test_null_remains_null_and_mixed_integer_null_column_can_be_compared():
    case, report = assessment([[None], [0], [1]], [[None], ["0.00"], ["1.0"]])
    assert assess_report(case, report) == []
    report["result"]["rows"][0][0] = "0"
    assert assess_report(case, report) == ["data_error"]


@pytest.mark.parametrize("expected,actual", [
    ([[None]], [["0"]]), ([[True]], [["1"]]),
    ([[1], ["2"]], [["1"], ["2"]]), ([[1], [True]], [["1"], [True]]),
])
def test_only_columns_with_at_least_one_integer_and_no_other_non_null_type_coerce(
    expected, actual,
):
    case, report = assessment(expected, actual)
    assert assess_report(case, report) == ["data_error"]


def test_decimal_amount_strings_preserve_the_original_precision_contract():
    case, report = assessment([[1, "100.00"]], [["1.0", "100.00"]],
                              column_types=("decimal", "decimal"))
    assert assess_report(case, report) == []
    report["result"]["rows"][0][1] = "100.0"
    assert assess_report(case, report) == ["data_error"]


def test_date_strings_remain_exact():
    case, report = assessment([["2026-02-01"]], [["2026-02-01"]], column_types=("date",))
    assert assess_report(case, report) == []
    report["result"]["rows"][0][0] = "2026-02-01T00:00:00"
    assert assess_report(case, report) == ["data_error"]


@pytest.mark.parametrize("actual", [[], [["1"], ["1"]], [["1", "2"]], [[]], ["1"]])
def test_row_count_and_row_width_must_match(actual):
    case, report = assessment([[1]], actual)
    assert assess_report(case, report) == ["data_error"]


def test_columns_width_and_reported_row_count_must_match():
    case, report = assessment([[1]], [["1"]], column_types=("decimal", "decimal"))
    assert assess_report(case, report) == ["data_error"]
    report["result"]["columns"].pop()
    report["result"]["row_count"] = 2
    assert assess_report(case, report) == ["data_error"]


def test_unordered_comparison_preserves_duplicates_and_column_positions():
    case, report = assessment([[1, 2], [1, 2], [3, 4]],
                              [["3", "4"], ["1.0", "2"], ["1", "2.0"]],
                              column_types=("decimal", "decimal"), ordered=False)
    assert assess_report(case, report) == []
    report["result"]["rows"][1] = ["3", "4"]
    assert assess_report(case, report) == ["data_error"]
    report["result"]["rows"] = [["2", "1"], ["2", "1"], ["4", "3"]]
    assert assess_report(case, report) == ["data_error"]


def test_ordered_comparison_does_not_discard_order():
    case, report = assessment([[1], [2]], [["2"], ["1"]])
    assert assess_report(case, report) == ["data_error"]


@pytest.mark.parametrize("field,value", [
    ("truncated", True), ("server_statement_status", "unknown"),
    ("truncation_reason", "row_limit"),
])
def test_numeric_match_cannot_hide_incomplete_execution(field, value):
    case, report = assessment([[1]], [["1"]])
    report["result"][field] = value
    assert assess_report(case, report) == ["data_error"]


def test_static_rejection_remains_a_failed_business_result():
    case, report = assessment([[1]], [["1"]])
    report.update(status="rejected", decision="BLOCK", execution_status="not_started",
                  result=None, findings=[{"rule_id": "FUNCTION_NOT_ALLOWED"}])
    assert assess_report(case, report) == ["data_error"]
