"""Stream deterministic e-commerce SQL into the exact local Compose MySQL.

No arbitrary SQL or target input; existing objects are never overwritten. A failed
load stays inspectable and must be resolved explicitly before another load.
"""

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pymysql.converters import escape_string

from db_agent.ecommerce import (
    DATASET_VERSION,
    SCHEMA_SQL,
    TABLE_COLUMNS,
    TABLES,
    EcommerceScale,
    iter_customers,
    iter_order_bundles,
    iter_products,
)

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--project-name", "db-agent", "--file", str(ROOT / "compose.yaml")]
MYSQL = [
    *COMPOSE, "exec", "-T", "mysql", "sh", "-c",
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=SOCKET --user=root '
    '--database=db_agent --batch --skip-column-names --raw --unbuffered',
]
MANIFEST = "ec_seed_manifest"
MANIFEST_SCHEMA = """CREATE TABLE ec_seed_manifest (
  id INT PRIMARY KEY,
  dataset_key CHAR(64) NOT NULL,
  dataset_version VARCHAR(64) NOT NULL,
  config_json TEXT NOT NULL,
  counts_json TEXT NOT NULL,
  status VARCHAR(16) NOT NULL,
  committed_batches BIGINT NOT NULL,
  created_at DATETIME NOT NULL,
  completed_at DATETIME NULL
) ENGINE=InnoDB"""
SESSION_SQL = (
    "SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';\n"
    "SET SESSION time_zone='+00:00';\nSET NAMES utf8mb4;\n"
    "SET SESSION lock_wait_timeout=5;\n"
)


class SeedError(RuntimeError):
    """Only messages without secrets, SQL literals or raw process errors."""


def command(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SeedError("本地 Docker 检查未完成。") from None
    if result.returncode:
        raise SeedError("本地 Docker 检查失败；原始输出未展示。")
    return result.stdout.strip()


def verify_local_service() -> None:
    if os.environ.get("DOCKER_HOST") and not os.environ["DOCKER_HOST"].startswith("unix://"):
        raise SeedError("仅允许本机 Docker socket。")
    endpoint = command([
        "docker", "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}',
    ])
    if not endpoint.startswith("unix://"):
        raise SeedError("Docker context 不是本机 socket。")
    try:
        containers = [
            json.loads(line) for line in command([*COMPOSE, "ps", "--format", "json", "mysql"])
            .splitlines() if line.strip()
        ]
        if len(containers) == 1 and isinstance(containers[0], list):
            containers = containers[0]
        if len(containers) != 1 or not isinstance(containers[0], dict):
            raise ValueError
        container = containers[0]
        labels = container.get("Labels", {})
        if isinstance(labels, str):
            labels = dict(item.split("=", 1) for item in labels.split(",") if "=" in item)
        bindings = [
            item for item in container.get("Publishers", []) if item.get("TargetPort") == 3306
        ]
        if (
            (container.get("Project"), container.get("Service"), container.get("State"),
             container.get("Health")) != ("db-agent", "mysql", "running", "healthy")
            or labels.get("com.docker.compose.project.working_dir") != str(ROOT)
            or bindings != [{"URL": "127.0.0.1", "TargetPort": 3306,
                             "PublishedPort": 13306, "Protocol": "tcp"}]
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SeedError("目标必须是本项目健康的 MySQL，且仅绑定 127.0.0.1:13306。") from None


class MysqlSession:
    """One socket session preserves the advisory lock for the entire import."""

    def __init__(self):
        self.process = None
        self.lines = queue.Queue(maxsize=128)
        self.errno = None
        self.deadline = time.monotonic() + 3600

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                MYSQL, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                start_new_session=True,
            )
        except OSError:
            raise SeedError("无法启动本地 MySQL socket 会话。") from None
        threading.Thread(target=self._stdout, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        return self

    def _stdout(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\n"))
        finally:
            self.lines.put(None)

    def _stderr(self):
        for line in self.process.stderr:
            match = re.search(r"ERROR\s+(\d+)\b", line)
            if match:
                self.errno = int(match.group(1))

    def request(self, sql: str, *, timeout: float = 120) -> list[str]:
        marker = "ec_ack_" + uuid4().hex
        deadline = min(time.monotonic() + timeout, self.deadline)
        written = queue.Queue(maxsize=1)

        def write():
            try:
                self.process.stdin.write(sql + f"\nSELECT '{marker}';\n")
                self.process.stdin.flush()
                written.put(True)
            except (OSError, ValueError):
                written.put(False)

        threading.Thread(target=write, daemon=True).start()
        try:
            success = written.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise SeedError("MySQL 批次写入超过时限；当前批次结果待核对。") from None
        if not success:
            raise SeedError("MySQL 输入连接已关闭；当前批次结果待核对。")
        rows = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SeedError("MySQL 批次超过时限；当前批次结果待核对。")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise SeedError("MySQL 批次超过时限；当前批次结果待核对。") from None
            if line is None:
                raise SeedError(f"MySQL 会话提前结束（errno={self.errno}）；未自动重试。")
            if line == marker:
                return rows
            if len(rows) >= 100 or len(line) > 16384:
                raise SeedError("管理查询返回内容超出预算。")
            rows.append(line)

    def __exit__(self, exc_type, exc, tb):
        if not self.process:
            return
        # Terminate first on failure: closing a buffered stdin could otherwise
        # wait forever for a writer whose pipe the child no longer consumes.
        def signal_group(value):
            try:
                os.killpg(self.process.pid, value)
            except ProcessLookupError:
                pass

        if exc_type is not None:
            signal_group(signal.SIGTERM)
        else:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGKILL)
                self.process.wait(timeout=5)
        # A local CLI descendant can outlive its parent while keeping a pipe
        # open. Close this session's process group before buffered stream close.
        signal_group(signal.SIGKILL)
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process.stdout.close()
        self.process.stderr.close()


def sql_value(value) -> str:
    if value is None:
        return "NULL"
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal) and value.is_finite():
        return format(value, "f")
    if isinstance(value, datetime) and value.tzinfo is None:
        return "'" + value.isoformat(sep=" ", timespec="microseconds") + "'"
    if isinstance(value, str):
        return "'" + escape_string(value) + "'"
    raise SeedError("生成器返回不支持的 SQL 值类型。")


def insert_sql(table: str, rows: list[tuple]) -> str:
    if table not in TABLES or not rows:
        raise SeedError("拒绝非固定表或空批次。")
    columns = TABLE_COLUMNS[table]
    if any(len(row) != len(columns) for row in rows):
        raise SeedError("生成数据列数与固定 schema 不符。")
    values = ",\n".join("(" + ",".join(sql_value(value) for value in row) + ")" for row in rows)
    return f"INSERT INTO `{table}` ({','.join('`' + c + '`' for c in columns)}) VALUES\n{values};\n"


def batches(items: Iterable, size: int):
    chunk = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def dataset_key(scale: EcommerceScale) -> str:
    contents = json.dumps({"version": DATASET_VERSION, "scale": asdict(scale),
                           "schema": SCHEMA_SQL}, sort_keys=True)
    return hashlib.sha256(contents.encode()).hexdigest()


def data_batches(scale: EcommerceScale, batch_size: int):
    if not 1 <= batch_size <= 5000:
        raise SeedError("batch-size 必须为 1–5000。")
    for table, items in (("ec_customers", iter_customers(scale)),
                         ("ec_products", iter_products(scale))):
        for chunk in batches(items, batch_size):
            yield {table: chunk}
    for chunk in batches(iter_order_bundles(scale), batch_size):
        yield {
            "ec_orders": [bundle.order for bundle in chunk],
            "ec_order_items": [row for bundle in chunk for row in bundle.items],
            "ec_payments": [row for bundle in chunk for row in bundle.payments],
            "ec_refunds": [row for bundle in chunk for row in bundle.refunds],
        }


def target_and_existing(session) -> set[str]:
    target = session.request("SELECT DATABASE(), VERSION();")
    if len(target) != 1 or not re.fullmatch(r"db_agent\t8\.4\.\d+(?:[-+][\w.-]+)?", target[0]):
        raise SeedError("socket 目标不是 db_agent / MySQL 8.4。")
    if session.request("SELECT GET_LOCK('db_agent.ec_seed', 0);") != ["1"]:
        raise SeedError("另一个电商导入仍持有锁。")
    names = ",".join(sql_value(name) for name in (*TABLES, MANIFEST))
    rows = session.request(
        "SELECT TABLE_NAME,TABLE_TYPE,ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA='db_agent' "
        f"AND TABLE_NAME IN ({names}) ORDER BY TABLE_NAME;"
    )
    existing = set()
    for row in rows:
        fields = row.split("\t")
        if (len(fields) != 3 or fields[0] not in (*TABLES, MANIFEST)
                or fields[1:] != ["BASE TABLE", "InnoDB"]):
            raise SeedError("已有目标对象不是预期的 InnoDB 基础表；未读取其数据。")
        existing.add(fields[0])
    return existing


def read_manifest(session) -> dict:
    rows = session.request(
        "SELECT dataset_key,dataset_version,config_json,counts_json,status,committed_batches "
        "FROM ec_seed_manifest WHERE id=1;"
    )
    try:
        if len(rows) != 1:
            raise ValueError
        key, version, config, counts, status, count = rows[0].split("\t")
        return {"dataset_key": key, "dataset_version": version, "config": json.loads(config),
                "counts": json.loads(counts), "status": status, "committed_batches": int(count)}
    except (ValueError, TypeError):
        raise SeedError("导入清单内容无法确认。") from None


def verify_counts(session, expected: dict[str, int]) -> dict[str, int]:
    sql = "\n".join(f"SELECT '{table}',COUNT(*) FROM `{table}`;" for table in TABLES)
    try:
        actual = dict((name, int(count)) for name, count in
                      (row.split("\t") for row in session.request(sql, timeout=300)))
    except ValueError:
        raise SeedError("实际数据量查询返回格式异常。") from None
    if actual != expected:
        raise SeedError("实际行数与导入清单不符；数据未通过验收。")
    return actual


def validate_manifest_counts(counts, scale: EcommerceScale) -> None:
    if (not isinstance(counts, dict) or set(counts) != set(TABLES)
            or any(type(count) is not int or count < 0 for count in counts.values())
            or counts["ec_orders"] != scale.orders
            or counts["ec_customers"] != scale.customers
            or counts["ec_products"] != scale.products
            or counts["ec_order_items"] < scale.orders
            or counts["ec_payments"] < scale.orders):
        raise SeedError("导入清单计数与目标规模不符。")


def validate_statistics(rows: list[str]) -> None:
    expected = {f"db_agent.{table}\tanalyze\tstatus\tOK" for table in TABLES}
    if len(rows) != len(expected) or set(rows) != expected:
        raise SeedError("统计信息采集未全部返回 OK；清单保持未完成。")


def verify_money(session) -> None:
    # Administrator setup validation, not a product query or precheck shortcut.
    checks = [
        "SELECT COUNT(*) FROM ec_orders o LEFT JOIN "
        "(SELECT order_id,SUM(line_total) total FROM ec_order_items GROUP BY order_id) i "
        "ON i.order_id=o.id WHERE i.order_id IS NULL OR i.total<>o.total_amount;",
        "SELECT COUNT(*) FROM ec_orders o LEFT JOIN "
        "(SELECT order_id,SUM(amount) total FROM ec_payments WHERE status='succeeded' "
        "GROUP BY order_id) p ON p.order_id=o.id WHERE "
        "(o.status IN ('paid','refunded','partially_refunded') "
        "AND COALESCE(p.total,0)<>o.total_amount) OR "
        "(o.status IN ('pending','cancelled') AND COALESCE(p.total,0)<>0);",
        "SELECT COUNT(*) FROM ec_refunds r INNER JOIN ec_payments p ON p.id=r.payment_id "
        "WHERE r.order_id<>p.order_id OR p.status<>'succeeded' OR r.amount>p.amount;",
        "SELECT COUNT(*) FROM ec_payments p INNER JOIN "
        "(SELECT payment_id,SUM(amount) total FROM ec_refunds WHERE status='succeeded' "
        "GROUP BY payment_id) r ON r.payment_id=p.id WHERE r.total>p.amount;",
        "SELECT COUNT(*) FROM ec_orders o LEFT JOIN "
        "(SELECT order_id,SUM(amount) total FROM ec_refunds WHERE status='succeeded' "
        "GROUP BY order_id) r ON r.order_id=o.id WHERE "
        "(o.status='refunded' AND COALESCE(r.total,0)<>o.total_amount) OR "
        "(o.status='partially_refunded' AND "
        "(COALESCE(r.total,0)<=0 OR COALESCE(r.total,0)>=o.total_amount)) OR "
        "(o.status IN ('paid','pending','cancelled') AND COALESCE(r.total,0)<>0);",
    ]
    for sql in checks:
        if session.request(sql, timeout=300) != ["0"]:
            raise SeedError("订单、明细、支付或退款金额关系检查失败。")


def load(scale: EcommerceScale, batch_size: int, *, session=None, export=None,
         verify_only=False) -> dict:
    started = time.monotonic()
    key = dataset_key(scale)
    expected_objects = {*TABLES, MANIFEST}
    if session:
        existing = target_and_existing(session)
        if existing:
            if existing != expected_objects:
                raise SeedError("部分目标表已存在；未覆盖、删除或自动补写。")
            manifest = read_manifest(session)
            allowed_states = {"COMPLETE", "UNVERIFIED"} if verify_only else {"COMPLETE"}
            if (manifest["dataset_key"] != key or manifest["status"] not in allowed_states
                    or manifest["config"] != asdict(scale)
                    or manifest["dataset_version"] != DATASET_VERSION):
                raise SeedError("已有数据的配置不匹配或导入未完成；请先核对现场。")
            validate_manifest_counts(manifest["counts"], scale)
            verify_counts(session, manifest["counts"])
            verify_money(session)
            if export:
                load(scale, batch_size, export=export)
            return {**manifest, "action": "verified_existing", "duration_seconds":
                    round(time.monotonic() - started, 3)}
        if verify_only:
            raise SeedError("电商数据尚未加载。")

    def emit(sql):
        if export:
            export.write(sql + "\n")
        if session:
            return session.request(sql)
        return []

    emit(SESSION_SQL)
    emit(MANIFEST_SCHEMA + ";")
    config = json.dumps(asdict(scale), sort_keys=True, separators=(",", ":"))
    emit("INSERT INTO ec_seed_manifest VALUES (1," + sql_value(key) + ","
         + sql_value(DATASET_VERSION) + "," + sql_value(config)
         + ",'{}','LOADING',0,NOW(),NULL);")
    for schema in SCHEMA_SQL:
        emit(schema.rstrip(";\n") + ";")
    counts = dict.fromkeys(TABLES, 0)
    batch_count = 0
    last_progress = started
    for group in data_batches(scale, batch_size):
        if time.monotonic() - started > 3600:
            raise SeedError("导入超过一小时总预算；保留当前清单，未自动重试。")
        statements = ["START TRANSACTION;\n"]
        for table, rows in group.items():
            if rows:
                statements.append(insert_sql(table, rows))
            counts[table] += len(rows)
        batch_count += 1
        statements.append("UPDATE ec_seed_manifest SET committed_batches=" + str(batch_count)
                          + " WHERE id=1;\nCOMMIT;\n")
        emit("".join(statements))
        if time.monotonic() - last_progress >= 5:
            print(f"已处理订单 {counts['ec_orders']:,}/{scale.orders:,}；批次 {batch_count}",
                  flush=True)
            last_progress = time.monotonic()
    if session:
        validate_manifest_counts(counts, scale)
        verify_counts(session, counts)
        verify_money(session)
    statistics = emit("ANALYZE TABLE " + ",".join("`" + table + "`" for table in TABLES) + ";")
    if session:
        validate_statistics(statistics)
    counts_json = json.dumps(counts, sort_keys=True, separators=(",", ":"))
    # A replayed export has not run the Python verifier, even if this particular
    # invocation also loaded and verified a live database.
    if export:
        export.write("UPDATE ec_seed_manifest SET status='UNVERIFIED',counts_json="
                     + sql_value(counts_json) + ",completed_at=NULL WHERE id=1;\n")
    if session:
        session.request("UPDATE ec_seed_manifest SET status='COMPLETE',counts_json="
                        + sql_value(counts_json) + ",completed_at=NOW() WHERE id=1;")
    return {"dataset_key": key, "dataset_version": DATASET_VERSION, "config": asdict(scale),
            "counts": counts, "committed_batches": batch_count,
            "status": "COMPLETE" if session else "GENERATED",
            "action": "loaded" if session else "generated_sql",
            "duration_seconds": round(time.monotonic() - started, 3)}


def output_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT / "outputs" / "ecommerce"):
        raise SeedError("生成文件仅允许写入本项目 outputs/ecommerce 目录。")
    if path.exists():
        raise SeedError("输出路径已存在；不会覆盖。")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=1_000_000)
    parser.add_argument("--customers", type=int, default=100_000)
    parser.add_argument("--products", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--sql-output", help="Optional new SQL file under outputs/ecommerce")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true", help="Load the exact local Compose DB")
    actions.add_argument("--verify-only", action="store_true", help="Verify an existing load")
    args = parser.parse_args(argv)
    export = None
    try:
        if not 1 <= args.batch_size <= 5000:
            raise SeedError("batch-size 必须为 1–5000。")
        if not args.apply and not args.verify_only and not args.sql_output:
            raise SeedError("请选择 --sql-output 生成 SQL，或 --apply 导入本地数据库。")
        if args.verify_only and args.sql_output:
            raise SeedError("verify-only 不生成 SQL 文件。")
        scale = EcommerceScale(args.orders, args.customers, args.products, args.seed)
        if args.apply or args.verify_only:
            verify_local_service()
        if args.sql_output:
            export = output_path(args.sql_output).open("x", encoding="utf-8")
        if args.apply or args.verify_only:
            with MysqlSession() as session:
                report = load(scale, args.batch_size, session=session, export=export,
                              verify_only=args.verify_only)
        else:
            report = load(scale, args.batch_size, export=export)
        report["sql_output"] = str(export.name) if export else None
        destination = output_path("outputs/ecommerce/load-" + uuid4().hex + ".json")
        destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print("记录：" + str(destination))
        return 0
    except (SeedError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, SeedError) else "配置或本地文件操作失败。"
        print(message + " 已存在的数据和部分导入内容均保留；未自动重试。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("导入已中断；当前批次结果待核对，已提交批次保留。", file=sys.stderr)
        return 130
    finally:
        if export:
            export.close()


if __name__ == "__main__":
    raise SystemExit(main())
