"""Opt-in PostgreSQL isolation probes on uniquely named disposable objects.

Admin setup uses only the exact local Compose container's peer-authenticated
socket. Only this fixture's successfully created random objects are cleaned up.
The original reader receives SELECT on probes, never write or membership grants.
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from db_agent.config import AnalysisSettings, PostgreSQLSettings, QuerySettings
from db_agent.db import DatabaseError
from db_agent.postgres import PostgreSQLConnector
from db_agent.query import QueryService

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_POSTGRES_DDL_INTEGRATION") != "1",
    reason="requires DB_AGENT_POSTGRES_DDL_INTEGRATION=1 for disposable local DDL probes",
)
ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--project-name", "db-agent-postgres", "--env-file",
           str(ROOT / ".env.postgres"), "--file", str(ROOT / "compose.postgres.yaml")]
ADMIN = [*COMPOSE, "exec", "-T", "--user", "postgres", "postgres", "psql",
         "--username=postgres", "--dbname=db_agent_pg", "--no-psqlrc",
         "--set=ON_ERROR_STOP=1", "--set=VERBOSITY=sqlstate", "--tuples-only",
         "--no-align", "--quiet"]
NAME = re.compile(r"(?:pg_probe|db_agent_probe)_[a-f0-9]{32}(?:_[a-z]+)?\Z")


@pytest.fixture(autouse=True)
def fixed_default_budgets(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("DB_AGENT_ANALYSIS_", "DB_AGENT_QUERY_")):
            monkeypatch.delenv(key)


def process(command, sql=None, *, timeout=10):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("DB_AGENT_POSTGRES_")}
    try:
        return subprocess.run(command, cwd=ROOT, env=environment, input=sql, text=True,
                              capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("local PostgreSQL probe command failed or exceeded its budget", pytrace=False)


def local_target(settings):
    target = (settings.host, settings.port, settings.database, settings.schema_name, settings.user)
    assert target == (
        "127.0.0.1", 15432, "db_agent_pg", "business", "db_agent_reader",
    )
    override = os.environ.get("DOCKER_HOST", "")
    assert not override or override.startswith("unix://"), "remote Docker hosts are not allowed"
    endpoint = process(["docker", "context", "inspect", "--format",
                        '{{(index .Endpoints "docker").Host}}'])
    assert endpoint.returncode == 0 and endpoint.stdout.strip().startswith("unix://")
    state = process([*COMPOSE, "ps", "--format", "json", "postgres"])
    assert state.returncode == 0, "could not inspect the fixed Compose target"
    parsed = json.loads(state.stdout)
    container = parsed[0] if isinstance(parsed, list) and len(parsed) == 1 else parsed
    assert (container.get("Project"), container.get("Service"), container.get("State"),
            container.get("Health")) == ("db-agent-postgres", "postgres", "running", "healthy")
    labels = container.get("Labels", {})
    if isinstance(labels, str):
        labels = dict(part.split("=", 1) for part in labels.split(",") if "=" in part)
    assert labels.get("com.docker.compose.project.working_dir") == str(ROOT)
    assert container["Publishers"] == [{
        "URL": "127.0.0.1", "TargetPort": 5432, "PublishedPort": 15432, "Protocol": "tcp",
    }]
    target = process(ADMIN, "SELECT current_database(),current_setting('server_version_num');")
    assert target.returncode == 0 and target.stdout.strip() == "db_agent_pg|180006"


class Probe:
    def __init__(self, tmp_path):
        self.table = "pg_probe_" + uuid4().hex
        self.schema = "db_agent_probe_" + self.table.removeprefix("pg_probe_")
        self.created = False
        self.kind = "TABLE"
        self.related = []
        self.writer = "db_agent_probe_" + uuid4().hex
        self.writer_created = False
        self.password = secrets.token_urlsafe(36)
        self.secret_file = tmp_path / "writer.env"
        self.secret_file.touch(mode=0o600)
        self.secret_file.write_text("DB_AGENT_POSTGRES_PASSWORD=" + self.password + "\n")

    def sql(self, action):
        table = self.table
        assert NAME.fullmatch(table)
        reference = "business." + table
        schema = self.schema
        assert NAME.fullmatch(schema)
        operations = {
            "exists": "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                      "ON n.oid=c.relnamespace WHERE n.nspname='business' "
                      f"AND c.relname='{table}';",
            "create": f"CREATE TABLE {reference} (id integer PRIMARY KEY);"
                      f"INSERT INTO {reference} VALUES (1);"
                      f"GRANT SELECT ON {reference} TO db_agent_reader;ANALYZE {reference};",
            "rls": f"ALTER TABLE {reference} ENABLE ROW LEVEL SECURITY;",
            "inheritance": f"CREATE TABLE business.{table}_child () INHERITS ({reference});",
            "generated": f"ALTER TABLE {reference} ADD COLUMN computed integer "
                         "GENERATED ALWAYS AS (id+1) STORED;",
            "domain": f"CREATE DOMAIN business.{table}_domain AS integer;"
                      f"ALTER TABLE {reference} ADD COLUMN custom business.{table}_domain;",
            "view": f"CREATE VIEW business.{table}_view AS SELECT id FROM {reference};"
                    f"GRANT SELECT ON business.{table}_view TO db_agent_reader;",
            "alter": f"SET lock_timeout='100ms';ALTER TABLE {reference} ADD COLUMN later integer;",
            "replace": f"DROP TABLE {reference};CREATE TABLE {reference} (id integer);"
                       f"INSERT INTO {reference} VALUES(77);"
                       f"GRANT SELECT ON {reference} TO db_agent_reader;ANALYZE {reference};",
            "revoke": f"REVOKE SELECT ON {reference} FROM db_agent_reader;",
            "grant": f"GRANT SELECT ON {reference} TO db_agent_reader;",
            "writer": f"CREATE ROLE {self.writer} LOGIN PASSWORD '{self.password}' "
                      "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;"
                      f"GRANT CONNECT ON DATABASE db_agent_pg TO {self.writer};"
                      f"GRANT USAGE ON SCHEMA business TO {self.writer};"
                      f"GRANT SELECT,INSERT ON {reference} TO {self.writer};",
            "column_writer": f"REVOKE INSERT ON {reference} FROM {self.writer};"
                             f"GRANT UPDATE(id) ON {reference} TO {self.writer};",
            "composite": f"CREATE TABLE business.{table}_parent "
                         "(a integer,b integer,PRIMARY KEY(a,b));"
                         f"CREATE TABLE business.{table}_fk (a integer,b integer,"
                         f"FOREIGN KEY(a,b) REFERENCES business.{table}_parent(a,b));"
                         f"GRANT SELECT ON business.{table}_parent,business.{table}_fk "
                         "TO db_agent_reader;",
            "expressions": f"CREATE SCHEMA {schema};"
                           f"CREATE FUNCTION {schema}.identity(integer) RETURNS integer "
                           "LANGUAGE sql IMMUTABLE AS 'SELECT $1';"
                           f"CREATE STATISTICS business.{table}_stats "
                           f"ON ({schema}.identity(id)) FROM {reference};",
            "constraint": f"CREATE SCHEMA {schema};"
                          f"CREATE FUNCTION {schema}.explode(integer) RETURNS boolean "
                          "LANGUAGE plpgsql IMMUTABLE AS $$BEGIN RAISE EXCEPTION "
                          "'probe expression evaluated'; END;$$;"
                          f"CREATE TABLE business.{table}_check (id integer "
                          f"CHECK ({schema}.explode(1)));"
                          f"GRANT SELECT ON business.{table}_check TO db_agent_reader;"
                          f"ANALYZE business.{table}_check;",
            "constraint_explain": "SET constraint_exclusion=on;EXPLAIN "
                                  f"SELECT id FROM business.{table}_check WHERE id=1;",
        }
        assert action in operations, "only the fixed probe operations are allowed"
        return operations[action]

    def try_action(self, action):
        result = process(ADMIN, "BEGIN;" + self.sql(action) + "COMMIT;")
        # SQL and driver details are deliberately not interpolated into assertions.
        match = re.search(r"(?:ERROR|FATAL):\s+([0-9A-Z]{5})\b", result.stderr)
        return result.returncode, match.group(1) if match else "", result.stdout.strip()

    def require(self, action):
        code, state, output = self.try_action(action)
        assert code == 0, f"fixed probe operation {action} failed; SQLSTATE={state}"
        return output

    def cleanup(self):
        if not self.created:
            return
        # Every name is generated by this fixture; no CASCADE or wildcard deletion.
        cleanup = "SET lock_timeout='1s';"
        for kind, name in reversed(self.related):
            assert NAME.fullmatch(name)
            if kind == "SCHEMA":
                continue
            if kind == "FUNCTION":
                cleanup += f"DROP FUNCTION {name}.identity(integer);"
            elif kind == "EXPLODE":
                cleanup += f"DROP FUNCTION {name}.explode(integer);"
            else:
                cleanup += f"DROP {kind} business.{name};"
        cleanup += f"DROP {self.kind} business.{self.table};"
        for kind, name in self.related:
            if kind == "SCHEMA":
                cleanup += f"DROP SCHEMA {name};"
        if self.writer_created:
            cleanup += f"REVOKE CONNECT ON DATABASE db_agent_pg FROM {self.writer};"
            cleanup += f"REVOKE USAGE ON SCHEMA business FROM {self.writer};"
            cleanup += f"DROP ROLE {self.writer};"
        result = process(ADMIN, cleanup)
        assert result.returncode == 0, "disposable PostgreSQL probe cleanup failed"
        assert self.require("exists") == "0", "disposable table cleanup was not confirmed"
        self.secret_file.unlink()


@pytest.fixture
def probe(tmp_path):
    settings = PostgreSQLSettings(_env_file=ROOT / ".env.postgres")
    local_target(settings)
    admin = Probe(tmp_path)
    assert admin.require("exists") == "0", "refusing to reuse an existing random table"
    admin.require("create")
    admin.created = True
    try:
        yield admin, settings.model_copy(update={"allowed_tables": (admin.table,)})
    finally:
        admin.cleanup()


def query(connector, sql, *, before_select=None, analysis=None, limits=None):
    service = QueryService(connector, analysis or AnalysisSettings(_env_file=None),
                           limits or QuerySettings(_env_file=None), before_select=before_select)
    return service.execute(sql)


@pytest.mark.parametrize("action", ["rls", "inheritance", "generated", "domain", "view"])
def test_real_unsupported_objects_are_refused(probe, action):
    admin, settings = probe
    admin.require(action)
    if action == "inheritance":
        admin.related.append(("TABLE", admin.table + "_child"))
    elif action == "view":
        admin.related.append(("VIEW", admin.table + "_view"))
        settings = settings.model_copy(update={"allowed_tables": (admin.table + "_view",)})
    connector = PostgreSQLConnector(settings)
    table = settings.allowed_tables[0]
    try:
        result = asyncio.run(query(connector, f"SELECT id FROM {table}"))
        assert result["decision"] == "UNKNOWN" and result["execution_status"] == "not_started"
        assert result["result"] is None and result["error"]["code"] == "UNSUPPORTED_TABLE"
    finally:
        if action == "domain":
            result = process(ADMIN, f"ALTER TABLE business.{admin.table} DROP COLUMN custom;"
                             f"DROP DOMAIN business.{admin.table}_domain;")
            assert result.returncode == 0


def test_real_expression_statistics_are_refused_even_with_function_in_other_schema(probe):
    admin, settings = probe
    admin.require("expressions")
    admin.related.extend([("SCHEMA", admin.schema),
                          ("FUNCTION", admin.schema),
                          ("STATISTICS", admin.table + "_stats")])
    result = asyncio.run(query(PostgreSQLConnector(settings), f"SELECT id FROM {admin.table}"))
    assert result["error"]["code"] == "UNSUPPORTED_TABLE", result
    assert result["execution_status"] == "not_started" and result["result"] is None


def test_real_check_function_cannot_run_during_planning(probe):
    admin, settings = probe
    admin.require("constraint")
    admin.related.extend([("SCHEMA", admin.schema),
                          ("EXPLODE", admin.schema),
                          ("TABLE", admin.table + "_check")])
    code, state, _ = admin.try_action("constraint_explain")
    assert code != 0 and state == "P0001", "unsafe planning control must evaluate the fixture CHECK"
    settings = settings.model_copy(update={"allowed_tables": (admin.table + "_check",)})
    result = asyncio.run(query(PostgreSQLConnector(settings),
                               f"SELECT id FROM {admin.table}_check WHERE id=1"))
    assert result["execution_status"] == "completed", json.dumps(result)
    assert result["result"]["rows"] == []


def test_real_composite_foreign_key_is_complete_and_scoped(probe):
    admin, settings = probe
    admin.require("composite")
    admin.related.extend([("TABLE", admin.table + "_parent"), ("TABLE", admin.table + "_fk")])
    settings = settings.model_copy(update={"allowed_tables": (
        admin.table + "_fk", admin.table + "_parent",
    )})
    details = asyncio.run(PostgreSQLConnector(settings).describe_table(admin.table + "_fk"))
    assert len(details["foreign_keys"]) == 1
    relation = details["foreign_keys"][0]
    assert relation["columns"] == ["a", "b"]
    assert relation["referenced_columns"] == ["a", "b"]
    assert relation["referenced_table"] == admin.table + "_parent"
    narrow = settings.model_copy(update={"allowed_tables": (admin.table + "_fk",)})
    details = asyncio.run(PostgreSQLConnector(narrow).describe_table(admin.table + "_fk"))
    assert details["foreign_keys"] == []


def test_admin_config_is_rejected_without_opening_connection():
    with pytest.raises(ValidationError):
        PostgreSQLSettings(_env_file=None, user="postgres", password="synthetic-unused")


@pytest.mark.parametrize("column_only", [False, True])
def test_real_writer_role_is_refused_on_probe_object(probe, column_only):
    admin, settings = probe
    admin.require("writer")
    admin.writer_created = True
    if column_only:
        admin.require("column_writer")
    settings = settings.model_copy(update={
        "user": admin.writer, "password": SecretStr(admin.password),
    })
    result = asyncio.run(query(PostgreSQLConnector(settings), f"SELECT id FROM {admin.table}"))
    assert result["decision"] == "BLOCK", result
    assert result["execution_status"] == "not_started"
    assert result["error"]["code"] == "PERMISSION_DENIED" and result["result"] is None


def test_real_readonly_transaction_lost_after_guard_is_refused(probe):
    admin, settings = probe

    async def lose_transaction(connection):
        await connection.execute("ROLLBACK")

    result = asyncio.run(query(PostgreSQLConnector(settings), f"SELECT id FROM {admin.table}",
                               before_select=lose_transaction))
    assert result["error"]["code"] == "TRANSACTION_STATE", result
    assert result["execution_status"] == "not_started" and result["result"] is None


def test_real_access_share_lock_survives_plan_and_guard(probe):
    admin, settings = probe
    observed = []

    async def contend(connection):
        code, state, _ = await asyncio.to_thread(admin.try_action, "alter")
        observed.append((code, state))

    result = asyncio.run(query(PostgreSQLConnector(settings), f"SELECT id FROM {admin.table}",
                               before_select=contend))
    assert observed and observed[0][0] != 0 and observed[0][1] == "55P03"
    assert result["execution_status"] == "completed" and result["result"]["rows"] == [[1]]
    admin.require("alter")  # The connection closed and released the lock after the query.


def test_real_object_replaced_before_lock_is_not_executed(probe):
    admin, settings = probe

    class ReplaceBeforeLock(PostgreSQLConnector):
        replaced = False

        async def _fetch(self, connection, statement, *args, **kwargs):
            if (not self.replaced and hasattr(statement, "as_string")
                    and statement.as_string(connection).startswith("LOCK TABLE ONLY")):
                self.replaced = True
                await asyncio.to_thread(admin.require, "replace")
            return await super()._fetch(connection, statement, *args, **kwargs)

    result = asyncio.run(query(ReplaceBeforeLock(settings), f"SELECT id FROM {admin.table}"))
    assert result["execution_status"] == "not_started" and result["result"] is None
    assert result["error"]["code"] == "OBJECT_CHANGED", result


def test_real_database_revoke_does_not_reuse_prior_success(probe):
    admin, settings = probe
    connector = PostgreSQLConnector(settings)
    first = asyncio.run(query(connector, f"SELECT id FROM {admin.table}"))
    assert first["execution_status"] == "completed"
    admin.require("revoke")
    second = asyncio.run(query(connector, f"SELECT id FROM {admin.table}"))
    assert second["decision"] == "BLOCK" and second["result"] is None
    assert second["execution_status"] == "not_started"


@pytest.mark.parametrize("revoke_after_read", [False, True])
def test_real_authorization_hook_revocation_discards_results(probe, monkeypatch, revoke_after_read):
    admin, settings = probe
    allowed = True

    def authorize():
        if not allowed:
            raise DatabaseError("PERMISSION_DENIED", "synthetic authorization revoked")

    async def guard(connection):
        nonlocal allowed
        if not revoke_after_read:
            allowed = False

    if revoke_after_read:
        from db_agent import postgres

        original = postgres.read_postgres_result

        async def read_then_revoke(cursor, limits):
            nonlocal allowed
            result = await original(cursor, limits)
            allowed = False
            return result

        monkeypatch.setattr(postgres, "read_postgres_result", read_then_revoke)
    connector = PostgreSQLConnector(settings, authorization_check=authorize)
    result = asyncio.run(query(connector, f"SELECT id FROM {admin.table}", before_select=guard))
    assert result["decision"] == "BLOCK" and result["result"] is None
    assert result["execution_status"] == ("unknown" if revoke_after_read else "not_started")


def test_before_select_only_vetoes_and_cannot_grant_blocked_sql(probe):
    admin, settings = probe
    called = []

    async def veto(connection):
        called.append(True)
        raise DatabaseError("PERMISSION_DENIED", "synthetic guard veto")

    connector = PostgreSQLConnector(settings)
    result = asyncio.run(query(connector, f"SELECT id FROM {admin.table}", before_select=veto))
    assert called == [True] and result["execution_status"] == "not_started"
    assert result["decision"] == "BLOCK" and result["result"] is None
    called.clear()
    result = asyncio.run(query(connector, f"DELETE FROM {admin.table}", before_select=veto))
    assert called == [] and result["decision"] == "BLOCK" and result["result"] is None


def test_real_execution_deadline_cleans_connection_and_does_not_claim_cancellation(probe):
    admin, settings = probe
    connector = PostgreSQLConnector(settings)
    limits = QuerySettings(_env_file=None, execution_timeout_seconds=0.000001)
    result = asyncio.run(query(connector, f"SELECT id FROM {admin.table}", limits=limits))
    assert result["execution_status"] == "unknown" and result["result"] is None
    assert result["error"]["code"] == "TIMEOUT", result
    admin.require("alter")
    following = asyncio.run(query(connector, f"SELECT id FROM {admin.table}"))
    assert following["execution_status"] == "completed" and following["result"]["rows"] == [[1]]


def test_real_table_lock_timeout_closes_waiter_and_next_query_recovers(probe):
    admin, settings = probe
    connector = PostgreSQLConnector(settings)

    async def exercise():
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("DB_AGENT_POSTGRES_")}
        blocker = await asyncio.create_subprocess_exec(
            *ADMIN, cwd=ROOT, env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            blocker.stdin.write((f"BEGIN;LOCK TABLE ONLY business.{admin.table} "
                                 "IN ACCESS EXCLUSIVE MODE;SELECT 'locked';\n").encode())
            await blocker.stdin.drain()
            assert await asyncio.wait_for(blocker.stdout.readline(), 3) == b"locked\n"
            result = await query(connector, f"SELECT id FROM {admin.table}",
                                 analysis=AnalysisSettings(_env_file=None, timeout_seconds=0.05))
            assert result["error"]["code"] == "TIMEOUT", result
            assert result["execution_status"] == "not_started" and result["result"] is None
        finally:
            if blocker.returncode is None:
                blocker.stdin.write(b"ROLLBACK;\n")
                await blocker.stdin.drain()
                blocker.stdin.close()
                try:
                    await asyncio.wait_for(blocker.wait(), 3)
                except TimeoutError:
                    blocker.kill()
                    await blocker.wait()
        assert blocker.returncode == 0, "fixed local lock holder failed"
        return await query(connector, f"SELECT id FROM {admin.table}")

    result = asyncio.run(exercise())
    assert result["execution_status"] == "completed" and result["result"]["rows"] == [[1]]


def test_real_cancellation_releases_locks_and_serial_queue_recovers(probe):
    admin, settings = probe
    connector = PostgreSQLConnector(settings)

    async def exercise():
        entered = asyncio.Event()

        async def wait_with_real_locks(connection):
            entered.set()
            await asyncio.Event().wait()

        first = asyncio.create_task(query(connector, f"SELECT id FROM {admin.table}",
                                          before_select=wait_with_real_locks))
        await asyncio.wait_for(entered.wait(), 3)
        second = await query(connector, f"SELECT id FROM {admin.table}",
                             limits=QuerySettings(_env_file=None, operation_timeout_seconds=0.03))
        assert second["error"]["code"] == "TIMEOUT" and second["execution_status"] == "not_started"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.to_thread(admin.require, "alter")
        return await query(connector, f"SELECT id FROM {admin.table}")

    started = time.monotonic()
    result = asyncio.run(exercise())
    assert time.monotonic() - started < 5
    assert result["execution_status"] == "completed" and result["result"]["rows"] == [[1]]
