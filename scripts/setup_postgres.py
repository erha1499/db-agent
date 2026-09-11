"""Initialize or verify only the fixed, isolated PostgreSQL synthetic fixture.

No application API imports, model calls, user SQL, or configurable target. Admin
access is through the container's local peer-authenticated postgres OS user.
"""

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / ".env.postgres"
FIXTURE = PROJECT_ROOT / "tests/fixtures/postgres_business.sql"
COMPOSE_FILE = PROJECT_ROOT / "compose.postgres.yaml"
COMPOSE = [
    "docker", "compose", "--project-name", "db-agent-postgres",
    "--env-file", str(CONFIG), "--file", str(COMPOSE_FILE),
]
PSQL = [
    *COMPOSE, "exec", "-T", "--user", "postgres", "postgres",
    "psql", "--username=postgres", "--dbname=db_agent_pg", "--no-psqlrc",
    "--set=ON_ERROR_STOP=1", "--tuples-only", "--no-align", "--quiet",
]
READER = [
    *COMPOSE, "exec", "-T", "postgres", "bash", "-c",
    'PGPASSWORD="$DB_AGENT_POSTGRES_PASSWORD" exec psql '
    "--host=127.0.0.1 --username=db_agent_reader --dbname=db_agent_pg "
    "--no-psqlrc --set=ON_ERROR_STOP=1 --tuples-only --no-align --quiet",
]
TARGET = {
    "DB_AGENT_DATABASE_KIND": "postgresql",
    "DB_AGENT_POSTGRES_HOST": "127.0.0.1",
    "DB_AGENT_POSTGRES_PORT": "15432",
    "DB_AGENT_POSTGRES_DATABASE": "db_agent_pg",
    "DB_AGENT_POSTGRES_SCHEMA": "business",
    "DB_AGENT_POSTGRES_USER": "db_agent_reader",
    "DB_AGENT_POSTGRES_ALLOWED_TABLES": '["customers","orders","order_items"]',
}
TABLES = {"customers", "orders", "order_items", "pg_scan_probe"}
PREFLIGHT = """
SELECT current_database();
SELECT relname FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname='business'
AND c.relname IN ('customers','orders','order_items','pg_scan_probe')
ORDER BY relname;
"""
VERIFY = """
SELECT json_build_object(
 'version', current_setting('server_version'),
 'version_num', current_setting('server_version_num')::integer,
 'database', current_database(),
 'customers', (SELECT count(*) FROM business.customers),
 'orders', (SELECT count(*) FROM business.orders),
 'order_items', (SELECT count(*) FROM business.order_items),
 'pg_scan_probe', (SELECT count(*) FROM business.pg_scan_probe),
 'orders_total', (SELECT sum(total_amount)::text FROM business.orders),
 'paid_total', (SELECT sum(total_amount)::text FROM business.orders WHERE status='paid'),
 'paid_created_feb', (SELECT sum(total_amount)::text FROM business.orders
     WHERE status='paid' AND created_at >= '2026-02-01' AND created_at < '2026-03-01'),
 'paid_in_feb_utc', (SELECT sum(total_amount)::text FROM business.orders
     WHERE status='paid' AND paid_at >= '2026-02-01+00' AND paid_at < '2026-03-01+00'),
 'item_total', (SELECT sum(quantity*unit_price-discount_amount)::text
     FROM business.order_items),
 'no_orders', (SELECT count(*) FROM business.customers c WHERE NOT EXISTS
     (SELECT 1 FROM business.orders o WHERE o.customer_id=c.id)),
 'null_regions', (SELECT count(*) FROM business.customers WHERE region IS NULL),
 'utc_payment', (SELECT paid_at::text FROM business.orders WHERE id=1001),
 'role_safe', (SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
     AND NOT rolinherit AND NOT rolreplication AND NOT rolbypassrls AND rolcanlogin
     FROM pg_catalog.pg_roles WHERE rolname='db_agent_reader'),
 'memberships', (SELECT count(*) FROM pg_catalog.pg_auth_members
     WHERE member=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname='db_agent_reader'))
);
"""
READER_VERIFY = """
SELECT json_build_object(
 'user', current_user,
 'database', current_database(),
 'read_only', current_setting('default_transaction_read_only'),
 'search_path', current_setting('search_path'),
 'timezone', current_setting('TimeZone'),
 'connect', has_database_privilege(current_user, current_database(), 'CONNECT'),
 'create', has_database_privilege(current_user, current_database(), 'CREATE'),
 'temp', has_database_privilege(current_user, current_database(), 'TEMP'),
 'schema_usage', has_schema_privilege(current_user, 'business', 'USAGE'),
 'schema_create', has_schema_privilege(current_user, 'business', 'CREATE'),
 'public_usage', has_schema_privilege(current_user, 'public', 'USAGE'),
 'table_select', has_table_privilege(current_user, 'business.orders', 'SELECT'),
 'table_insert', has_table_privilege(current_user, 'business.orders', 'INSERT'),
 'table_update', has_table_privilege(current_user, 'business.orders', 'UPDATE'),
 'table_delete', has_table_privilege(current_user, 'business.orders', 'DELETE'),
 'table_truncate', has_table_privilege(current_user, 'business.orders', 'TRUNCATE'),
 'orders', (SELECT count(*) FROM business.orders)
);
"""


class SetupError(RuntimeError):
    """A safe message that never includes subprocess output or credentials."""


def run(command: list[str], sql: str | None = None, timeout: int = 30) -> str:
    # Compose gives shell variables priority over --env-file. Remove only these
    # fixture variables so an unrelated shell cannot replace the fixed config.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("DB_AGENT_POSTGRES_")
    }
    try:
        result = subprocess.run(
            command, cwd=PROJECT_ROOT, env=environment, input=sql, text=True,
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("本地 PostgreSQL 命令未完成；未自动重试或输出原始错误。") from None
    if result.returncode:
        raise SetupError("本地 PostgreSQL 命令失败；未输出原始错误，请核对固定目标。")
    return result.stdout.strip()


def prepare_config(*, create: bool) -> None:
    if not CONFIG.exists():
        if not create:
            raise SetupError("缺少本工作区 .env.postgres；verify-only 不生成配置。")
        content = "\n".join(f"{key}='{value}'" for key, value in TARGET.items()) + "\n"
        content += f"DB_AGENT_POSTGRES_PASSWORD={secrets.token_urlsafe(36)}\n"
        content += f"DB_AGENT_POSTGRES_ADMIN_PASSWORD={secrets.token_urlsafe(36)}\n"
        try:
            descriptor = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
        except OSError:
            raise SetupError("无法独占创建私有 .env.postgres；未覆盖现有配置。") from None
    try:
        mode = CONFIG.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise SetupError(".env.postgres 必须为当前工作区的普通私有文件，权限为 0600。")
        values = dotenv_values(CONFIG, interpolate=False)
    except (OSError, UnicodeError):
        raise SetupError("无法读取本工作区 .env.postgres。") from None
    if any(values.get(key) != value for key, value in TARGET.items()):
        raise SetupError(".env.postgres 必须匹配脚本内固定的本机合成目标和三表白名单。")
    passwords = [values.get(f"DB_AGENT_POSTGRES_{name}") for name in (
        "PASSWORD", "ADMIN_PASSWORD",
    )]
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", value)
           for value in passwords) or passwords[0] == passwords[1]:
        raise SetupError("须配置两个不同的 24–128 位字母、数字、_ 或 - 密码。")


def verify_local_docker() -> None:
    override = os.environ.get("DOCKER_HOST", "")
    endpoint = run([
        "docker", "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}',
    ])
    if (override and not override.startswith("unix://")) or not endpoint.startswith("unix://"):
        raise SetupError("初始化和验收仅允许本机 Docker Unix socket，拒绝远端 Docker 目标。")


def verify_container(*, running: bool) -> bool:
    ids = run([*COMPOSE, "ps", "--all", "--quiet", "postgres"]).splitlines()
    if not ids:
        return False
    if len(ids) != 1:
        raise SetupError("必须为唯一的 db-agent-postgres/postgres 容器。")
    raw = run([
        "docker", "inspect", "--format",
        '{"labels":{{json .Config.Labels}},"ports":{{json .HostConfig.PortBindings}},'
        '"running":{{json .State.Running}},"image":{{json .Config.Image}}}', ids[0],
    ])
    try:
        item = json.loads(raw)
        labels = item["labels"]
        matches = (
            labels.get("com.docker.compose.project") == "db-agent-postgres"
            and labels.get("com.docker.compose.service") == "postgres"
            and labels.get("com.docker.compose.project.working_dir") == str(PROJECT_ROOT)
            and labels.get("com.docker.compose.project.config_files") == str(COMPOSE_FILE)
            and item["ports"] == {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15432"}]}
            and item["image"].split("@")[0] == "postgres:18.6"
            and (not running or item["running"] is True)
        )
    except (ValueError, KeyError, TypeError):
        matches = False
    if not matches:
        raise SetupError("已存在容器的工作区、目标、镜像或端口不匹配，停止且不重建。")
    return True


def verify() -> dict[str, object]:
    if not verify_container(running=True):
        raise SetupError("固定 PostgreSQL 容器未运行，verify-only 不启动服务。")
    try:
        result = json.loads(run(PSQL, VERIFY))
        reader = json.loads(run(READER, READER_VERIFY))
    except (ValueError, TypeError):
        raise SetupError("PostgreSQL 验收输出不完整。") from None
    expected = {
        "version_num": 180006, "database": "db_agent_pg", "customers": 5,
        "orders": 6, "order_items": 6, "pg_scan_probe": 100001,
        "orders_total": "230.00", "paid_total": "130.00", "paid_created_feb": "30.00",
        "paid_in_feb_utc": "130.00", "item_total": "230.00", "no_orders": 1,
        "null_regions": 1, "utc_payment": "2026-02-01 00:01:00+00",
        "role_safe": True, "memberships": 0,
    }
    expected_reader = {
        "user": "db_agent_reader", "database": "db_agent_pg", "read_only": "on",
        "search_path": "pg_catalog, business", "timezone": "UTC", "connect": True,
        "create": False, "temp": False, "schema_usage": True, "schema_create": False,
        "public_usage": False, "table_select": True, "table_insert": False,
        "table_update": False, "table_delete": False, "table_truncate": False, "orders": 6,
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise SetupError("固定版本、数据或角色属性验收不符；未覆盖、修复或重试。")
    if reader != expected_reader:
        raise SetupError("真实 reader 身份、数据、默认只读设置或权限验收不符。")
    return {"fixture": result, "reader": reader}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        verify_local_docker()
        prepare_config(create=args.apply)
        if args.apply:
            verify_container(running=False)
            run([*COMPOSE, "up", "--detach", "--wait", "--no-recreate", "postgres"], timeout=180)
            if not verify_container(running=True):
                raise SetupError("启动后未找到固定容器。")
            rows = run(PSQL, PREFLIGHT).splitlines()
            if not rows or rows[0] != "db_agent_pg" or not set(rows[1:]).issubset(TABLES):
                raise SetupError("预检查的数据库或对象不符，停止初始化。")
            if rows[1:]:
                raise SetupError("固定目标表已存在；未写入数据，请用 --verify-only 验收。")
            run(PSQL, FIXTURE.read_text(encoding="utf-8"))
        print(json.dumps({"status": "verified", **verify()}, ensure_ascii=False, indent=2))
    except (SetupError, OSError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, SetupError) else "无法读取固定 fixture。"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
