"""Render only captured query evidence; SQL and values stay in this run's memory."""

import json
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class QueryExecution:
    sql: str = field(repr=False)
    report: dict = field(repr=False)


@dataclass(frozen=True)
class AgentRunResult:
    answer: str = field(repr=False)
    queries: list[QueryExecution] = field(repr=False)
    model_calls: int
    tool_calls: list[str]
    semantic_reviews: list[dict] = field(default_factory=list, repr=False)
    query_intents: list[dict] = field(default_factory=list, repr=False)
    analyses: list[QueryExecution] = field(default_factory=list, repr=False)


def has_complete_query_results(result: AgentRunResult) -> bool:
    """Require complete evidence for every query before inheriting its user request."""
    attempts = result.tool_calls.count("execute_query")
    if not attempts or attempts != len(result.queries):
        return False
    for query in result.queries:
        if not isinstance(query, QueryExecution) or not isinstance(query.report, dict):
            return False
        report = query.report
        if (
            report.get("status") != "ok" or report.get("decision") != "ALLOW"
            or report.get("execution_status") != "completed"
            or report.get("error") is not None
            or not isinstance(report.get("result"), dict)
            or report["result"].get("truncated") is not False
        ):
            return False
        data = report["result"]
        columns, rows = data.get("columns"), data.get("rows")
        if (
            not isinstance(columns, list) or not columns
            or any(not isinstance(column, dict)
                   or not isinstance(column.get("name"), str)
                   or not isinstance(column.get("type"), str) for column in columns)
            or not isinstance(rows, list)
            or type(data.get("row_count")) is not int
            or data["row_count"] != len(rows)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
        ):
            return False
    return True


def _visible_text(value: str, *, multiline: bool = False) -> str:
    """Keep data readable while making control/bidi characters non-operative."""
    return "".join(
        char if (not unicodedata.category(char).startswith("C") and char not in "\u2028\u2029") or (
            multiline and char in "\n\t"
        ) else f"\\u{ord(char):04x}"
        for char in value
    )


def _text(value: str) -> str:
    # Entities are parsed as text, not as new Markdown/HTML delimiters. Newlines
    # become visible escapes so a cell cannot inject a row, heading or fence.
    return "".join(
        f"&#{ord(char)};" if char in "&<>\\`|[]*_!#~" else char
        for char in _visible_text(value)
    )


def _sql_block(sql: str) -> str:
    # A fence longer than every run in the payload cannot be closed by SQL.
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", sql)), default=0))
    return f"{fence}sql\n{_visible_text(sql, multiline=True)}\n{fence}"


def _cell(value) -> str:
    if value is None:
        return "NULL"
    # Quoted strings distinguish the string "NULL" from a database NULL, and
    # retain exact decimal/large-integer text without rounding or aggregation.
    return _text(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _render_result(report: dict) -> list[str]:
    result = report["result"]
    columns, rows = result["columns"], result["rows"]
    if (
        not isinstance(columns, list) or not columns or not isinstance(rows, list)
        or result.get("row_count") != len(rows)
        or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
        or any(not isinstance(column, dict) or not isinstance(column.get("name"), str)
               or not isinstance(column.get("type"), str) for column in columns)
        or type(result.get("truncated")) is not bool
        or report.get("execution_status") != (
            "truncated" if result["truncated"] else "completed"
        )
    ):
        raise ValueError("incomplete query result")

    lines = [f"返回 {len(rows)} 行。", ""]
    headers = [
        _text(f"{index}. {column['name']} ({column['type']})")
        for index, column in enumerate(columns, 1)
    ]
    lines.extend([
        "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
    ])
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    lines.append("")
    if result["truncated"]:
        reason = {"row_limit": "行数上限", "byte_limit": "结果字节上限"}.get(
            result.get("truncation_reason"), "结果预算",
        )
        lines.append(
            f"仅返回部分结果（{reason}）；不能据此推断总行数或全量统计。"
            "已停止读取并清理连接，服务器执行状态未确认。"
        )
    else:
        lines.append("当前 SQL 结果已完整返回；结论仅限 SQL 的 WHERE/LIMIT 范围。")
        if not rows:
            lines.append("本次查询返回空集，不表示工具失败。")

    if report.get("session_time_zone") == "+00:00":
        lines.append(
            "时间说明：DATETIME 不自带时区；本次会话时区为 +00:00，TIMESTAMP 按 UTC 返回。"
        )
    else:
        lines.append("时间说明：DATETIME 不自带时区；当前报告未确认会话时区。")
    return lines


def _render_query(execution: QueryExecution, index: int) -> str:
    report = execution.report
    lines = [f"### 查询 {index}", "", "本次提交的 SQL：", "", _sql_block(execution.sql), ""]
    if report.get("status") == "ok" and report.get("decision") == "ALLOW" and isinstance(
        report.get("result"), dict,
    ):
        try:
            lines.extend(_render_result(report))
        except (KeyError, TypeError, ValueError, OverflowError):
            lines.append("查询报告不完整，未取得可展示的可信结果。")
    elif report.get("status") == "rejected" and report.get("execution_status") == "not_started":
        decision = report.get("decision")
        label = decision if decision in {"BLOCK", "REVIEW", "UNKNOWN"} else "UNKNOWN"
        lines.append(f"查询被拒绝（{label}），业务 SQL 未执行。")
        for finding in report.get("findings", []):
            if isinstance(finding, dict) and isinstance(finding.get("message"), str):
                lines.append("原因：" + _text(finding["message"]))
    else:
        lines.append("未取得可确认的查询结果。")
        lines.append(
            "业务 SQL 未执行。" if report.get("execution_status") == "not_started"
            else "执行状态未知，不能确认是否完成。"
        )
        error = report.get("error")
        if isinstance(error, dict):
            for key, label in (("code", "错误码"), ("message", "错误")):
                if isinstance(error.get(key), str):
                    lines.append(label + "：" + _text(error[key]))
    for item in report.get("business_knowledge", []):
        lines.append("业务知识引用：" + _text(item["title"]) + "；ID：" + _text(item["id"])
                     + "；来源：" + _text(item["source"])
                     + "；版本：" + _text(item["source_version"])
                     + "；摘要：" + _text(item["digest"]) + "。引用不是执行授权。")
    return "\n".join(lines)


def render_queries(queries: list[QueryExecution], *, missing_reports: int = 0) -> str:
    """Rebuild the entire query answer without taking any free model text."""
    if not queries and not missing_reports:
        return "查询工具未取得可确认的执行报告，未生成查数结果。"
    parts = [
        _render_query(execution, index) for index, execution in enumerate(queries, 1)
    ]
    if missing_reports:
        parts.append(
            f"有 {missing_reports} 次查询工具请求未取得可确认的执行报告；"
            "这些请求未取得可确认结果，执行状态无法确认。"
        )
    return "\n\n".join(parts)
