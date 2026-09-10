"""Bounded collection of an already executed MySQL tuple cursor, without draining it."""

import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from pymysql.constants import FIELD_TYPE

if TYPE_CHECKING:
    from db_agent.config import QuerySettings

_MAX_COLUMN_NAME_LENGTH = 256
_MAX_EXACT_JSON_INTEGER = 2**53 - 1
_COLUMN_TYPES = {
    FIELD_TYPE.DECIMAL: "decimal",
    FIELD_TYPE.TINY: "tinyint",
    FIELD_TYPE.SHORT: "smallint",
    FIELD_TYPE.LONG: "int",
    FIELD_TYPE.FLOAT: "float",
    FIELD_TYPE.DOUBLE: "double",
    FIELD_TYPE.NULL: "null",
    FIELD_TYPE.TIMESTAMP: "timestamp",
    FIELD_TYPE.LONGLONG: "bigint",
    FIELD_TYPE.INT24: "mediumint",
    FIELD_TYPE.DATE: "date",
    FIELD_TYPE.TIME: "time",
    FIELD_TYPE.DATETIME: "datetime",
    FIELD_TYPE.YEAR: "year",
    FIELD_TYPE.NEWDATE: "date",
    FIELD_TYPE.VARCHAR: "varchar",
    FIELD_TYPE.NEWDECIMAL: "decimal",
    FIELD_TYPE.ENUM: "enum",
    FIELD_TYPE.VAR_STRING: "varchar",
    FIELD_TYPE.STRING: "char",
}


class ResultError(ValueError):
    """A fixed, value-free result error for adaptation by the database connector."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _columns(description: object, max_columns: int) -> list[dict]:
    if not isinstance(description, (list, tuple)) or not description:
        raise ResultError("INVALID_RESULT", "查询未返回有效的列描述")
    if len(description) > max_columns:
        raise ResultError("RESULT_LIMIT", "查询列数超过上限")
    columns = []
    for field in description:
        if not isinstance(field, (list, tuple)) or len(field) != 7:
            raise ResultError("INVALID_RESULT", "查询列描述格式不受支持")
        name, code = field[:2]
        if not isinstance(name, str) or len(name) > _MAX_COLUMN_NAME_LENGTH:
            raise ResultError("RESULT_LIMIT", "查询列名无效或超过长度上限")
        if type(code) is not int or code not in _COLUMN_TYPES:
            # BLOB/TEXT, JSON, BIT, SET and geometry may be binary or very large.
            # Reject them before fetching a row; never decode bytes speculatively.
            raise ResultError("UNSUPPORTED_RESULT_TYPE", "查询包含当前不支持的结果列类型")
        columns.append({"name": name, "type": _COLUMN_TYPES[code]})
    return columns


def _duration(value: timedelta) -> str:
    microseconds = (value.days * 86400 + value.seconds) * 1000000 + value.microseconds
    sign = "-" if microseconds < 0 else ""
    seconds, micros = divmod(abs(microseconds), 1000000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    fraction = f".{micros:06d}" if micros else ""
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}{fraction}"


def _value(value: object):
    kind = type(value)
    if value is None or kind in (bool, str):
        return value
    if kind is int:
        return str(value) if abs(value) > _MAX_EXACT_JSON_INTEGER else value
    if kind is float and math.isfinite(value):
        return value
    if kind is Decimal and value.is_finite():
        return str(value)
    if kind in (datetime, date, time):
        return value.isoformat()
    if kind is timedelta:
        return _duration(value)
    raise ResultError("UNSUPPORTED_RESULT_VALUE", "查询包含非有限数值或不支持的结果值类型")


def _result(columns: list[dict], rows: list[list], reason: str | None = None) -> dict:
    result = {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": reason is not None,
        "truncation_reason": reason,
        "result_bytes": 0,
        "server_statement_status": "unknown" if reason else "completed",
    }
    # Count the entire returned object using the same JSON options as tool output.
    # Updating result_bytes can change its own digit count; converge to that size.
    while True:
        try:
            size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            raise ResultError("INVALID_RESULT", "查询结果无法编码为受支持的 JSON") from None
        if size == result["result_bytes"]:
            return result
        result["result_bytes"] = size


def _truncated(columns: list[dict], rows: list[list], reason: str, max_bytes: int) -> dict:
    while True:
        result = _result(columns, rows, reason)
        if result["result_bytes"] <= max_bytes:
            return result
        if not rows:
            raise ResultError("RESULT_LIMIT", "查询列描述和结果外层字段超过字节上限")
        # A truncation reason has its own bytes. Remove a row if that envelope
        # would otherwise exceed the budget, and report the byte limit honestly.
        rows.pop()
        reason = "byte_limit"


async def read_query_result(cursor, limits: "QuerySettings") -> dict:
    """Read at most max_rows + 1 rows; the caller owns deadlines and socket cleanup.

    The cursor must be an executed SSCursor returning tuples, never a DictCursor.
    This function does not close the cursor: SSCursor.close() drains unread rows.
    Only an observed EOF permits the completed status; truncation proves no cancel.
    """
    columns = _columns(cursor.description, limits.max_columns)
    rows: list[list] = []
    result = _result(columns, rows)
    if result["result_bytes"] > limits.max_result_bytes:
        raise ResultError("RESULT_LIMIT", "查询列描述和结果外层字段超过字节上限")
    while True:
        raw = await cursor.fetchone()
        if raw is None:
            return result
        if len(rows) >= limits.max_rows:
            return _truncated(columns, rows, "row_limit", limits.max_result_bytes)
        if not isinstance(raw, (list, tuple)) or len(raw) != len(columns):
            raise ResultError("INVALID_RESULT", "查询行与列描述不匹配")
        rows.append([_value(value) for value in raw])
        result = _result(columns, rows)
        if result["result_bytes"] > limits.max_result_bytes:
            rows.pop()
            return _truncated(columns, rows, "byte_limit", limits.max_result_bytes)
