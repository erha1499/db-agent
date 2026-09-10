"""Constructed plan fixtures for policy boundaries; these are not real MySQL plans."""

import copy
import json

import pytest

from db_agent.config import AnalysisSettings
from db_agent.plans import analyze_plan


@pytest.fixture
def limits():
    # Explicit values keep these pure rule tests independent of .env and the shell.
    return AnalysisSettings(
        _env_file=None,
        max_plan_bytes=65536,
        max_plan_nodes=256,
        review_scan_rows=100000,
        review_join_rows=1000000,
        review_sort_rows=100000,
    )


def table(name="o", scan=10, produced=10, access="ALL"):
    """Build a constructed fixture, never claim it is captured from a database."""
    return {
        "table_name": name,
        "access_type": access,
        "rows_examined_per_scan": scan,
        "rows_produced_per_join": produced,
        "filtered": "100.00",
        "cost_info": {"read_cost": "1.00", "eval_cost": "0.10", "prefix_cost": "1.10"},
    }


def plan(node=None):
    return {
        "query_block": {
            "select_id": 1,
            "cost_info": {"query_cost": "1.10"},
            "table": table() if node is None else node,
        }
    }


def test_small_full_scan_is_allowed_without_a_limit_requirement(limits):
    result = analyze_plan(plan(), limits, {"o": "orders"})

    assert result.decision == "ALLOW"
    assert result.summary["tables"][0] == {
        "table": "orders",
        "alias": "o",
        "jsonpath": "$.query_block.table",
        "access_type": "ALL",
        "rows_examined_per_scan": 10,
        "rows_produced_per_join": 10,
        "filtered": 100,
        "cost_info": {"read_cost": 1, "eval_cost": 0.1, "prefix_cost": 1.1},
    }


@pytest.mark.parametrize("access", ["ALL", "index", "range", "ref", "eq_ref", "const", "system"])
def test_large_scan_requires_review_for_every_supported_access_type(limits, access):
    # Small produced rows can represent a selective predicate or a small final result.
    result = analyze_plan(
        plan(table(scan=100001, produced=1, access=access)), limits, {"o": "orders"}
    )

    assert result.decision == "REVIEW"
    assert "PLAN_LARGE_SCAN" in {finding["rule_id"] for finding in result.findings}


def test_scan_and_join_thresholds_are_strict_and_accept_zero(limits):
    for scan, produced in [(0, 0), (100000, 1000000)]:
        result = analyze_plan(plan(table(scan=scan, produced=produced)), limits, {"o": "orders"})
        assert result.decision == "ALLOW"

    result = analyze_plan(plan(table(produced=1000001)), limits, {"o": "orders"})
    assert result.decision == "REVIEW"
    assert result.findings[0]["rule_id"] == "PLAN_LARGE_JOIN"


@pytest.mark.parametrize(("flags", "covering"), [
    ({"using_index": True}, True),
    ({"using_index": False}, False),
    ({"using_index_condition": True}, False),
    ({}, False),
])
def test_covering_evidence_is_distinct_from_pushdown_and_never_exempts_large_scan(
    limits, flags, covering
):
    node = {**table(access="index", scan=200000), **flags}
    result = analyze_plan(plan(node), limits, {"o": "orders"})
    rules = {finding["rule_id"] for finding in result.findings}
    assert ("PLAN_COVERING_INDEX" in rules) is covering
    assert "PLAN_LARGE_SCAN" in rules
    assert result.decision == "REVIEW"


@pytest.mark.parametrize("flag", ["backward_index_scan", "not_exists"])
@pytest.mark.parametrize("value", [True, False])
def test_index_and_join_boolean_observations_are_preserved_in_summary(limits, flag, value):
    # Constructed fixture. MySQL 8.4's EXPLAIN manual describes these observations;
    # mysql-server tag mysql-8.4.11/sql/opt_explain_json.cc defines their JSON keys.
    node = {**table(access="ref", scan=2, produced=2), flag: value}
    result = analyze_plan(plan(node), limits, {"o": "orders"})

    assert result.decision == "ALLOW"
    assert result.summary["tables"][0][flag] is value
    assert "PLAN_COVERING_INDEX" not in {finding["rule_id"] for finding in result.findings}


@pytest.mark.parametrize("flag", ["backward_index_scan", "not_exists"])
@pytest.mark.parametrize("value", [0, 1, "true", None, [], {}])
def test_index_and_join_observations_reject_nonboolean_evidence(limits, flag, value):
    result = analyze_plan(
        plan({**table(), flag: value}), limits, {"o": "orders"},
    )

    assert result.decision == "UNKNOWN"
    assert flag not in result.summary["tables"][0]


@pytest.mark.parametrize("flag", ["backward_index_scan", "not_exists"])
def test_index_and_join_observations_do_not_exempt_a_large_scan(limits, flag):
    node = {**table(access="index", scan=100001, produced=1), flag: True}
    result = analyze_plan(plan(node), limits, {"o": "orders"})

    assert result.decision == "REVIEW"
    assert "PLAN_LARGE_SCAN" in {finding["rule_id"] for finding in result.findings}
    assert result.summary["tables"][0][flag] is True


@pytest.mark.parametrize("flag", ["backward_index_scan", "not_exists"])
def test_index_and_join_observations_do_not_allow_other_unknown_plan_fields(limits, flag):
    node = {**table(), flag: True, "unrecognized_scan_detail": "private-marker"}
    result = analyze_plan(plan(node), limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    serialized = json.dumps({"summary": result.summary, "findings": result.findings})
    assert "unrecognized_scan_detail" not in serialized and "private-marker" not in serialized


@pytest.mark.parametrize("operation,flag,produced,rule", [
    (None, None, 1000001, "PLAN_LARGE_JOIN"),
    ("ordering_operation", "using_filesort", 100001, "PLAN_LARGE_SORT_OR_TEMPORARY"),
    ("grouping_operation", "using_temporary_table", 100001, "PLAN_LARGE_SORT_OR_TEMPORARY"),
])
def test_not_exists_does_not_exempt_join_sort_or_temporary_table_risk(
    limits, operation, flag, produced, rule,
):
    node = {**table(access="ref", scan=11, produced=produced), "not_exists": True}
    fixture = (
        {"query_block": {operation: {flag: True, "table": node}}} if operation else plan(node)
    )
    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "REVIEW"
    assert rule in {finding["rule_id"] for finding in result.findings}
    assert result.summary["tables"][0]["not_exists"] is True


def test_not_exists_cannot_replace_a_missing_row_estimate_with_zero(limits):
    node = {**table(), "not_exists": True}
    del node["rows_produced_per_join"]
    result = analyze_plan(plan(node), limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert "rows_produced_per_join" not in result.summary["tables"][0]


def test_nested_join_keeps_paths_and_does_not_sum_prefix_estimates(limits):
    fixture = {
        "query_block": {
            "select_id": 1,
            "nested_loop": [
                {"table": table(name="o", produced=600000)},
                {"table": table(name="c", produced=600000, access="ref")},
            ],
        }
    }

    result = analyze_plan(fixture, limits, {"o": "orders", "c": "customers"})

    assert result.decision == "ALLOW"
    assert [item["jsonpath"] for item in result.summary["tables"]] == [
        "$.query_block.nested_loop[0].table",
        "$.query_block.nested_loop[1].table",
    ]
    assert [item["rows_produced_per_join"] for item in result.summary["tables"]] == [600000, 600000]


@pytest.mark.parametrize(
    "operation", ["ordering_operation", "grouping_operation", "duplicates_removal"]
)
@pytest.mark.parametrize("flag", ["using_filesort", "using_temporary_table"])
@pytest.mark.parametrize(("rows", "decision"), [(100000, "ALLOW"), (100001, "REVIEW")])
def test_sort_and_temporary_table_risk_depends_on_input_scale(
    limits, operation, flag, rows, decision
):
    fixture = {"query_block": {operation: {flag: True, "table": table(produced=rows)}}}

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == decision
    assert result.summary["operations"][0]["rows_produced_per_join"] == rows
    if decision == "REVIEW":
        assert result.findings[0]["rule_id"] == "PLAN_LARGE_SORT_OR_TEMPORARY"


def test_sort_uses_last_join_prefix_without_summing_all_inputs(limits):
    fixture = {
        "query_block": {
            "ordering_operation": {
                "using_filesort": True,
                "nested_loop": [
                    {"table": table("o", produced=80000)},
                    {"table": table("c", produced=80000)},
                ],
            }
        }
    }

    result = analyze_plan(fixture, limits, {"o": "orders", "c": "customers"})

    assert result.decision == "ALLOW"
    assert result.summary["operations"][0]["rows_produced_per_join"] == 80000


def test_grouping_and_ordering_wrappers_preserve_their_hierarchy(limits):
    fixture = {
        "query_block": {
            "ordering_operation": {
                "using_filesort": True,
                "grouping_operation": {"using_temporary_table": True, "table": table()},
            }
        }
    }

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "ALLOW"
    assert {item["jsonpath"] for item in result.summary["operations"]} == {
        "$.query_block.ordering_operation",
        "$.query_block.ordering_operation.grouping_operation",
    }


@pytest.mark.parametrize("key", ["rows_examined_per_scan", "rows_produced_per_join", "filtered"])
def test_missing_required_table_estimate_is_unknown(limits, key):
    fixture = plan()
    del fixture["query_block"]["table"][key]

    assert analyze_plan(fixture, limits, {"o": "orders"}).decision == "UNKNOWN"


@pytest.mark.parametrize(
    "value", [None, True, False, -1, "-1", "NaN", "Infinity", "1e999", "bad", "1_000", [], {}]
)
@pytest.mark.parametrize("key", ["rows_examined_per_scan", "rows_produced_per_join", "filtered"])
def test_invalid_numeric_estimates_are_unknown(limits, value, key):
    fixture = plan()
    fixture["query_block"]["table"][key] = value

    assert analyze_plan(fixture, limits, {"o": "orders"}).decision == "UNKNOWN"


def test_filtered_cannot_exceed_100(limits):
    fixture = plan()
    fixture["query_block"]["table"]["filtered"] = "100.01"

    assert analyze_plan(fixture, limits, {"o": "orders"}).decision == "UNKNOWN"


def test_numeric_strings_and_cost_are_evidence_without_runtime_conversion(limits):
    fixture = plan(table(scan="2e1", produced="5.0"))
    fixture["query_block"]["table"].update(
        {
            "key": "PRIMARY",
            "used_key_parts": ["id"],
            "filtered": "25.0",
            "cost_info": {"read_cost": "1e12", "data_read_per_join": "16K"},
        }
    )

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "ALLOW"
    assert result.summary["tables"][0]["cost_info"]["read_cost"] == 1e12
    assert "seconds" not in json.dumps(result.summary)


@pytest.mark.parametrize(
    "cost",
    [
        None,
        [],
        {"query_cost": "NaN"},
        {"query_cost": -1},
        {"query_cost": True},
        {"new_cost": 1},
        {"data_read_per_join": "secret-marker"},
    ],
)
def test_invalid_or_unknown_cost_fields_do_not_allow_a_plan(limits, cost):
    fixture = plan()
    fixture["query_block"]["cost_info"] = cost

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert "secret-marker" not in json.dumps(result.summary)


@pytest.mark.parametrize(
    "message",
    [
        "No tables used",
        "Impossible WHERE",
        "Impossible WHERE noticed after reading const tables",
        "Select tables optimized away",
        "no matching row in const table",
        "const row not found",
    ],
)
def test_exact_mysql_special_cases_allow_missing_table_estimates(limits, message):
    result = analyze_plan({"query_block": {"select_id": 1, "message": message}}, limits, {})

    assert result.decision == "ALLOW"
    assert result.summary == {"tables": [], "operations": []}
    assert result.findings
    assert message not in json.dumps(result.findings, ensure_ascii=False)


def test_special_message_cannot_override_regular_large_scan_evidence(limits):
    fixture = plan(table(scan=100001))
    fixture["query_block"]["table"]["message"] = "Select tables optimized away"

    assert analyze_plan(fixture, limits, {"o": "orders"}).decision == "UNKNOWN"


def test_sort_without_known_input_scale_is_unknown(limits):
    fixture = {
        "query_block": {
            "ordering_operation": {
                "using_filesort": True,
                "table": {"table_name": "o", "message": "const row not found"},
            }
        }
    }

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert any("输入规模" in finding["message"] for finding in result.findings)


@pytest.mark.parametrize(
    "addition",
    [
        {"attached_subqueries": []},
        {"select_list_subqueries": []},
        {"union_result": {}},
        {"materialized_from_subquery": {}},
        {"windowing": {}},
        {"future_secret_marker": {}},
    ],
)
def test_unsupported_structures_are_unknown_and_do_not_leak_field_names(limits, addition):
    fixture = plan()
    fixture["query_block"].update(addition)

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert "future_secret_marker" not in json.dumps((result.summary, result.findings))


@pytest.mark.parametrize(
    "fixture",
    [
        {},
        {"inputs": [], "estimated_rows": 1},
        {"query_block": []},
        {"query_block": {}},
        {"query_block": {"nested_loop": []}},
        {"query_block": {"message": "secret-marker"}},
    ],
)
def test_unknown_versions_shapes_or_messages_do_not_allow(limits, fixture):
    result = analyze_plan(fixture, limits, {})

    assert result.decision == "UNKNOWN"
    assert "secret-marker" not in json.dumps((result.summary, result.findings))


def test_unknown_plan_table_is_not_treated_as_authorized_or_echoed(limits):
    result = analyze_plan(plan(table(name="secret_marker")), limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert "secret_marker" not in json.dumps((result.summary, result.findings))


@pytest.mark.parametrize("aliases", [{"o": "secret.marker"}, {"secret marker": "orders"}, {"o": 1}])
def test_only_validated_identifier_aliases_can_be_reported(limits, aliases):
    result = analyze_plan(plan(), limits, aliases)

    assert result.decision == "UNKNOWN"
    assert "secret" not in json.dumps((result.summary, result.findings))


def test_sensitive_conditions_and_literals_never_enter_summary_or_findings(limits):
    fixture = plan()
    fixture["query_block"]["table"].update(
        {
            "attached_condition": "customer_code = 'secret-marker'",
            "index_condition": "id = 'secret-marker'",
            "ref": ["const", "secret-marker"],
            "used_columns": ["secret-marker"],
        }
    )
    before = copy.deepcopy(fixture)

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "ALLOW"
    assert "secret-marker" not in json.dumps((result.summary, result.findings))
    assert fixture == before


def test_verified_index_and_join_flags_are_available_to_the_explainer(limits):
    fixture = plan()
    safe_flags = {
        "using_index": True,
        "using_index_condition": False,
        "using_index_for_group_by": True,
        "using_join_buffer": "hash join",
    }
    fixture["query_block"]["table"].update(safe_flags)

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "ALLOW"
    assert {key: result.summary["tables"][0][key] for key in safe_flags} == safe_flags


@pytest.mark.parametrize("name", ["using_index", "using_join_buffer"])
def test_unknown_index_or_join_flag_content_is_not_reported(limits, name):
    fixture = plan()
    fixture["query_block"]["table"][name] = "secret-marker"

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert "secret-marker" not in json.dumps((result.summary, result.findings))


def test_large_raw_literal_is_subject_to_byte_budget_even_though_not_reported(limits):
    fixture = plan()
    fixture["query_block"]["table"]["attached_condition"] = "secret-marker" * 1000
    bounded = limits.model_copy(update={"max_plan_bytes": 1024})

    result = analyze_plan(fixture, bounded, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert result.summary == {"tables": [], "operations": []}
    assert "secret-marker" not in json.dumps(result.findings)


def test_json_node_budget_counts_nested_values_and_stops_cycles(limits):
    bounded = limits.model_copy(update={"max_plan_nodes": 3})
    assert analyze_plan(plan(), bounded, {"o": "orders"}).decision == "UNKNOWN"
    cyclic = {}
    cyclic["query_block"] = cyclic
    assert analyze_plan(cyclic, bounded, {}).decision == "UNKNOWN"


def test_json_node_budget_accepts_the_exact_boundary(limits):
    fixture = {"query_block": {"message": "No tables used"}}

    assert (
        analyze_plan(fixture, limits.model_copy(update={"max_plan_nodes": 3}), {}).decision
        == "ALLOW"
    )
    assert (
        analyze_plan(fixture, limits.model_copy(update={"max_plan_nodes": 2}), {}).decision
        == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"access_type": "secret-marker"},
        {"access_type": None},
        {"key": "secret-marker"},
        {"used_key_parts": ["expression('secret-marker')"]},
        {"attached_subqueries": [{"query": "secret-marker"}]},
        {"materialized_from_subquery": {"query_block": {"message": "No tables used"}}},
        {"attached_condition": {"query": "secret-marker"}},
    ],
)
def test_unsupported_table_details_are_unknown_without_echoing_them(limits, extra):
    fixture = plan()
    fixture["query_block"]["table"].update(extra)

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert "secret-marker" not in json.dumps((result.summary, result.findings))


@pytest.mark.parametrize("flags", [{}, {"using_filesort": "false"}, {"using_temporary_table": 1}])
def test_missing_or_invalid_operation_flags_do_not_allow(limits, flags):
    fixture = {"query_block": {"ordering_operation": {**flags, "table": table()}}}

    assert analyze_plan(fixture, limits, {"o": "orders"}).decision == "UNKNOWN"


def test_unknown_evidence_takes_precedence_over_a_known_review_finding(limits):
    fixture = plan(table(scan=100001))
    del fixture["query_block"]["table"]["filtered"]

    result = analyze_plan(fixture, limits, {"o": "orders"})

    assert result.decision == "UNKNOWN"
    assert {finding["rule_id"] for finding in result.findings} == {
        "PLAN_UNKNOWN",
        "PLAN_LARGE_SCAN",
    }
