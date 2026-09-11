"""Constructed PostgreSQL plan fixtures; actual server tests are separate."""

import copy
import json

import pytest

from db_agent.config import AnalysisSettings
from db_agent.postgres_plans import analyze_postgres_plan


@pytest.fixture
def limits():
    return AnalysisSettings(
        _env_file=None, max_plan_bytes=65536, max_plan_nodes=256,
        review_scan_rows=100000, review_join_rows=1000000, review_sort_rows=100000,
    )


def node(kind="Seq Scan", rows=10, **fields):
    result = {
        "Node Type": kind, "Parallel Aware": False, "Async Capable": False,
        "Startup Cost": 0, "Total Cost": 10, "Plan Rows": rows, "Plan Width": 8,
    }
    if kind in {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"}:
        result.update({"Relation Name": "orders", "Schema": "business", "Alias": "o"})
    if kind in {"Index Scan", "Index Only Scan", "Bitmap Index Scan"}:
        result.update({"Index Name": "orders_pkey", "Index Cond": "(o.id = 5)"})
    if kind in {"Index Scan", "Index Only Scan"}:
        result["Scan Direction"] = "Forward"
    result.update(fields)
    return result


def parent(kind, children, rows=10, **fields):
    children = copy.deepcopy(children)
    for index, child in enumerate(children):
        child["Parent Relationship"] = (
            "Member" if kind in {"BitmapAnd", "BitmapOr"} else "Inner" if index else "Outer"
        )
    return node(kind, rows, Plans=children, **fields)


def assess(limits, value=None, stats=None, aliases=None, **kwargs):
    return analyze_postgres_plan(
        [{"Plan": node() if value is None else value}], limits,
        {"o": "orders", "c": "customers"} if aliases is None else aliases,
        {"orders": 10, "customers": 5} if stats is None else stats, **kwargs,
    )


def rules(result):
    return {finding["rule_id"] for finding in result.findings}


def test_small_scan_uses_trusted_relation_statistics(limits):
    result = assess(limits, node(rows=1, Filter="secret = 'private'"), stats={"orders": 500})
    assert result.decision == "ALLOW"
    assert result.summary["tables"][0]["rows_examined_per_scan"] == 500
    assert result.summary["tables"][0]["rows_produced_per_join"] == 1
    assert "private" not in json.dumps(result.summary)
    assert result.summary["cost_info"] == {"startup_cost": 0, "total_cost": 10}


def test_limit_and_filtered_output_do_not_exempt_large_scan(limits):
    result = assess(
        limits, parent("Limit", [node(rows=1, Filter="id < 0")], rows=1),
        stats={"orders": 100001},
    )
    assert result.decision == "REVIEW" and "PLAN_LARGE_SCAN" in rules(result)


@pytest.mark.parametrize("stats", [{}, {"orders": -1}, {"orders": None}, {"orders": True}])
def test_unknown_relation_statistics_never_turn_into_zero(limits, stats):
    result = assess(limits, stats=stats)
    assert result.decision == "UNKNOWN"
    assert result.summary["tables"][0]["rows_examined_per_scan"] is None


@pytest.mark.parametrize("kind", ["Index Scan", "Index Only Scan"])
def test_selective_index_without_residual_filter_can_use_output_estimate(limits, kind):
    result = assess(limits, node(kind, rows=1), stats={})
    assert result.decision == "ALLOW"
    assert result.summary["tables"][0]["rows_examined_per_scan"] == 1
    filtered = assess(limits, node(kind, rows=1, Filter="id > 0"), stats={"orders": 100001})
    assert filtered.decision == "REVIEW"
    assert assess(limits, node(kind, Filter="id > 0"), stats={}).decision == "UNKNOWN"


def test_full_index_walk_cannot_use_post_limit_rows(limits):
    child = node("Index Scan", rows=1)
    del child["Index Cond"]
    assert assess(limits, child, stats={"orders": 100001}).decision == "REVIEW"


@pytest.mark.parametrize("kind", ["BitmapAnd", "BitmapOr"])
def test_bitmap_tree_checks_every_access_and_parent_relationship(limits, kind):
    bitmap = parent(kind, [node("Bitmap Index Scan"), node("Bitmap Index Scan")])
    heap = parent("Bitmap Heap Scan", [bitmap], rows=1, **{"Recheck Cond": "id > 0"})
    assert assess(limits, heap).decision == "ALLOW"
    assert assess(limits, heap, stats={"orders": 100001}).decision == "REVIEW"
    heap["Plans"][0]["Plans"][1]["Node Type"] = "Foreign Scan"
    assert assess(limits, heap).decision == "UNKNOWN"


@pytest.mark.parametrize("kind", ["Nested Loop", "Hash Join", "Merge Join"])
@pytest.mark.parametrize("join_type", ["Inner", "Left", "Right"])
def test_known_physical_joins_and_strict_output_threshold(limits, kind, join_type):
    children = [node(), node(Alias="c", **{"Relation Name": "customers"})]
    plan = parent(kind, children, rows=1000000, **{"Join Type": join_type, "Inner Unique": False})
    assert assess(limits, plan).decision == "ALLOW"
    plan["Plan Rows"] += 1
    result = assess(limits, plan)
    assert result.decision == "REVIEW" and "PLAN_LARGE_JOIN" in rules(result)


@pytest.mark.parametrize(("kind", "fields"), [
    ("Sort", {"Sort Key": ["secret"]}), ("Hash", {}), ("Materialize", {}),
    ("Aggregate", {"Strategy": "Plain", "Partial Mode": "Simple"}),
    ("Aggregate", {"Strategy": "Sorted", "Partial Mode": "Simple", "Group Key": ["id"]}),
    ("Aggregate", {"Strategy": "Hashed", "Partial Mode": "Simple", "Planned Partitions": 0}),
])
def test_sort_and_temporary_estimates_use_input_not_small_output(limits, kind, fields):
    child = node("Index Scan", rows=100000)
    assert assess(limits, parent(kind, [child], rows=1, **fields)).decision == "ALLOW"
    child["Plan Rows"] = 100001
    result = assess(limits, parent(kind, [child], rows=1, **fields))
    assert result.decision == "REVIEW" and "PLAN_LARGE_SORT_OR_TEMPORARY" in rules(result)
    assert "secret" not in json.dumps(result.summary)


def test_zero_and_exact_scan_threshold_are_allowed(limits):
    for rows in (0, 100000):
        assert assess(limits, node(rows=rows), stats={"orders": rows}).decision == "ALLOW"


def test_empty_optimized_result_can_omit_relations(limits):
    result = assess(limits, node("Result", rows=0, **{"One-Time Filter": "false"}), stats={})
    assert result.decision == "ALLOW"
    assert result.summary["tables"] == []


@pytest.mark.parametrize("key", ["Plan Rows", "Plan Width", "Startup Cost", "Total Cost"])
@pytest.mark.parametrize("bad", [None, -1, True, "10", float("nan"), float("inf"), []])
def test_missing_invalid_estimates_fail_closed(limits, key, bad):
    assert assess(limits, node(**{key: bad})).decision == "UNKNOWN"
    plan = node()
    del plan[key]
    assert assess(limits, plan).decision == "UNKNOWN"


@pytest.mark.parametrize("kind", [
    "Gather", "Gather Merge", "WindowAgg", "Append", "CTE Scan", "Function Scan", "Foreign Scan",
    "Recursive Union", "Subquery Scan", "ModifyTable", "LockRows", "Memoize", "Incremental Sort",
])
def test_unimplemented_nodes_are_unknown(limits, kind):
    assert assess(limits, node(kind)).decision == "UNKNOWN"


@pytest.mark.parametrize("fields", [
    {"Parallel Aware": True}, {"Async Capable": 0}, {"Actual Rows": 10},
    {"unknown_private_field": "sensitive-marker"}, {"Schema": "private"}, {"Alias": "unknown"},
    {"Relation Name": "private"}, {"Parent Relationship": "SubPlan"}, {"Output": {}},
    {"Filter": 0}, {"Plans": {}}, {"Startup Cost": 11},
])
def test_untrusted_structural_details_are_rejected_without_echo(limits, fields):
    result = assess(limits, node(**fields))
    assert result.decision == "UNKNOWN"
    public = json.dumps({"summary": result.summary, "findings": result.findings})
    assert "private" not in public and "sensitive-marker" not in public


@pytest.mark.parametrize("plan", [
    {}, [], [{"Plan": {}}], [{"Plan": node()}, {"Plan": node()}],
    [{"Plan": node(), "Execution Time": 1}], [{"Plan": node(), "Query Identifier": True}],
])
def test_only_single_ordinary_explain_document_is_accepted(limits, plan):
    result = analyze_postgres_plan(plan, limits, {"o": "orders"}, {"orders": 10})
    assert result.decision == "UNKNOWN"


def test_plan_bytes_and_json_value_node_budgets(limits):
    assert assess(limits.model_copy(update={"max_plan_nodes": 4})).decision == "UNKNOWN"
    result = assess(
        limits.model_copy(update={"max_plan_bytes": 1024}), node(Output=["secret" * 200]),
    )
    assert result.decision == "UNKNOWN"
    assert "secret" not in json.dumps(result.findings)


def test_unknown_has_precedence_over_large_review(limits):
    result = assess(limits, node(**{"Future Detail": True}), stats={"orders": 100001})
    assert result.decision == "UNKNOWN"
    assert rules(result) == {"PLAN_UNKNOWN", "PLAN_LARGE_SCAN"}


def test_explicit_schema_and_alias_mapping_are_required(limits):
    assert assess(limits, aliases={"o": "wrong"}).decision == "UNKNOWN"
    assert assess(limits, aliases={"bad;": "orders"}).decision == "UNKNOWN"
    assert assess(limits, expected_schema="public").decision == "UNKNOWN"
    assert assess(limits, node(Schema="public"), expected_schema="public").decision == "ALLOW"


def test_invalid_child_count_or_relationship_never_becomes_empty_input(limits):
    assert assess(limits, parent("Sort", [], **{"Sort Key": ["id"]})).decision == "UNKNOWN"
    assert assess(limits, parent("Seq Scan", [node()])).decision == "UNKNOWN"
    join = parent("Hash Join", [node(), node()], **{"Join Type": "Inner"})
    join["Plans"][1]["Parent Relationship"] = "Outer"
    assert assess(limits, join).decision == "UNKNOWN"


def test_bitmap_nodes_cannot_appear_outside_a_heap_scan(limits):
    assert assess(limits, node("Bitmap Index Scan")).decision == "UNKNOWN"
    plan = parent("Limit", [node("Bitmap Index Scan")])
    assert assess(limits, plan).decision == "UNKNOWN"
