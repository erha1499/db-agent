"""Bounded collection from an executed psycopg asynchronous server cursor."""

from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from db_agent.results import ResultError, _result, _truncated, _value

if TYPE_CHECKING:
    from db_agent.config import QuerySettings

# PostgreSQL built-in type OIDs, not display names or user-defined type aliases.
# Domains must additionally be refused while checking the underlying schema:
# PostgreSQL can expose a domain result using its base type OID on the wire.
_TYPES = {
    16: ("boolean", bool),
    20: ("bigint", int),
    21: ("smallint", int),
    23: ("int", int),
    25: ("text", str),
    700: ("float", float),
    701: ("double", float),
    1042: ("char", str),
    1043: ("varchar", str),
    1082: ("date", date),
    1083: ("time", time),
    1114: ("timestamp", datetime),
    1184: ("timestamptz", datetime),
    1700: ("decimal", Decimal),
}


def _columns(description: object, max_columns: int) -> tuple[list[dict], list[int]]:
    if not isinstance(description, (list, tuple)) or not description:
        raise ResultError("INVALID_RESULT", "查询未返回有效的列描述")
    if len(description) > max_columns:
        raise ResultError("RESULT_LIMIT", "查询列数超过上限")
    columns, codes = [], []
    for field in description:
        # psycopg.Column is an object with named attributes, not a tuple.
        name, code = getattr(field, "name", None), getattr(field, "type_code", None)
        if not isinstance(name, str) or len(name) > 256:
            raise ResultError("RESULT_LIMIT", "查询列名无效或超过长度上限")
        if type(code) is not int or code not in _TYPES:
            raise ResultError("UNSUPPORTED_RESULT_TYPE", "查询包含当前不支持的结果列类型")
        columns.append({"name": name, "type": _TYPES[code][0]})
        codes.append(code)
    return columns, codes


def _postgres_value(value: object, oid: int):
    if value is not None:
        if type(value) is not _TYPES[oid][1]:
            raise ResultError("UNSUPPORTED_RESULT_VALUE", "查询值与已验证的列类型不匹配")
        if oid in (1083, 1114, 1184):
            aware = value.utcoffset() is not None
            if aware != (oid == 1184):
                raise ResultError("UNSUPPORTED_RESULT_VALUE", "查询时间值的时区与列类型不匹配")
    return _value(value)


async def read_postgres_result(cursor, limits: "QuerySettings") -> dict:
    """Fetch one row at a time; connector owns deadlines and connection cleanup.

    The caller must provide an AsyncServerCursor using tuple_row. No close, drain,
    fetchall, or implicit retry occurs here. TEXT remains subject to the returned
    JSON budget, which is not a hard bound on one field's driver allocation.
    """
    columns, codes = _columns(cursor.description, limits.max_columns)
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
        rows.append([_postgres_value(value, oid) for value, oid in zip(raw, codes, strict=True)])
        result = _result(columns, rows)
        if result["result_bytes"] > limits.max_result_bytes:
            rows.pop()
            return _truncated(columns, rows, "byte_limit", limits.max_result_bytes)
