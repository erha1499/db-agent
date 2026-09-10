"""Create fixed synthetic business tables in this project's local MySQL instance."""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "mysql_business.sql"
COMPOSE = [
    "docker",
    "compose",
    "--project-name",
    "db-agent",
    "--file",
    str(PROJECT_ROOT / "compose.yaml"),
]
MYSQL = [
    *COMPOSE,
    "exec",
    "-T",
    "mysql",
    "bash",
    "-c",
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot '
    "--database=db_agent --batch --skip-column-names",
]
TABLES = frozenset({"customers", "orders", "order_items"})
PREFLIGHT_SQL = """SELECT DATABASE();
SELECT TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'db_agent'
  AND TABLE_NAME IN ('customers', 'orders', 'order_items')
ORDER BY TABLE_NAME;
"""


class SeedError(RuntimeError):
    """An error message that does not expose database or Docker output."""


def run_command(command: list[str], sql: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            input=sql,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SeedError("命令未完成，请检查本地 Docker/MySQL 状态；未输出原始错误。") from None
    if result.returncode:
        raise SeedError("Docker/MySQL 命令失败；请核对目标状态，原始输出未展示。")
    return result.stdout


def verify_local_service() -> None:
    raw = run_command([*COMPOSE, "ps", "--format", "json", "mysql"])
    try:
        decoded = json.loads(raw)
        containers = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        raise SeedError("无法确认唯一的 db-agent/mysql 容器，停止初始化。") from None
    if len(containers) != 1 or not isinstance(containers[0], dict):
        raise SeedError("目标必须是唯一的 db-agent/mysql 容器，停止初始化。")
    container = containers[0]
    if (
        container.get("Project") != "db-agent"
        or container.get("Service") != "mysql"
        or container.get("State") != "running"
    ):
        raise SeedError("容器项目、服务或运行状态不匹配，停止初始化。")


def seed() -> bool:
    """Return False without writes when any fixture table already exists."""
    try:
        sql = FIXTURE.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise SeedError("无法读取项目内固定的合成数据文件。") from None
    verify_local_service()
    rows = run_command(MYSQL, PREFLIGHT_SQL).splitlines()
    if not rows or rows[0] != "db_agent" or not set(rows[1:]).issubset(TABLES):
        raise SeedError("数据库目标或预检查结果不匹配，停止初始化。")
    if rows[1:]:
        print("目标表已存在，未写入任何数据：" + ", ".join(rows[1:]))
        return False
    try:
        run_command(MYSQL, sql)
    except SeedError:
        raise SeedError(
            "初始化未完成；DDL 可能已创建部分表，请核对现场。未自动重试、删除或覆盖。"
        ) from None
    print("已在 db-agent/mysql 的 db_agent 中创建 customers、orders、order_items 合成数据。")
    return True


def main() -> int:
    if len(sys.argv) != 1:
        print("此脚本不接受参数，仅初始化项目内固定的本地合成数据。", file=sys.stderr)
        return 2
    try:
        seed()
    except SeedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
