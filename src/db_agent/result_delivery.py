"""Deterministic analysis and portable reports over one saved query, without I/O."""

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from db_agent.presentation import _visible_text

NUMERIC_TYPES = frozenset({
    "tinyint", "smallint", "mediumint", "int", "bigint", "decimal", "float", "double", "year",
})
TIME_TYPES = frozenset({"date", "datetime", "timestamp", "year"})
MAX_GROUPS = 100
SCOPE_NOTE = (
    "分析仅针对本次已返回的结果行，仍受原 SQL 的 WHERE / LIMIT 等范围限制；"
    "分组求和不会识别或消除 SQL 关联重复，不代表全库统计。"
)


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: int = Field(strict=True, ge=0, le=255)
    measure: int = Field(strict=True, ge=0, le=255)
    kind: Literal["comparison", "trend"] = "comparison"


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["json", "html"]
    analysis: AnalysisInput | None = None


def validate_result(report: dict) -> dict:
    """Reject contradictory/incomplete persisted evidence, including old snapshots."""
    data = report.get("result")
    if (
        report.get("status") != "ok" or report.get("decision") != "ALLOW"
        or report.get("error") is not None or not isinstance(data, dict)
    ):
        raise ValueError("没有可交付的查询结果。")
    columns, rows = data.get("columns"), data.get("rows")
    truncated = data.get("truncated")
    if (
        type(truncated) is not bool
        or report.get("execution_status") != ("truncated" if truncated else "completed")
        or data.get("server_statement_status") != ("unknown" if truncated else "completed")
        or (truncated and data.get("truncation_reason") not in {"row_limit", "byte_limit"})
        or (not truncated and data.get("truncation_reason") is not None)
        or not isinstance(columns, list) or not 1 <= len(columns) <= 256
        or any(not isinstance(c, dict) or not isinstance(c.get("name"), str)
               or not isinstance(c.get("type"), str) for c in columns)
        or not isinstance(rows, list) or len(rows) > 1000
        or type(data.get("row_count")) is not int or data["row_count"] != len(rows)
        or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
    ):
        raise ValueError("结果证据不完整，无法生成分析或导出。")
    for row in rows:
        for value in row:
            if value is not None and (
                type(value) not in {str, int, float, bool}
                or (type(value) is float and not math.isfinite(value))
                or (type(value) is int and abs(value) > 2**53 - 1)
            ):
                raise ValueError("结果包含不支持或精度不明确的值。")
    return data


def number(value) -> Decimal:
    if type(value) not in {str, int, float} or not re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", str(value),
    ):
        raise ValueError("所选指标含非数值，请选择数值类型的列。")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("所选指标含非数值，请选择数值类型的列。") from None
    # MySQL DECIMAL has at most 65 digits; FLOAT/DOUBLE may use wider exponents.
    if not result.is_finite() or len(result.as_tuple().digits) > 100 or abs(
        result.as_tuple().exponent
    ) > 400:
        raise ValueError("数值超出分析支持范围。")
    return result


def display(value) -> str:
    # Quote strings so NULL and the string "NULL" (or empty string) remain distinct.
    return "NULL" if value is None else _visible_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
    )


def analyze_result(data: dict, selection: AnalysisInput) -> dict:
    columns, rows = data["columns"], data["rows"]
    d, m = selection.dimension, selection.measure
    if max(d, m) >= len(columns) or d == m:
        raise ValueError("请选择不同且存在的维度列与指标列。")
    if columns[m]["type"].lower() not in NUMERIC_TYPES:
        raise ValueError("指标必须是数值类型；不会把文本数字猜测为金额。")
    if selection.kind == "trend" and columns[d]["type"].lower() not in TIME_TYPES:
        raise ValueError("趋势需要 date、datetime、timestamp 或 year 时间列。")
    groups = {}
    # Enough for supported exponents, significant digits and bounded row counts.
    with localcontext() as context:
        context.prec = 1000
        for row in rows:
            dimension, value = row[d], row[m]
            key = (type(dimension).__name__, dimension)
            if selection.kind == "trend":
                try:
                    kind = columns[d]["type"].lower()
                    if kind == "year":
                        if type(dimension) is not int or not 1 <= dimension <= 9999:
                            raise ValueError()
                    elif kind == "date":
                        if date.fromisoformat(dimension).isoformat() != dimension:
                            raise ValueError()
                    else:
                        parsed = datetime.fromisoformat(dimension)
                        if parsed.tzinfo is not None or parsed.isoformat() != dimension:
                            raise ValueError()
                except (TypeError, ValueError):
                    raise ValueError(
                        "趋势时间含 NULL 或无效时间，请先用 SQL 明确时间范围。"
                    ) from None
            if key not in groups:
                if len(groups) >= MAX_GROUPS:
                    raise ValueError("超过 100 个分组，请先通过受控查询按业务维度汇总。")
                groups[key] = {"dimension": dimension, "label": display(dimension),
                               "row_count": 0, "non_null_count": 0, "total": None}
            group = groups[key]
            group["row_count"] += 1
            if value is not None:
                numeric = number(value)
                group["non_null_count"] += 1
                group["total"] = numeric if group["total"] is None else group["total"] + numeric
        points = list(groups.values())
        if selection.kind == "trend":
            points.sort(key=lambda p: p["dimension"])
        totals = [p["total"] for p in points if p["total"] is not None]
        lower, upper = min([Decimal(0), *totals]), max([Decimal(0), *totals])
        span = upper - lower or Decimal(1)
        zero = float(-lower / span)
        first, last = (points[0]["total"], points[-1]["total"]) if points else (None, None)
        difference = last - first if first is not None and last is not None else None
        for point in points:
            total = point.pop("total")
            point["sum"] = format(total, "f") if total is not None else None
            point["position"] = float((total - lower) / span) if total is not None else None
        return {
            "selection": selection.model_dump(),
            "dimension_label": _visible_text(f"{d + 1}. {columns[d]['name']}"),
            "measure_label": _visible_text(f"{m + 1}. {columns[m]['name']}"),
            "aggregation": "sum", "points": points, "zero_position": zero,
            "minimum": format(min(totals), "f") if totals else None,
            "maximum": format(max(totals), "f") if totals else None,
            "first_to_last_difference": (
                format(difference, "f") if selection.kind == "trend" and difference is not None
                else None
            ),
            "notes": [
                SCOPE_NOTE,
                "同维度的返回行按所选指标求和；NULL 不参与求和，全 NULL 分组保留 NULL。"
                "金额及整数按十进制精确计算；浮点列只按已返回值计算，不能恢复数据库浮点精度。",
                "趋势按时间值升序，等距显示已返回的时间点；不补齐缺失日期、不推断增长率。"
                if selection.kind == "trend" else "对比按结果中维度首次出现顺序，不自动排名。",
                "图形坐标为近似比例；精确数值以标签及表格为准。",
                "按已返回值精确匹配分组，不模拟 MySQL 排序规则；SQL 已聚合的值也只做求和，"
                "不会将平均值的和解释为总体均值。",
            ],
        }


def chart_svg(analysis: dict) -> str:
    points = analysis["points"]
    trend = analysis["selection"]["kind"] == "trend"
    width = max(640, len(points) * 100) if trend else 760
    height = 320 if trend else max(100, len(points) * 42 + 30)
    zero = analysis["zero_position"]
    items = []
    previous = None
    for index, point in enumerate(points):
        position = point["position"]
        full_label = escape(point["label"])
        label = escape(point["label"][:20] + ("…" if len(point["label"]) > 20 else ""))
        value = escape(point["sum"] if point["sum"] is not None else "NULL")
        if trend:
            x = 100 + index * (width - 200) / max(1, len(points) - 1)
            y = 245 - position * 205 if position is not None else None
            if previous and y is not None:
                items.append(f'<path d="M {previous[0]} {previous[1]} L {x} {y}" '
                             'stroke="currentColor" stroke-width="2" fill="none"/>')
            if y is not None:
                items.append(f'<circle cx="{x}" cy="{y}" r="4"><title>{full_label}: {value}'
                             '</title></circle>')
            else:
                items.append(f'<text x="{x}" y="140" text-anchor="middle">NULL</text>')
            items.append(f'<text x="{x}" y="280" text-anchor="middle" '
                         f'transform="rotate(-25 {x} 280)">{label}</text>')
            previous = (x, y) if y is not None else None
        else:
            y, baseline = 25 + index * 42, 240 + zero * 300
            items.append(f'<text x="4" y="{y}" >{label}</text>')
            items.append(f'<path d="M {baseline} {y - 18} v 26" stroke="#9aa4b2"/>')
            if position is not None:
                x = 240 + position * 300
                items.append(f'<rect x="{min(x, baseline)}" y="{y - 16}" '
                             f'width="{abs(x - baseline)}" height="22" rx="3">'
                             f'<title>{full_label}: {value}</title></rect>')
            short_value = value if len(value) <= 23 else value[:20] + "…"
            items.append(f'<text x="565" y="{y}"><title>{value}</title>{short_value}</text>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="结果图表" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'style="color:#2563eb;fill:currentColor;font:12px monospace">'
        '<title>已返回结果的分组求和图；精确数值见明细表</title>' + "".join(items) + '</svg>'
    )


def html_report(snapshot: dict) -> str:
    data, analysis = snapshot["report"]["result"], snapshot["analysis"]
    def e(value):
        return escape(_visible_text(
            "NULL / 不适用" if value is None else str(value), multiline=True,
        ))
    headers = ''.join(f'<th>{i + 1}. {e(c["name"])} ({e(c["type"])})</th>'
                      for i, c in enumerate(data["columns"]))
    rows = ''.join('<tr>' + ''.join(f'<td>{e(display(v))}</td>' for v in row) + '</tr>'
                   for row in data["rows"])
    notes = ''.join(f'<li>{e(note)}</li>' for note in snapshot["notes"])
    section = ''
    if analysis:
        points = ''.join(
            f'<tr><td>{e(p["label"])}</td><td>{e(p["sum"] if p["sum"] is not None else "NULL")}'
            f'</td><td>{p["row_count"]}</td><td>{p["non_null_count"]}</td></tr>'
            for p in analysis["points"]
        )
        section = (
            f'<h2>{e(analysis["dimension_label"])} / {e(analysis["measure_label"])} · 求和</h2>'
            + '<ul>' + ''.join(f'<li>{e(n)}</li>' for n in analysis["notes"]) + '</ul>'
            + '<div class="scroll">' + chart_svg(analysis) + '</div>'
            + '<table><thead><tr><th>维度</th><th>指标合计</th><th>返回行</th><th>非 NULL 行</th>'
            + '</tr></thead><tbody>' + points + '</tbody></table>'
            + f'<p>分组合计最小值：{e(analysis["minimum"])}；最大值：{e(analysis["maximum"])}；'
            + f'趋势首末差额：{e(analysis["first_to_last_difference"])}</p>'
        )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
        '<title>DB Agent 查询结果报告</title><style>'
        'body{font:15px/1.7 system-ui,sans-serif;color:#202936;margin:32px auto;'
        'padding:0 24px;max-width:1100px}h1{font-size:28px}h2{font-size:19px}'
        'pre,td,th{white-space:pre-wrap;overflow-wrap:anywhere}pre{background:#f6f7f9;padding:16px}'
        'table{border-collapse:collapse;width:100%;font:13px/1.6 monospace}'
        'td,th{border:1px solid #e5e8ed;padding:8px;text-align:left}.scroll{overflow:auto}'
        'li{margin:6px 0}@media print{body{margin:0}.scroll{overflow:visible}svg{max-width:100%;'
        'height:auto}tr{break-inside:avoid}}</style><body><h1>查询结果报告</h1>'
        f'<p>保存时间：{e(snapshot["finished_at"])} · 结果 ID：{e(snapshot["result_id"])}</p>'
        f'<p>会话：{e(snapshot["conversation_id"])} · 运行：{e(snapshot["run_id"])}</p>'
        f'<h2>本轮请求</h2><pre>{e(snapshot["prompt"])}</pre>'
        f'<p>决策 {e(snapshot["report"]["decision"])} · 执行 '
        f'{e(snapshot["report"]["execution_status"])} · 返回 {data["row_count"]} 行</p>'
        f'<ul>{notes}</ul><h2>实际执行 SQL</h2><pre>{e(snapshot["sql"])}</pre>'
        + section + '<h2>原始结果（字符串带引号，NULL 表示空值）</h2>'
        + f'<div class="scroll"><table><thead><tr>{headers}</tr></thead><tbody>{rows}'
        + '</tbody></table></div>'
        + ('<p>本次查询返回空集。</p>' if not data["rows"] else '')
        + '</body></html>'
    )
