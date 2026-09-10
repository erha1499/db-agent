"""Opt-in DDL races on disposable tables in the exact local Compose MySQL.

Run with DB_AGENT_MYSQL_DDL_INTEGRATION=1. Admin SQL uses the container socket;
only tables successfully created by this fixture are eligible for cleanup.
"""

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from db_agent.config import AnalysisSettings, QuerySettings, load_database_settings
from db_agent.db import MetadataConnector

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_DDL_INTEGRATION") != "1",
    reason="requires DB_AGENT_MYSQL_DDL_INTEGRATION=1 for disposable local DDL fixtures",
)

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = ["docker", "compose", "--project-name", "db-agent", "--file", "compose.yaml"]
_TABLE = re.compile(r"query_isolation_[a-f0-9]{32}\Z")
_ADMIN = [
    *_COMPOSE, "exec", "-T", "mysql", "sh", "-c",
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=SOCKET --user=root '
    '--database=db_agent --batch --skip-column-names --raw',
]


def _process(command, *, sql=None, timeout=3):
    try:
        return subprocess.run(
            command, cwd=_ROOT, input=sql, text=True, capture_output=True, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("local isolation probe exceeded its command budget", pytrace=False)


def _assert_local_target(settings):
    assert (settings.host, settings.port, settings.database, settings.user) == (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    ), "isolation tests require the exact local reader target"
    assert not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://"), (
        "isolation tests refuse a remote Docker host override"
    )
    endpoint = _process([
        "docker", "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}',
    ])
    assert endpoint.returncode == 0 and endpoint.stdout.strip().startswith("unix://"), (
        "isolation tests require a local Docker socket"
    )
    state = _process([*_COMPOSE, "ps", "--format", "json", "mysql"])
    assert state.returncode == 0, "could not inspect the local Compose target"
    try:
        containers = [json.loads(line) for line in state.stdout.splitlines() if line.strip()]
        if len(containers) == 1 and isinstance(containers[0], list):
            containers = containers[0]
    except ValueError:
        pytest.fail("unrecognized Compose target state", pytrace=False)
    assert len(containers) == 1, "expected exactly one local Compose MySQL container"
    container = containers[0]
    assert (
        container.get("Project"), container.get("Service"), container.get("State"),
        container.get("Health"),
    ) == ("db-agent", "mysql", "running", "healthy"), "local Compose target mismatch"
    labels = container.get("Labels", {})
    if isinstance(labels, str):
        labels = dict(item.split("=", 1) for item in labels.split(",") if "=" in item)
    assert labels.get("com.docker.compose.project.working_dir") == str(_ROOT), (
        "Compose container belongs to a different working directory"
    )
    bindings = [
        item for item in container.get("Publishers", []) if item.get("TargetPort") == 3306
    ]
    assert bindings == [{
        "URL": "127.0.0.1", "TargetPort": 3306, "PublishedPort": 13306, "Protocol": "tcp",
    }], "MySQL must be published only on 127.0.0.1:13306"


class _FixtureAdmin:
    def __init__(self):
        self.table = "query_isolation_" + uuid4().hex
        self.deadline = time.monotonic() + 14
        self.created = False

    def run(self, action):
        assert _TABLE.fullmatch(self.table), "invalid disposable fixture identifier"
        table = self.table
        statements = {
            "target": "SELECT DATABASE(), VERSION();",
            "exists": (
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = 'db_agent' "
                f"AND CAST(TABLE_NAME AS BINARY) = CAST('{table}' AS BINARY);"
            ),
            "create": (
                f"CREATE TABLE `db_agent`.`{table}` (id INT NOT NULL PRIMARY KEY) "
                "ENGINE=InnoDB COMMENT='query-isolation-created';"
            ),
            "seed": f"INSERT INTO `db_agent`.`{table}` (id) VALUES (1);",
            "alter_comment": (
                "SET SESSION lock_wait_timeout=1; "
                f"ALTER TABLE `db_agent`.`{table}` COMMENT='query-isolation-altered';"
            ),
            "change_engine": (
                "SET SESSION lock_wait_timeout=1; "
                f"ALTER TABLE `db_agent`.`{table}` ENGINE=MyISAM;"
            ),
            "state": (
                "SELECT TABLE_TYPE, ENGINE, TABLE_COMMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = 'db_agent' "
                f"AND CAST(TABLE_NAME AS BINARY) = CAST('{table}' AS BINARY);"
            ),
            "drop": f"SET SESSION lock_wait_timeout=1; DROP TABLE `db_agent`.`{table}`;",
        }
        if action == "drop":
            assert self.created, "refusing to clean up an object this fixture did not create"
        remaining = self.deadline - time.monotonic()
        assert remaining > 0, "isolation fixture exceeded its total time budget"
        result = _process(_ADMIN, sql=statements[action], timeout=min(3, remaining))
        if result.returncode:
            match = re.search(r"ERROR\s+(\d+)\b", result.stderr)
            # Never surface the raw admin exception, SQL output or credentials.
            return int(match.group(1)) if match else -1, ""
        return 0, result.stdout.strip()

    def require(self, action):
        code, value = self.run(action)
        assert code == 0, f"disposable fixture admin operation failed: errno={code}"
        return value


@pytest.fixture
def isolated_table():
    admin = _FixtureAdmin()
    settings = load_database_settings()
    _assert_local_target(settings)
    target = admin.require("target").split("\t")
    assert len(target) == 2 and target[0] == "db_agent" and re.fullmatch(
        r"8\.4\.\d+(?:[-+][A-Za-z0-9._-]+)?", target[1],
    ), "container socket is not the expected MySQL 8.4 database"
    assert admin.require("exists") == "0", "random fixture name already exists; refusing to reuse"
    admin.require("create")
    admin.created = True
    try:
        admin.require("seed")
        yield admin, settings.model_copy(update={"allowed_tables": (admin.table,)})
    finally:
        admin.require("drop")
        assert admin.require("exists") == "0", "disposable fixture cleanup was not confirmed"


def _limits():
    # Fixed synthetic test budgets, independent of the developer's .env limits.
    analysis = AnalysisSettings.model_construct(timeout_seconds=7)
    query = QuerySettings.model_construct(
        max_rows=5, execution_timeout_seconds=2, operation_timeout_seconds=8,
    )
    return analysis, query


async def _finish_task(task, resume):
    resume.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("completion", ["normal", "cancel"])
def test_explain_holds_metadata_lock_until_reader_finishes(isolated_table, completion):
    admin, settings = isolated_table

    async def scenario():
        ready, resume = asyncio.Event(), asyncio.Event()

        class PausedAfterPlan(MetadataConnector):
            async def _read_plan(self, connection, sql, limits):
                plan = await super()._read_plan(connection, sql, limits)
                ready.set()
                await resume.wait()
                return plan

        connector = PausedAfterPlan(settings)
        task = asyncio.create_task(connector.execute_checked(
            f"SELECT id FROM {admin.table} ORDER BY id LIMIT 1", *_limits(),
        ))
        try:
            async with asyncio.timeout(8):
                await asyncio.wait_for(ready.wait(), 3)
                code, _ = await asyncio.to_thread(admin.run, "alter_comment")
                assert code == 1205, f"DDL was not blocked by transaction MDL: errno={code}"
                state = await asyncio.to_thread(admin.require, "state")
                assert state == "BASE TABLE\tInnoDB\tquery-isolation-created"
                if completion == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    resume.set()
                    outcome = await task
                    assert outcome["decision"] == "ALLOW"
                    assert outcome["execution_status"] == "completed"
                    assert outcome["result"]["rows"] == [[1]]
                await asyncio.to_thread(admin.require, "alter_comment")
                state = await asyncio.to_thread(admin.require, "state")
                assert state == "BASE TABLE\tInnoDB\tquery-isolation-altered"
        finally:
            await _finish_task(task, resume)

    asyncio.run(scenario())


def test_engine_change_after_first_validation_prevents_business_execution(isolated_table):
    admin, settings = isolated_table

    async def scenario():
        ready, resume = asyncio.Event(), asyncio.Event()

        class PausedAfterValidation(MetadataConnector):
            validation_count = 0

            async def _validate_analysis_tables(self, connection, checked, *, require_innodb=False):
                await super()._validate_analysis_tables(
                    connection, checked, require_innodb=require_innodb,
                )
                self.validation_count += 1
                if self.validation_count == 1:
                    ready.set()
                    await resume.wait()

        connector = PausedAfterValidation(settings)
        task = asyncio.create_task(connector.execute_checked(
            f"SELECT id FROM {admin.table} ORDER BY id LIMIT 1", *_limits(),
        ))
        try:
            async with asyncio.timeout(8):
                await asyncio.wait_for(ready.wait(), 3)
                await asyncio.to_thread(admin.require, "change_engine")
                state = await asyncio.to_thread(admin.require, "state")
                assert state == "BASE TABLE\tMyISAM\tquery-isolation-created"
                resume.set()
                outcome = await task
                assert outcome["decision"] == "UNKNOWN"
                assert outcome["execution_status"] == "not_started"
                assert outcome["result"] is None
                assert outcome["error"]["code"] == "UNSUPPORTED_ENGINE"
        finally:
            await _finish_task(task, resume)

    asyncio.run(scenario())
