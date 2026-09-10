"""Parse a deliberately limited MySQL EXPLAIN JSON v1 plan without exposing SQL."""

import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db_agent.config import AnalysisSettings

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SOURCES = {
    "table",
    "nested_loop",
    "ordering_operation",
    "grouping_operation",
    "duplicates_removal",
}
_OPERATIONS = _SOURCES - {"table", "nested_loop"}
_ACCESS_TYPES = {
    "system",
    "const",
    "eq_ref",
    "ref",
    "fulltext",
    "ref_or_null",
    "index_merge",
    "range",
    "index",
    "ALL",
}
_COST_FIELDS = {"query_cost", "read_cost", "eval_cost", "prefix_cost", "sort_cost"}
_TABLE_FLAGS = {"using_index", "using_index_condition", "using_index_for_group_by"}
_IGNORED_STRINGS = {"attached_condition", "index_condition", "key_length"}
_IGNORED_LISTS = {"possible_keys", "ref", "used_columns", "partitions"}
_SPECIAL_MESSAGES = {
    "No tables used": ("PLAN_NO_TABLES", "计划无需访问表"),
    "Impossible WHERE": ("PLAN_IMPOSSIBLE_WHERE", "优化器判断查询条件不可能匹配"),
    "Impossible WHERE noticed after reading const tables": (
        "PLAN_IMPOSSIBLE_WHERE",
        "优化器读取常量表后判断查询条件不可能匹配",
    ),
    "Select tables optimized away": ("PLAN_OPTIMIZED_AWAY", "优化器已消除表访问计划"),
    "no matching row in const table": ("PLAN_CONST_NO_MATCH", "常量表中没有匹配行"),
    "const row not found": ("PLAN_CONST_NO_MATCH", "常量行不存在"),
}


@dataclass(frozen=True)
class PlanAnalysis:
    decision: str
    findings: tuple[dict, ...]
    summary: dict


def _number(value: object, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError
    if (
        isinstance(value, str)
        and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value)
        is None
    ):
        raise ValueError
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError from None
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        raise ValueError
    return number


class _PlanReader:
    def __init__(self, limits: "AnalysisSettings", aliases: dict[str, str]):
        self.limits = limits
        self.aliases = aliases
        self.findings: list[dict] = []
        self.summary: dict = {"tables": [], "operations": []}
        self.unknown = False
        self.review = False

    def finding(self, rule: str, message: str, path: str, **evidence):
        self.findings.append(
            {"rule_id": rule, "message": message, "evidence": {"jsonpath": path, **evidence}}
        )

    def unsupported(self, path: str, message: str = "计划结构或证据不在当前支持范围内"):
        self.unknown = True
        self.finding("PLAN_UNKNOWN", message, path)

    def estimate(self, node: dict, key: str, path: str, maximum: float | None = None):
        try:
            return _number(node[key], maximum=maximum)
        except (KeyError, ValueError):
            self.unsupported(path, "关键估算字段缺失或不是有效数值")
            return None

    def cost(self, value: object, path: str) -> dict:
        if not isinstance(value, dict):
            self.unsupported(path)
            return {}
        result = {}
        for key, raw in value.items():
            if key in _COST_FIELDS:
                number = self.estimate(value, key, path)
                if number is not None:
                    result[key] = number
            elif (
                key == "data_read_per_join"
                and isinstance(raw, str)
                and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,10})?[KMGTP]?", raw)
            ):
                result[key] = raw
            else:
                self.unsupported(path, "成本证据字段不在当前支持范围内")
        return result

    def large(self, value: float | None, limit: float, rule: str, message: str, path: str):
        if value is not None and value > limit:
            self.review = True
            self.finding(rule, message, path, estimated_rows=value, threshold=limit)

    def special_message(self, node: dict, path: str) -> bool:
        if "message" not in node:
            return False
        message = node["message"]
        known = _SPECIAL_MESSAGES.get(message) if isinstance(message, str) else None
        if known is None:
            self.unsupported(path, "优化器返回了尚未支持的计划消息")
        else:
            self.finding(*known, path)
        return True

    def table(self, node: object, path: str) -> float | None:
        if not isinstance(node, dict):
            self.unsupported(path)
            return None
        allowed_keys = (
            {
                "table_name",
                "access_type",
                "key",
                "used_key_parts",
                "cost_info",
                "message",
                "rows_examined_per_scan",
                "rows_produced_per_join",
                "filtered",
                "using_join_buffer",
            }
            | _TABLE_FLAGS
            | _IGNORED_STRINGS
            | _IGNORED_LISTS
        )
        if node.keys() - allowed_keys:
            self.unsupported(path)
        alias = node.get("table_name")
        if not isinstance(alias, str) or alias not in self.aliases:
            self.unsupported(path, "计划引用了未验证的表或别名")
            return None
        table = {"table": self.aliases[alias], "alias": alias, "jsonpath": path}
        self.summary["tables"].append(table)
        for key in _IGNORED_STRINGS & node.keys():
            if not isinstance(node[key], str):
                self.unsupported(path)
        for key in _IGNORED_LISTS & node.keys():
            if not isinstance(node[key], list) or not all(
                isinstance(item, str) for item in node[key]
            ):
                self.unsupported(path)
        for key in _TABLE_FLAGS & node.keys():
            if not isinstance(node[key], bool):
                self.unsupported(path)
            else:
                table[key] = node[key]
        if "using_join_buffer" in node:
            if node["using_join_buffer"] not in (
                "hash join",
                "Block Nested Loop",
                "Batched Key Access",
            ):
                self.unsupported(path)
            else:
                table["using_join_buffer"] = node["using_join_buffer"]
        if self.special_message(node, path):
            # A special message does not provide a numeric input-size estimate.
            if node.keys() - {"table_name", "message", "cost_info"}:
                self.unsupported(path, "特殊计划消息与普通访问证据同时出现，不能确定计划语义")
            if "cost_info" in node:
                table["cost_info"] = self.cost(node["cost_info"], path)
            return None
        access_type = node.get("access_type")
        if not isinstance(access_type, str) or access_type not in _ACCESS_TYPES:
            self.unsupported(path, "表访问方式缺失或尚未支持")
        else:
            table["access_type"] = access_type
        for key in ("key", "used_key_parts"):
            if key not in node:
                continue
            raw = node[key]
            names = raw if key == "used_key_parts" else [raw]
            if not isinstance(names, list) or not all(
                isinstance(name, str) and _IDENTIFIER.fullmatch(name) for name in names
            ):
                self.unsupported(path, "索引标识信息尚未支持")
            else:
                table[key] = list(names) if key == "used_key_parts" else raw
        for key in ("rows_examined_per_scan", "rows_produced_per_join", "filtered"):
            value = self.estimate(node, key, path, 100 if key == "filtered" else None)
            if value is not None:
                table[key] = value
        if "cost_info" in node:
            table["cost_info"] = self.cost(node["cost_info"], path)
        self.large(
            table.get("rows_examined_per_scan"),
            self.limits.review_scan_rows,
            "PLAN_LARGE_SCAN",
            "单次扫描行数估计超过审核阈值",
            path,
        )
        self.large(
            table.get("rows_produced_per_join"),
            self.limits.review_join_rows,
            "PLAN_LARGE_JOIN",
            "连接阶段产出行数估计超过审核阈值",
            path,
        )
        if table.get("using_index") is True:
            # MySQL 8.4 EXPLAIN's Using index is covering/index-only evidence.
            # It is distinct from Using index condition (condition pushdown).
            self.finding(
                "PLAN_COVERING_INDEX",
                "计划标记覆盖索引访问，可从索引取得所需列；不是实际 I/O 次数测量。",
                path,
                using_index=True,
            )
        return table.get("rows_produced_per_join")

    def container(self, node: object, path: str, kind: str) -> float | None:
        if not isinstance(node, dict):
            self.unsupported(path)
            return None
        allowed_keys = _SOURCES | {"cost_info"}
        if kind == "query_block":
            allowed_keys |= {"select_id", "message"}
        elif kind in _OPERATIONS:
            allowed_keys |= {"using_filesort", "using_temporary_table"}
        if node.keys() - allowed_keys:
            self.unsupported(path)
        if "select_id" in node:
            value = node["select_id"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                self.unsupported(path)
        costs = self.cost(node["cost_info"], path) if "cost_info" in node else None
        if kind == "query_block" and costs is not None:
            self.summary["cost_info"] = costs
        sources = _SOURCES & node.keys()
        if self.special_message(node, path):
            if sources:
                self.unsupported(path, "特殊计划消息与访问节点同时出现，不能确定计划语义")
            return None
        if len(sources) != 1:
            self.unsupported(path, "计划缺失访问节点或包含不明确的并行结构")
            return None
        source = next(iter(sources))
        child_path = f"{path}.{source}"
        if source == "table":
            rows = self.table(node[source], child_path)
        elif source == "nested_loop":
            children = node[source]
            rows = None
            if not isinstance(children, list) or not children:
                self.unsupported(child_path)
            else:
                for index, child in enumerate(children):
                    # Each estimate already represents a join prefix; do not sum them.
                    rows = self.container(child, f"{child_path}[{index}]", "join_member")
        else:
            rows = self.container(node[source], child_path, source)
        if kind in _OPERATIONS:
            operation = {"operation": kind, "jsonpath": path}
            flags = {"using_filesort", "using_temporary_table"} & node.keys()
            if not flags:
                self.unsupported(path, "排序或分组节点缺少处理方式证据")
            for flag in flags:
                if not isinstance(node[flag], bool):
                    self.unsupported(path)
                else:
                    operation[flag] = node[flag]
            if costs is not None:
                operation["cost_info"] = costs
            if rows is not None:
                operation["rows_produced_per_join"] = rows
            self.summary["operations"].append(operation)
            if operation.get("using_filesort") or operation.get("using_temporary_table"):
                if rows is None:
                    self.unsupported(path, "排序或临时表缺少输入规模证据")
                self.large(
                    rows,
                    self.limits.review_sort_rows,
                    "PLAN_LARGE_SORT_OR_TEMPORARY",
                    "排序或临时表的输入行数估计超过审核阈值",
                    path,
                )
        return rows

    def bounded_json(self, plan: object) -> bool:
        pending = [plan]
        count = 0
        while pending:
            value = pending.pop()
            count += 1
            if count > self.limits.max_plan_nodes:
                self.unsupported("$", "计划超过 JSON 节点预算")
                return False
            if isinstance(value, dict):
                if not all(isinstance(key, str) for key in value):
                    self.unsupported("$", "计划不是有效 JSON 对象")
                    return False
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                self.unsupported("$", "计划包含不支持的 JSON 值类型")
                return False
            if count + len(pending) > self.limits.max_plan_nodes:
                self.unsupported("$", "计划超过 JSON 节点预算")
                return False
        try:
            size = len(
                json.dumps(plan, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self.unsupported("$", "计划不是有效 JSON 数据")
            return False
        if size > self.limits.max_plan_bytes:
            self.unsupported("$", "计划超过 JSON 字节预算")
            return False
        return True

    def analyze(self, plan: dict) -> PlanAnalysis:
        if not isinstance(self.aliases, dict) or not all(
            isinstance(alias, str)
            and _IDENTIFIER.fullmatch(alias)
            and isinstance(table, str)
            and _IDENTIFIER.fullmatch(table)
            for alias, table in self.aliases.items()
        ):
            self.unsupported("$", "缺少有效的 SQL 表别名映射")
        elif self.bounded_json(plan):
            if not isinstance(plan, dict) or set(plan) != {"query_block"}:
                self.unsupported("$", "仅支持明确的 MySQL EXPLAIN JSON v1 结构")
            else:
                self.container(plan["query_block"], "$.query_block", "query_block")
        decision = "UNKNOWN" if self.unknown else "REVIEW" if self.review else "ALLOW"
        return PlanAnalysis(decision, tuple(self.findings), self.summary)


def analyze_plan(plan: dict, limits: "AnalysisSettings", aliases: dict[str, str]) -> PlanAnalysis:
    """Evaluate constructed or real plans; estimates are never runtime measurements."""
    return _PlanReader(limits, aliases).analyze(plan)
