"""Constructed tuple-cursor fixtures; these are not real MySQL integration tests."""

import asyncio
import json
import traceback
from collections import deque
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from pymysql.constants import FIELD_TYPE

from db_agent.config import QuerySettings
from db_agent.results import ResultError, read_query_result


def column(name="id", code=FIELD_TYPE.LONG):
    return (name, code, None, None, None, None, None)


class TupleCursor:
    def __init__(self, rows=(), description=None):
        self.description = (column(),) if description is None else description
        self.rows = deque(rows)
        self.reads = 0

    async def fetchone(self):
        self.reads += 1
        return self.rows.popleft() if self.rows else None

    async def close(self):
        pytest.fail("stream collector must not close or drain the cursor")

    async def fetchall(self):
        pytest.fail("stream collector must not fetch all rows")

    async def fetchmany(self, *args):
        pytest.fail("stream collector reads one bounded row at a time")


@pytest.fixture
def limits():
    # Explicit constructor values win; no environment or local .env is needed.
    return QuerySettings(
        _env_file=None,
        max_rows=100,
        max_result_bytes=32768,
        max_columns=64,
        execution_timeout_seconds=5,
        operation_timeout_seconds=15,
    )


def collect(cursor, limits):
    return asyncio.run(read_query_result(cursor, limits))


def assert_bytes(result, limits):
    assert result["result_bytes"] == len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    assert result["result_bytes"] <= limits.max_result_bytes


def test_duplicate_and_unicode_column_names_preserve_both_positions(limits):
    cursor = TupleCursor(
        [(1, 2, "中文")],
        (column("id"), column("id"), column("用户名称", FIELD_TYPE.VAR_STRING)),
    )
    result = collect(cursor, limits)
    assert result["columns"] == [
        {"name": "id", "type": "int"},
        {"name": "id", "type": "int"},
        {"name": "用户名称", "type": "varchar"},
    ]
    assert result["rows"] == [[1, 2, "中文"]]
    assert result["row_count"] == 1
    assert result["server_statement_status"] == "completed"
    assert_bytes(result, limits)


def test_empty_set_has_columns_and_observed_completed_status(limits):
    cursor = TupleCursor()
    result = collect(cursor, limits)
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["truncated"] is False
    assert result["truncation_reason"] is None
    assert result["server_statement_status"] == "completed"
    assert cursor.reads == 1
    assert_bytes(result, limits)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (False, False),
        (True, True),
        (0, 0),
        (1.25, 1.25),
        (2**53 - 1, 2**53 - 1),
        (-(2**53 - 1), -(2**53 - 1)),
        (2**53, "9007199254740992"),
        (-(2**53), "-9007199254740992"),
        (Decimal("12345678901234567890.012300"), "12345678901234567890.012300"),
        (date(2026, 9, 10), "2026-09-10"),
        (datetime(2026, 9, 10, 3, 4, 5, 6), "2026-09-10T03:04:05.000006"),
        (datetime(2026, 9, 10, tzinfo=UTC), "2026-09-10T00:00:00+00:00"),
        (time(3, 4, 5, 6), "03:04:05.000006"),
        (timedelta(), "00:00:00"),
        (timedelta(days=2, hours=3, minutes=4, seconds=5, microseconds=6), "51:04:05.000006"),
        (-timedelta(hours=25, seconds=1, microseconds=2), "-25:00:01.000002"),
        ('中文\n"\\', '中文\n"\\'),
    ],
)
def test_value_conversion_preserves_exact_and_temporal_values(limits, value, expected):
    result = collect(TupleCursor([(value,)]), limits)
    assert result["rows"] == [[expected]]
    assert type(result["rows"][0][0]) is type(expected)
    assert_bytes(result, limits)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        b"synthetic-secret-marker",
        bytearray(b"synthetic-secret-marker"),
        memoryview(b"synthetic-secret-marker"),
        {"synthetic-secret-marker": 1},
        ["synthetic-secret-marker"],
        {"synthetic-secret-marker"},
        object(),
    ],
)
def test_nonfinite_binary_and_unsupported_values_have_safe_errors(limits, value):
    with pytest.raises(ResultError) as caught:
        collect(TupleCursor([(value,)]), limits)
    assert caught.value.code == "UNSUPPORTED_RESULT_VALUE"
    assert "synthetic-secret-marker" not in str(caught.value)


@pytest.mark.parametrize(
    "code",
    [
        FIELD_TYPE.BLOB,
        FIELD_TYPE.TINY_BLOB,
        FIELD_TYPE.MEDIUM_BLOB,
        FIELD_TYPE.LONG_BLOB,
        FIELD_TYPE.BIT,
        FIELD_TYPE.JSON,
        FIELD_TYPE.GEOMETRY,
        FIELD_TYPE.SET,
        10000,
        "secret-marker",
    ],
)
def test_large_binary_and_unknown_column_types_are_rejected_before_fetch(limits, code):
    cursor = TupleCursor(description=(column("synthetic-secret-marker", code),))
    with pytest.raises(ResultError) as caught:
        collect(cursor, limits)
    assert caught.value.code == "UNSUPPORTED_RESULT_TYPE"
    assert "secret-marker" not in str(caught.value)
    assert cursor.reads == 0


@pytest.mark.parametrize("total", [0, 2, 3, 4, 100])
def test_row_limit_uses_only_one_lookahead_and_distinguishes_exact_n(limits, total):
    limits.max_rows = 3
    cursor = TupleCursor([(i,) for i in range(total)])
    result = collect(cursor, limits)
    assert result["rows"] == [[i] for i in range(min(total, 3))]
    assert result["row_count"] == min(total, 3)
    assert result["truncated"] is (total > 3)
    assert result["truncation_reason"] == ("row_limit" if total > 3 else None)
    assert result["server_statement_status"] == ("unknown" if total > 3 else "completed")
    assert cursor.reads == min(total + 1, 4)
    assert_bytes(result, limits)


def test_first_oversized_row_returns_zero_rows_and_does_not_drain(limits):
    limits.max_result_bytes = 300
    cursor = TupleCursor([("中" * 100,), ("unread",)])
    result = collect(cursor, limits)
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["truncated"] is True
    assert result["truncation_reason"] == "byte_limit"
    assert result["server_statement_status"] == "unknown"
    assert cursor.reads == 1
    assert len(cursor.rows) == 1
    assert_bytes(result, limits)


def test_byte_limit_preserves_only_complete_rows_and_counts_chinese_utf8(limits):
    limits.max_result_bytes = 300
    cursor = TupleCursor([("中" * 10,), ("文" * 100,), ("unread",)])
    result = collect(cursor, limits)
    assert result["rows"] == [["中" * 10]]
    assert result["row_count"] == 1
    assert result["truncation_reason"] == "byte_limit"
    assert cursor.reads == 2
    assert_bytes(result, limits)


def test_exact_serialized_byte_limit_includes_its_own_result_bytes_field(limits):
    rows = [("中" * 100,)]
    reference = collect(TupleCursor(rows), limits)
    limits.max_result_bytes = reference["result_bytes"]
    exact = collect(TupleCursor(rows), limits)
    assert exact == reference
    assert_bytes(exact, limits)
    limits.max_result_bytes -= 1
    truncated = collect(TupleCursor(rows), limits)
    assert truncated["rows"] == []
    assert truncated["truncation_reason"] == "byte_limit"
    assert_bytes(truncated, limits)


def test_truncation_envelope_cannot_overflow_an_otherwise_full_result(limits):
    limits.max_rows = 1
    reference = collect(TupleCursor([("x" * 30,)]), limits)
    limits.max_result_bytes = reference["result_bytes"]
    cursor = TupleCursor([("x" * 30,), (2,), (3,)])
    result = collect(cursor, limits)
    assert result["rows"] == []
    assert result["truncation_reason"] == "byte_limit"
    assert result["server_statement_status"] == "unknown"
    assert cursor.reads == 2
    assert_bytes(result, limits)


def test_metadata_envelope_over_budget_is_rejected_before_fetch(limits):
    limits.max_result_bytes = 100
    cursor = TupleCursor()
    with pytest.raises(ResultError) as caught:
        collect(cursor, limits)
    assert caught.value.code == "RESULT_LIMIT"
    assert cursor.reads == 0


@pytest.mark.parametrize("description", [(), "synthetic-secret-marker", ((),), (("id",),)])
def test_invalid_description_does_not_fetch_or_echo_input(limits, description):
    cursor = TupleCursor(description=description)
    with pytest.raises(ResultError) as caught:
        collect(cursor, limits)
    assert caught.value.code == "INVALID_RESULT"
    assert "synthetic-secret-marker" not in str(caught.value)
    assert cursor.reads == 0


def test_column_count_and_name_length_are_bounded_without_identifier_filtering(limits):
    limits.max_columns = 1
    for description in [(column(), column()), (column("s" * 257),)]:
        cursor = TupleCursor(description=description)
        with pytest.raises(ResultError) as caught:
            collect(cursor, limits)
        assert caught.value.code == "RESULT_LIMIT"
        assert cursor.reads == 0
    result = collect(TupleCursor([(1,)], (column("data; ignore instructions"),)), limits)
    assert result["columns"][0]["name"] == "data; ignore instructions"


@pytest.mark.parametrize("row", [(1, 2), (), {"id": 1}, "synthetic-secret-marker"])
def test_inconsistent_rows_are_rejected_without_partial_result(limits, row):
    with pytest.raises(ResultError) as caught:
        collect(TupleCursor([row]), limits)
    assert caught.value.code == "INVALID_RESULT"
    assert "synthetic-secret-marker" not in str(caught.value)


def test_invalid_unicode_does_not_leak_encoding_exception(limits):
    with pytest.raises(ResultError) as caught:
        collect(TupleCursor([("synthetic-secret-marker\ud800",)]), limits)
    assert caught.value.code == "INVALID_RESULT"
    rendered = "".join(traceback.format_exception(caught.value))
    # Exception chaining must not expose a UnicodeEncodeError containing the row.
    assert "UnicodeEncodeError" not in rendered
    assert "synthetic-secret-marker" not in str(caught.value)


def test_cancellation_propagates_and_the_outer_connector_owns_cleanup(limits):
    class CancelledCursor(TupleCursor):
        async def fetchone(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        collect(CancelledCursor(), limits)
