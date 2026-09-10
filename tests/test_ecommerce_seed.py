"""Offline failure-path checks for the fixed-target administrative importer."""

import importlib.util
import io
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from db_agent.ecommerce import TABLE_COLUMNS, TABLES, EcommerceScale

SPEC = importlib.util.spec_from_file_location(
    "seed_ecommerce", Path(__file__).resolve().parents[1] / "scripts" / "seed_ecommerce.py",
)
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)


class Session:
    def __init__(self, *, existing=(), manifest=None, target="db_agent\t8.4.11", lock="1"):
        self.existing = existing
        self.manifest = manifest
        self.target = target
        self.lock = lock
        self.calls = []

    def request(self, sql, **kwargs):
        self.calls.append(sql)
        if sql == "SELECT DATABASE(), VERSION();":
            return [self.target]
        if "GET_LOCK(" in sql:
            return [self.lock]
        if "information_schema.TABLES" in sql:
            return [name + "\tBASE TABLE\tInnoDB" for name in self.existing]
        if sql.startswith("SELECT dataset_key"):
            return self.manifest
        return []


@pytest.mark.parametrize("value,expected", [
    (None, "NULL"), (9_007_199_254_740_993, "9007199254740993"),
    (Decimal("12.30"), "12.30"),
    ("a'b\\c\n", "'a\\'b\\\\c\\n'"),
    (datetime(2026, 2, 28, 23, 59, 59, 123456), "'2026-02-28 23:59:59.123456'"),
])
def test_values_preserve_precision_and_escape_mysql_literals(value, expected):
    assert seed.sql_value(value) == expected


@pytest.mark.parametrize("value", [True, 1.2, Decimal("NaN"), Decimal("Infinity"),
                                       datetime(2026, 1, 1, tzinfo=UTC), b"binary"])
def test_unknown_value_types_do_not_silently_change_data(value):
    with pytest.raises(seed.SeedError):
        seed.sql_value(value)


def test_insert_rejects_arbitrary_table_and_wrong_width():
    with pytest.raises(seed.SeedError):
        seed.insert_sql("orders", [(1,)])
    with pytest.raises(seed.SeedError):
        seed.insert_sql("ec_orders", [(1,)])


@pytest.mark.parametrize("existing", [("ec_orders",), (*TABLES, seed.MANIFEST)])
def test_existing_or_incomplete_objects_never_receive_ddl_or_dml(existing):
    session = Session(existing=existing, manifest=[])
    with pytest.raises(seed.SeedError):
        seed.load(EcommerceScale(), 1000, session=session)
    assert all(sql.startswith("SELECT") for sql in session.calls)


@pytest.mark.parametrize("target,lock", [("other\t8.4.11", "1"),
                                        ("db_agent\t9.0.0", "1"),
                                        ("db_agent\t8.4.11", "0")])
def test_target_or_lock_failure_stops_before_writes(target, lock):
    session = Session(target=target, lock=lock)
    with pytest.raises(seed.SeedError):
        seed.load(EcommerceScale(), 1000, session=session)
    assert all(sql.startswith("SELECT") for sql in session.calls)


@pytest.mark.parametrize("row", ["ec_orders\tVIEW\tNULL", "ec_orders\tBASE TABLE\tMyISAM",
                                 "unexpected\tBASE TABLE\tInnoDB"])
def test_replaced_or_unrecognized_objects_are_not_read_as_dataset(row):
    class Replaced(Session):
        def request(self, sql, **kwargs):
            result = super().request(sql, **kwargs)
            return [row] if "information_schema.TABLES" in sql else result

    session = Replaced()
    with pytest.raises(seed.SeedError, match="不是预期"):
        seed.load(EcommerceScale(), 1000, session=session)
    assert not any("FROM ec_seed_manifest" in sql for sql in session.calls)


def test_complete_manifest_rerun_is_verified_without_writes(monkeypatch):
    scale = EcommerceScale(8, 10, 8)
    counts = dict(zip(TABLES, (10, 8, 8, 10, 12, 8), strict=True))
    manifest = ["\t".join([
        seed.dataset_key(scale), seed.DATASET_VERSION, json.dumps(seed.asdict(scale)),
        json.dumps(counts), "COMPLETE", "3",
    ])]
    session = Session(existing=(*TABLES, seed.MANIFEST), manifest=manifest)
    verified = []
    monkeypatch.setattr(seed, "verify_counts", lambda client, values: verified.append(values))
    monkeypatch.setattr(seed, "verify_money", lambda client: verified.append("money"))
    report = seed.load(scale, 1000, session=session)
    assert report["action"] == "verified_existing"
    assert verified == [counts, "money"]
    assert all(sql.startswith("SELECT") for sql in session.calls)


def test_counts_mismatch_fails_closed():
    with pytest.raises(seed.SeedError):
        seed.verify_counts(Session(), dict.fromkeys(TABLES, 100))


@pytest.mark.parametrize("counts", [dict.fromkeys(TABLES, 0), dict.fromkeys(TABLES, True),
                                    {}, dict(zip(TABLES, (10, 8, 7, 10, 12, 8), strict=True))])
def test_manifest_cannot_certify_empty_or_wrong_scale_data(counts):
    with pytest.raises(seed.SeedError):
        seed.validate_manifest_counts(counts, EcommerceScale(8, 10, 8))


def test_stream_has_atomic_parent_child_batches_and_is_deterministic():
    scale = EcommerceScale(31, 20, 15)
    first = list(seed.data_batches(scale, 7))
    assert first == list(seed.data_batches(scale, 7))
    totals = dict.fromkeys(TABLES, 0)
    for group in first:
        for table, rows in group.items():
            totals[table] += len(rows)
            assert all(len(row) == len(TABLE_COLUMNS[table]) for row in rows)
        if "ec_orders" in group:
            ids = {row[0] for row in group["ec_orders"]}
            for table in ("ec_order_items", "ec_payments", "ec_refunds"):
                assert all(row[1] in ids for row in group[table])
            assert len(ids) <= 7
    assert totals["ec_orders"] == 31
    assert totals["ec_customers"] == 20
    assert totals["ec_products"] == 15


def test_generated_export_has_manifest_and_no_destructive_statements():
    output = io.StringIO()
    report = seed.load(EcommerceScale(8, 10, 8), 3, export=output)
    sql = output.getvalue()
    assert report["status"] == "GENERATED"
    assert report["counts"]["ec_orders"] == 8
    assert sql.index("'LOADING'") < sql.index("INSERT INTO `ec_orders`")
    assert sql.index("INSERT INTO `ec_orders`") < sql.index("status='UNVERIFIED'")
    assert "status='COMPLETE'" not in sql
    assert sql.count("START TRANSACTION;") == sql.count("COMMIT;")
    assert "DROP " not in sql and "TRUNCATE " not in sql
    assert "INSERT INTO `orders`" not in sql


def test_failure_after_first_batch_stays_incomplete_without_retry():
    class FailingSession(Session):
        def request(self, sql, **kwargs):
            result = super().request(sql, **kwargs)
            if "INSERT INTO `ec_customers`" in sql:
                raise seed.SeedError("uncertain batch")
            return result

    session = FailingSession()
    with pytest.raises(seed.SeedError, match="uncertain"):
        seed.load(EcommerceScale(8, 10, 8), 3, session=session)
    assert sum("INSERT INTO `ec_customers`" in sql for sql in session.calls) == 1
    assert not any("status='COMPLETE'" in sql for sql in session.calls)
    assert not any("DROP " in sql for sql in session.calls)


def test_export_refuses_outside_directory_and_existing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "ROOT", tmp_path)
    with pytest.raises(seed.SeedError):
        seed.output_path("elsewhere.sql")
    path = seed.output_path("outputs/ecommerce/data.sql")
    path.write_text("existing")
    with pytest.raises(seed.SeedError):
        seed.output_path("outputs/ecommerce/data.sql")
    assert path.read_text() == "existing"


def test_remote_docker_host_is_rejected_before_invoking_commands(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    monkeypatch.setattr(seed, "command", lambda _: pytest.fail("must not access remote Docker"))
    with pytest.raises(seed.SeedError):
        seed.verify_local_service()


def test_non_consuming_child_is_terminated_within_write_budget(monkeypatch):
    # An actual local child stops reading stdin; no Docker or database involved.
    monkeypatch.setattr(seed, "MYSQL", [sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    with pytest.raises(seed.SeedError, match="写入超过时限"):
        with seed.MysqlSession() as session:
            session.request("x" * 1_000_000, timeout=0.15)
    assert time.monotonic() - started < 3
    assert session.process.poll() is not None


def test_descendant_inheriting_pipes_cannot_keep_timed_out_session_alive(monkeypatch):
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
    )
    monkeypatch.setattr(seed, "MYSQL", [sys.executable, "-c", script])
    started = time.monotonic()
    with pytest.raises(seed.SeedError):
        with seed.MysqlSession() as session:
            session.request("x" * 1_000_000, timeout=0.3)
    assert time.monotonic() - started < 3
    assert session.process.poll() is not None


def test_analyze_table_level_error_prevents_complete_marker(monkeypatch):
    class TableError(Session):
        def request(self, sql, **kwargs):
            result = super().request(sql, **kwargs)
            if sql.startswith("ANALYZE TABLE"):
                return ["db_agent.ec_orders\tanalyze\terror\tfixture failure"]
            return result

    session = TableError()
    monkeypatch.setattr(seed, "verify_counts", lambda *args: None)
    monkeypatch.setattr(seed, "verify_money", lambda *args: None)
    with pytest.raises(seed.SeedError, match="统计信息"):
        seed.load(EcommerceScale(8, 10, 8), 3, session=session)
    assert not any("status='COMPLETE'" in sql for sql in session.calls)


def test_statistics_require_one_success_per_fixed_table():
    rows = [f"db_agent.{table}\tanalyze\tstatus\tOK" for table in TABLES]
    seed.validate_statistics(rows)
    for invalid in (rows[:-1], [*rows, rows[0]], [*rows[:-1], "other\tanalyze\tstatus\tOK"]):
        with pytest.raises(seed.SeedError):
            seed.validate_statistics(invalid)
