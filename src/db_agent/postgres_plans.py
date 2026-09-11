"""A fail-closed reader of PostgreSQL 18 ordinary EXPLAIN JSON, never ANALYZE.

Plan Rows estimates output after filters. Sequential scans therefore require
trusted relation estimates; filtered index accesses conservatively use those
estimates too. These possibly stale estimates are not measured scan counts.
"""

import json
import math
import re
from typing import TYPE_CHECKING

from db_agent.plans import PlanAnalysis

if TYPE_CHECKING:
    from db_agent.config import AnalysisSettings

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_SCANS = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
_JOINS = {"Nested Loop", "Hash Join", "Merge Join"}
_UNARY = {"Limit", "Sort", "Aggregate", "Hash", "Materialize"}
_BITMAPS = {"Bitmap Index Scan", "BitmapAnd", "BitmapOr"}
_NODES = _SCANS | _JOINS | _UNARY | _BITMAPS | {"Result"}
_COMMON = {
    "Node Type", "Parent Relationship", "Parallel Aware", "Async Capable",
    "Startup Cost", "Total Cost", "Plan Rows", "Plan Width", "Output", "Plans", "Disabled",
}
_FIELDS = {
    "Seq Scan": {"Relation Name", "Schema", "Alias", "Filter"},
    "Index Scan": {
        "Relation Name", "Schema", "Alias", "Filter", "Index Name", "Index Cond",
        "Scan Direction", "Order By",
    },
    "Index Only Scan": {
        "Relation Name", "Schema", "Alias", "Filter", "Index Name", "Index Cond",
        "Scan Direction",
    },
    "Bitmap Heap Scan": {"Relation Name", "Schema", "Alias", "Filter", "Recheck Cond"},
    "Bitmap Index Scan": {"Index Name", "Index Cond"},
    "BitmapAnd": set(),
    "BitmapOr": set(),
    "Nested Loop": {"Join Type", "Inner Unique", "Join Filter", "Filter"},
    "Hash Join": {"Join Type", "Inner Unique", "Hash Cond", "Join Filter", "Filter"},
    "Merge Join": {"Join Type", "Inner Unique", "Merge Cond", "Join Filter", "Filter"},
    "Limit": set(),
    "Sort": {"Sort Key"},
    "Aggregate": {"Strategy", "Partial Mode", "Group Key", "Filter", "Planned Partitions"},
    "Hash": set(),
    "Materialize": set(),
    "Result": {"One-Time Filter"},
}
_EXPRESSIONS = {
    "Filter", "Index Cond", "Recheck Cond", "Join Filter", "Hash Cond", "Merge Cond",
    "One-Time Filter",
}
_EXPRESSION_LISTS = {"Output", "Sort Key", "Group Key", "Order By"}


def _number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError
    try:
        number = float(value)
    except OverflowError:
        raise ValueError from None
    if not math.isfinite(number) or number < 0:
        raise ValueError
    return number


class _Reader:
    def __init__(self, limits, aliases, relation_rows, expected_schema):
        self.limits = limits
        self.aliases = aliases
        self.relation_rows = relation_rows
        self.expected_schema = expected_schema
        self.unknown = False
        self.review = False
        self.findings: list[dict] = []
        self.summary: dict = {
            "dialect": "postgres", "tables": [], "operations": [],
            "estimate_basis": "普通 EXPLAIN 输出估计及可信关系统计；不是实测，统计可能陈旧",
        }

    def unsupported(self, path: str, message: str = "计划结构或证据不在当前支持范围内"):
        self.unknown = True
        self.findings.append({
            "rule_id": "PLAN_UNKNOWN", "message": message, "evidence": {"jsonpath": path},
        })

    def estimate(self, node: dict, key: str, path: str) -> float | None:
        try:
            return _number(node[key])
        except (KeyError, ValueError):
            self.unsupported(path, "关键估算字段缺失或不是有效数值")
            return None

    def large(self, rows: float | None, threshold: int, rule: str, path: str):
        if rows is not None and rows > threshold:
            self.review = True
            self.findings.append({
                "rule_id": rule, "message": "估计行数严格超过审核阈值；不是实测行数或耗时",
                "evidence": {"jsonpath": path, "estimated_rows": rows, "threshold": threshold},
            })

    def bounded(self, plan: object) -> bool:
        pending = [plan]
        count = 0
        while pending:
            value = pending.pop()
            count += 1
            if isinstance(value, dict) and all(isinstance(key, str) for key in value):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif value is not None and type(value) not in (str, int, float, bool):
                self.unsupported("$", "计划包含不支持的 JSON 值类型")
                return False
            if count + len(pending) > self.limits.max_plan_nodes:
                self.unsupported("$", "计划超过 JSON 节点预算")
                return False
        try:
            size = len(json.dumps(
                plan, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            ).encode("utf-8"))
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self.unsupported("$", "计划不是有效 JSON 数据")
            return False
        if size > self.limits.max_plan_bytes:
            self.unsupported("$", "计划超过 JSON 字节预算")
            return False
        return True

    def scan(self, node: dict, path: str, rows: float | None):
        alias, table = node.get("Alias"), node.get("Relation Name")
        if (
            not isinstance(alias, str) or alias not in self.aliases
            or self.aliases[alias] != table or node.get("Schema") != self.expected_schema
        ):
            self.unsupported(path, "计划引用了未验证的表、schema 或别名")
            return
        kind = node["Node Type"]
        # No selective index predicate, or a residual filter: output rows cannot
        # establish the number of input tuples. Full relation statistics are a
        # conservative estimate, not a claim about actual index selectivity.
        full_estimate = (
            kind in {"Seq Scan", "Bitmap Heap Scan"}
            or "Filter" in node or "Index Cond" not in node
        )
        scan_rows = rows
        basis = "plan_output_without_residual_filter"
        if full_estimate:
            scan_rows = self.estimate(self.relation_rows, table, path)
            if scan_rows is not None and rows is not None:
                scan_rows = max(scan_rows, rows)
            basis = "relation_statistics_conservative"
        self.summary["tables"].append({
            "table": table, "schema": self.expected_schema, "alias": alias,
            "access_type": kind, "jsonpath": path, "rows_examined_per_scan": scan_rows,
            "rows_produced_per_join": rows, "scan_estimate_basis": basis,
        })
        self.large(scan_rows, self.limits.review_scan_rows, "PLAN_LARGE_SCAN", path)

    def node(
        self, node: object, path: str, relationship: str | None = None, *, bitmap: bool = False,
    ) -> float | None:
        if not isinstance(node, dict):
            self.unsupported(path)
            return None
        kind = node.get("Node Type")
        if not isinstance(kind, str) or kind not in _NODES:
            self.unsupported(path, "计划节点种类尚未支持")
            return None
        if node.keys() - (_COMMON | _FIELDS[kind]):
            self.unsupported(path)
        if (kind in _BITMAPS) != bitmap:
            self.unsupported(path, "位图访问必须属于已验证的位图堆扫描结构")
        if node.get("Parent Relationship") != relationship:
            self.unsupported(path, "计划父子关系不完整或包含未支持子计划")
        for key in {"Parallel Aware", "Async Capable"} & node.keys():
            if node[key] is not False:
                self.unsupported(path, "不支持并行或异步计划节点")
        for key in {"Disabled", "Inner Unique"} & node.keys():
            if type(node[key]) is not bool:
                self.unsupported(path)
        for key in _EXPRESSIONS & node.keys():
            if not isinstance(node[key], str) or not node[key]:
                self.unsupported(path)
        for key in _EXPRESSION_LISTS & node.keys():
            if not isinstance(node[key], list) or not all(
                isinstance(value, str) for value in node[key]
            ):
                self.unsupported(path)
        if "Index Name" in _FIELDS[kind]:
            index = node.get("Index Name")
            if not isinstance(index, str) or not _IDENTIFIER.fullmatch(index):
                self.unsupported(path, "缺少受支持的索引标识")
        if "Scan Direction" in _FIELDS[kind] and node.get("Scan Direction") not in {
            "Forward", "Backward", "NoMovement",
        }:
            self.unsupported(path)
        rows = self.estimate(node, "Plan Rows", path)
        self.estimate(node, "Plan Width", path)
        startup = self.estimate(node, "Startup Cost", path)
        total = self.estimate(node, "Total Cost", path)
        if startup is not None and total is not None and startup > total:
            self.unsupported(path, "计划启动成本大于总成本")
        children = node.get("Plans", [])
        if not isinstance(children, list):
            self.unsupported(path)
            return rows
        expected_count = 2 if kind in _JOINS else 1 if kind in _UNARY else 0
        if kind == "Bitmap Heap Scan":
            expected_count = 1
        if kind == "Result":
            valid_count = len(children) <= 1
        elif kind in {"BitmapAnd", "BitmapOr"}:
            valid_count = len(children) >= 2
        else:
            valid_count = len(children) == expected_count
        if not valid_count:
            self.unsupported(path, "计划子节点数量不符合已支持结构")
        input_rows = []
        for index, child in enumerate(children):
            is_bitmap = kind in {"Bitmap Heap Scan", "BitmapAnd", "BitmapOr"}
            parent = (
                "Member" if kind in {"BitmapAnd", "BitmapOr"} else "Inner" if index else "Outer"
            )
            if is_bitmap and (
                not isinstance(child, dict) or child.get("Node Type") not in _BITMAPS
            ):
                self.unsupported(path, "位图节点包含未支持的子结构")
            input_rows.append(self.node(child, f"{path}.Plans[{index}]", parent, bitmap=is_bitmap))
        if kind in _SCANS:
            self.scan(node, path, rows)
        if kind in _JOINS:
            # PostgreSQL may swap the physical sides of a SQL LEFT JOIN.
            if node.get("Join Type") not in {"Inner", "Left", "Right"}:
                self.unsupported(path, "计划连接方式尚未支持")
            self.large(rows, self.limits.review_join_rows, "PLAN_LARGE_JOIN", path)
        if kind == "Aggregate":
            if node.get("Strategy") not in {"Plain", "Sorted", "Hashed"}:
                self.unsupported(path, "聚合策略尚未支持")
            if node.get("Partial Mode") != "Simple":
                self.unsupported(path, "分段或并行聚合尚未支持")
            if "Planned Partitions" in node:
                self.estimate(node, "Planned Partitions", path)
        if kind == "Sort" and not node.get("Sort Key"):
            self.unsupported(path, "排序节点缺少排序键证据")
        if kind in {"Sort", "Aggregate", "Hash", "Materialize"}:
            incoming = input_rows[0] if len(input_rows) == 1 else None
            if incoming is None:
                self.unsupported(path, "排序、聚合或临时结构缺少输入规模证据")
            self.large(
                incoming, self.limits.review_sort_rows, "PLAN_LARGE_SORT_OR_TEMPORARY", path,
            )
        if kind not in _SCANS:
            self.summary["operations"].append({
                "operation": kind, "jsonpath": path, "estimated_rows": rows,
                "estimated_input_rows": input_rows,
            })
        if relationship is None:
            self.summary["cost_info"] = {"startup_cost": startup, "total_cost": total}
        return rows

    def analyze(self, plan: object) -> PlanAnalysis:
        if (
            not isinstance(self.aliases, dict)
            or not all(
                isinstance(alias, str) and _IDENTIFIER.fullmatch(alias)
                and isinstance(table, str) and _IDENTIFIER.fullmatch(table)
                for alias, table in self.aliases.items()
            )
            or not isinstance(self.expected_schema, str)
            or not _IDENTIFIER.fullmatch(self.expected_schema)
            or not isinstance(self.relation_rows, dict)
        ):
            self.unsupported("$", "缺少有效的可信表、schema 或统计映射")
        elif self.bounded(plan):
            if (
                not isinstance(plan, list) or len(plan) != 1 or not isinstance(plan[0], dict)
                or "Plan" not in plan[0] or plan[0].keys() - {"Plan", "Query Identifier"}
            ):
                self.unsupported("$", "仅支持单份 PostgreSQL 普通 EXPLAIN JSON 结构")
            else:
                if "Query Identifier" in plan[0] and type(plan[0]["Query Identifier"]) is not int:
                    self.unsupported("$", "查询标识格式不受支持")
                self.node(plan[0]["Plan"], "$[0].Plan")
        decision = "UNKNOWN" if self.unknown else "REVIEW" if self.review else "ALLOW"
        return PlanAnalysis(decision, tuple(self.findings), self.summary)


def analyze_postgres_plan(
    plan: object,
    limits: "AnalysisSettings",
    aliases: dict[str, str],
    relation_rows: dict[str, int | float] | None = None,
    *,
    expected_schema: str = "business",
) -> PlanAnalysis:
    """Assess bounded plans using connector-owned identities and relation statistics."""
    statistics = {} if relation_rows is None else relation_rows
    return _Reader(limits, aliases, statistics, expected_schema).analyze(plan)
