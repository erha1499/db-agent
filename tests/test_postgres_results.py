"""Constructed server-cursor/Column fixtures, not live PostgreSQL validation."""

import asyncio
import json
from collections import deque
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from db_agent.config import QuerySettings
from db_agent.postgres_results import read_postgres_result
from db_agent.results import ResultError


def column(name="id", code=23):
    return SimpleNamespace(name=name, type_code=code)


class Cursor:
    def __init__(self, rows=(), description=None):
        self.description = [column()] if description is None else description
        self.rows = deque(rows)
        self.reads = 0

    async def fetchone(self):
        self.reads += 1
        return self.rows.popleft() if self.rows else None

    async def close(self):
        pytest.fail("collector must not close a cursor")

    async def fetchall(self):
        pytest.fail("collector must not drain a cursor")

    async def fetchmany(self, *args):
        pytest.fail("collector must fetch one row at a time")


@pytest.fixture
def limits():
    return QuerySettings(
        _env_file=None, max_rows=100, max_result_bytes=32768, max_columns=64,
        execution_timeout_seconds=5, operation_timeout_seconds=15,
    )


def collect(cursor, limits):
    result = asyncio.run(read_postgres_result(cursor, limits))
    assert result["result_bytes"] == len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    assert result["result_bytes"] <= limits.max_result_bytes
    return result


def test_duplicate_columns_and_exact_precision(limits):
    cursor = Cursor(
        [(Decimal("99999999999999999999.123400"), 2**53, None)],
        [column("额", 1700), column("额", 20), column("空", 25)],
    )
    result = collect(cursor, limits)
    assert result["columns"] == [
        {"name": "额", "type": "decimal"}, {"name": "额", "type": "bigint"},
        {"name": "空", "type": "text"},
    ]
    assert result["rows"] == [["99999999999999999999.123400", "9007199254740992", None]]
    assert result["server_statement_status"] == "completed"
    assert cursor.reads == 2


@pytest.mark.parametrize(("oid", "value", "expected"), [
    (16, False, False), (16, True, True), (20, -(2**53), "-9007199254740992"),
    (20, 2**53 - 1, 2**53 - 1), (21, 0, 0), (23, -1, -1),
    (25, "中文\n\\\"", "中文\n\\\""), (1042, "a", "a"), (1043, "", ""),
    (700, 1.25, 1.25), (701, -2.5, -2.5),
    (1700, Decimal("-0.001200"), "-0.001200"),
    (1082, date(2026, 9, 11), "2026-09-11"),
    (1083, time(1, 2, 3, 4), "01:02:03.000004"),
    (1114, datetime(2026, 9, 11, 1, 2, 3), "2026-09-11T01:02:03"),
    (1184, datetime(2026, 9, 11, tzinfo=UTC), "2026-09-11T00:00:00+00:00"),
])
def test_builtin_value_and_temporal_contract(limits, oid, value, expected):
    result = collect(Cursor([(value,)], [column(code=oid)]), limits)
    assert result["rows"] == [[expected]]
    assert type(result["rows"][0][0]) is type(expected)


@pytest.mark.parametrize("oid", [
    16, 20, 21, 23, 25, 700, 701, 1042, 1043, 1082, 1083, 1114, 1184, 1700,
])
def test_null_keeps_declared_type(limits, oid):
    assert collect(Cursor([(None,)], [column(code=oid)]), limits)["rows"] == [[None]]


@pytest.mark.parametrize("oid", [17, 114, 3802, 1007, 1186, 1266, 1560, 2950, 99999, True, "23"])
def test_unknown_array_json_binary_enum_domain_types_stop_before_read(limits, oid):
    cursor = Cursor([(None,)], [column(code=oid)])
    with pytest.raises(ResultError, match="结果列类型") as caught:
        collect(cursor, limits)
    assert caught.value.code == "UNSUPPORTED_RESULT_TYPE"
    assert cursor.reads == 0


@pytest.mark.parametrize(("oid", "value"), [
    (1700, Decimal("NaN")), (1700, Decimal("Infinity")), (700, float("inf")),
    (701, float("nan")), (23, True), (16, 1), (1700, 1.25), (25, b"secret"),
    (1082, datetime(2026, 9, 11)), (1083, timedelta(seconds=3)),
    (1083, time(1, tzinfo=UTC)), (1114, datetime(2026, 9, 11, tzinfo=UTC)),
    (1184, datetime(2026, 9, 11)),
])
def test_nonfinite_type_or_timezone_mismatch_is_value_free_error(limits, oid, value):
    with pytest.raises(ResultError) as caught:
        collect(Cursor([(value,)], [column(code=oid)]), limits)
    assert caught.value.code == "UNSUPPORTED_RESULT_VALUE"
    assert "secret" not in str(caught.value)


def test_row_limit_reads_only_one_extra_and_never_claims_eof(limits):
    limits = limits.model_copy(update={"max_rows": 2})
    cursor = Cursor([(1,), (2,), (3,), (4,)])
    result = collect(cursor, limits)
    assert result["rows"] == [[1], [2]]
    assert result["truncated"] is True and result["truncation_reason"] == "row_limit"
    assert result["server_statement_status"] == "unknown"
    assert cursor.reads == 3 and len(cursor.rows) == 1


@pytest.mark.parametrize("rows", [[], [(1,), (2,)]])
def test_empty_and_exact_limit_are_complete_only_after_eof(limits, rows):
    cursor = Cursor(rows)
    result = collect(cursor, limits.model_copy(update={"max_rows": 2}))
    assert not result["truncated"] and result["server_statement_status"] == "completed"
    assert cursor.reads == len(rows) + 1


def test_text_byte_limit_stops_without_reading_next_row(limits):
    cursor = Cursor([("small",), ("中" * 600,), ("never-read",)], [column(code=25)])
    result = collect(cursor, limits.model_copy(update={"max_result_bytes": 1024}))
    assert result["rows"] == [["small"]]
    assert result["truncation_reason"] == "byte_limit"
    assert result["server_statement_status"] == "unknown"
    assert cursor.reads == 2 and len(cursor.rows) == 1


@pytest.mark.parametrize("description", [
    None, [], [()], [column(name=None)], [column(name="x" * 257)],
])
def test_invalid_descriptions_are_rejected_before_read(limits, description):
    cursor = Cursor()
    cursor.description = description
    with pytest.raises(ResultError):
        collect(cursor, limits)
    assert cursor.reads == 0


def test_column_count_and_envelope_bytes_refuse_before_fetch(limits):
    for description in ([column()] * 65, [column(name="中" * 256)] * 2):
        cursor = Cursor(description=description)
        with pytest.raises(ResultError) as caught:
            collect(cursor, limits.model_copy(update={"max_result_bytes": 1024}))
        assert caught.value.code == "RESULT_LIMIT"
        assert cursor.reads == 0


@pytest.mark.parametrize("row", [{"id": 1}, (1, 2), 1])
def test_rows_must_match_positional_description(limits, row):
    with pytest.raises(ResultError) as caught:
        collect(Cursor([row]), limits)
    assert caught.value.code == "INVALID_RESULT"


@pytest.mark.parametrize("error", [asyncio.CancelledError, ConnectionError, TimeoutError])
def test_cancellation_and_fetch_failure_escape_without_drain(limits, error):
    class Broken(Cursor):
        async def fetchone(self):
            raise error()

    with pytest.raises(error):
        collect(Broken(), limits)
