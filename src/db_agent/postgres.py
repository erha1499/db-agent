"""PostgreSQL 18.6 reader: separate catalog, session, plan and streaming boundaries.

Only the configured ordinary heap tables and built-in column types are supported.
A trusted administrator owns database/schema definitions; runtime has no DDL/write
capability. An ordinary EXPLAIN is not a business query nor an execution grant.
"""

import asyncio
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager

import psycopg
from psycopg import sql as pgsql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row, tuple_row

from db_agent.config import PostgreSQLSettings
from db_agent.db import DatabaseError
from db_agent.policy import check_sql
from db_agent.postgres_plans import analyze_postgres_plan
from db_agent.postgres_results import read_postgres_result
from db_agent.results import ResultError

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
# Built-in scalar types only. A domain, enum or user type can invoke custom code.
_TYPE_OIDS = (16, 20, 21, 23, 25, 700, 701, 1042, 1043, 1082, 1083, 1114, 1184, 1700)


class PostgreSQLConnector:
    dialect = "postgres"
    policy_version = "postgres-select-v1"
    evidence_source = "postgres_explain_json"

    def __init__(self, settings: PostgreSQLSettings, *, authorization_check=None,
                 knowledge_scope: str | None = None):
        self._settings = settings.model_copy(deep=True)
        self._lock = asyncio.Lock()
        self._authorization_check = authorization_check
        self._knowledge_scope = knowledge_scope

    def _authorize(self):
        if self._authorization_check:
            self._authorization_check()

    @property
    def database(self):
        return self._settings.database

    @property
    def knowledge_scope(self):
        from db_agent.conversations import source_scope

        self._authorize()
        return self._knowledge_scope or source_scope(self._settings)

    @property
    def authorized_table_candidates(self):
        self._authorize()
        return tuple(sorted(self.validate_table(name) for name in self._settings.allowed_tables))

    def validate_table(self, table):
        self._authorize()
        if not isinstance(table, str) or not _IDENTIFIER.fullmatch(table):
            raise DatabaseError("INVALID_TABLE", "仅支持至多63字节的简单 ASCII 表名")
        if table not in self._settings.allowed_tables:
            raise DatabaseError("PERMISSION_DENIED", "目标表不在当前授权范围内")
        return table

    def check_sql(self, sql, limits):
        self._authorize()
        return check_sql(sql, self._settings.schema_name, self._settings.allowed_tables,
                         limits, dialect="postgres")

    def _bounded_result(self, value):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > (
            self._settings.max_metadata_bytes
        ):
            raise DatabaseError("RESULT_LIMIT", "元数据超过结果字节上限")
        return value

    async def _fetch(self, connection, query, params=(), all_rows=None):
        self._authorize()
        cursor = connection.cursor(row_factory=dict_row)
        await cursor.execute(query, params or None)
        rows = []
        if cursor.description:
            while (row := await cursor.fetchone()) is not None:
                rows.append(row)
                aggregate = [*(all_rows or []), *rows]
                if len(aggregate) > self._settings.max_metadata_rows:
                    raise DatabaseError("RESULT_LIMIT", "元数据超过结果行数上限")
                self._bounded_result(aggregate)
        await cursor.close()
        self._authorize()
        if all_rows is not None:
            all_rows.extend(rows)
        return rows

    async def _session(self, connection, *, isolation="read committed"):
        rows = await self._fetch(connection, """
            SELECT current_database() AS database_name, current_user AS current_name,
                   session_user AS session_name,
                   current_setting('server_version_num') AS version_num,
                   current_setting('transaction_read_only') AS read_only,
                   current_setting('transaction_isolation') AS isolation,
                   current_setting('standard_conforming_strings') AS standard_strings,
                   current_setting('TimeZone') AS timezone,
                   current_setting('search_path') AS search_path,
                   r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                     OR r.rolbypassrls AS privileged,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                           WHERE m.member = r.oid) AS member,
                   pg_catalog.has_database_privilege(current_database(), 'CREATE,TEMP') AS db_write,
                   pg_catalog.has_schema_privilege(%s, 'CREATE') AS schema_write,
                   pg_catalog.has_schema_privilege(%s, 'USAGE') AS schema_usage,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
                     JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                     WHERE n.nspname=%s) AS custom_functions,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_operator o
                     JOIN pg_catalog.pg_namespace n ON n.oid=o.oprnamespace
                     WHERE n.nspname=%s) AS custom_operators
            FROM pg_catalog.pg_roles r WHERE r.rolname=current_user
        """, (self._settings.schema_name,) * 4)
        if len(rows) != 1:
            raise DatabaseError("IDENTITY_UNCONFIRMED", "未能确认实际数据库身份")
        row = rows[0]
        if (row["database_name"] != self.database or row["current_name"] != self._settings.user
                or row["session_name"] != self._settings.user):
            raise DatabaseError("PERMISSION_DENIED", "数据库目标或实际身份与可信配置不匹配")
        if row["version_num"] != "180006":
            raise DatabaseError("UNSUPPORTED_SERVER", "当前 PostgreSQL 连接器仅验证了18.6")
        if any(row[k] is not False for k in ("privileged", "member", "db_write", "schema_write")):
            raise DatabaseError("PERMISSION_DENIED", "PostgreSQL 运行账号必须为最小只读角色")
        if row["schema_usage"] is not True:
            raise DatabaseError("PERMISSION_DENIED", "当前角色无授权 schema 使用权限")
        if row["custom_functions"] or row["custom_operators"]:
            raise DatabaseError("UNSUPPORTED_SCHEMA", "当前 schema 包含未支持的自定义函数或运算符")
        if (connection.info.transaction_status != TransactionStatus.INTRANS
                or row["read_only"] != "on" or row["isolation"] != isolation
                or row["standard_strings"] != "on" or row["timezone"] != "UTC"
                or row["search_path"] != 'pg_catalog, "' + self._settings.schema_name + '"'):
            raise DatabaseError("TRANSACTION_STATE", "未确认 PostgreSQL 显式只读事务和固定会话设置")
        return "18.6"

    @asynccontextmanager
    async def _connection(self, timeout_seconds=None, *, paired=False):
        budget = self._settings.metadata_timeout_seconds if timeout_seconds is None else (
            timeout_seconds
        )
        connection = None
        try:
            async with asyncio.timeout(budget):
                async with self._lock:
                    self._authorize()
                    # libpq service/options/hostaddr cannot silently replace the trusted target.
                    if any(os.environ.get(key) for key in (
                        "PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS", "PGPASSFILE",
                    )):
                        raise DatabaseError("CONFIGURATION_ERROR", "libpq 配置覆盖了可信目标")
                    connection = await psycopg.AsyncConnection.connect(
                        host=self._settings.host, port=self._settings.port,
                        dbname=self.database, user=self._settings.user,
                        password=self._settings.password.get_secret_value(),
                        connect_timeout=math.ceil(self._settings.connect_timeout_seconds),
                        options="-c default_transaction_read_only=on -c search_path=pg_catalog",
                        client_encoding="UTF8", application_name="db-agent", autocommit=True,
                        prepare_threshold=None,
                    )
                    await self._fetch(connection, "SELECT pg_catalog.set_config("
                                      "'statement_timeout',"
                                      " %s, false)", (str(max(1, math.ceil(budget * 1000))),))
                    await self._fetch(connection, "SELECT pg_catalog.set_config('lock_timeout',"
                                      " %s, false)", (str(max(1, math.ceil(budget * 1000))),))
                    for statement in (
                        "SET TIME ZONE 'UTC'", "SET standard_conforming_strings = on",
                        "SET max_parallel_workers_per_gather = 0", "SET jit = off",
                        "SET row_security = off", "SET cursor_tuple_fraction = 1.0",
                    ):
                        await self._fetch(connection, statement)
                    await self._fetch(connection, "SELECT pg_catalog.set_config('search_path',"
                                      " %s, false)",
                                      ('pg_catalog, "' + self._settings.schema_name + '"',))
                    await self._fetch(connection, "BEGIN ISOLATION LEVEL " + (
                        "REPEATABLE READ" if paired else "READ COMMITTED"
                    ) + " READ ONLY")
                    await self._session(connection, isolation=(
                        "repeatable read" if paired else "read committed"
                    ))
                    yield connection
        except TimeoutError:
            raise DatabaseError("TIMEOUT", "数据库请求超时，连接已清理；服务器状态未确认") from None
        except psycopg.Error as exc:
            state = exc.sqlstate or ""
            if state.startswith("28") or state == "42501":
                code, message = "PERMISSION_DENIED", "数据库认证或权限校验失败"
            elif state in {"57014", "55P03"}:
                code, message = "TIMEOUT", "数据库查询或锁等待超过预算"
            elif state.startswith("42"):
                code, message = "SQL_ERROR", "数据库拒绝语法或对象引用"
            elif state.startswith("22"):
                code, message = "DATA_ERROR", "查询表达式或结果类型无法计算"
            else:
                code, message = "DATABASE_ERROR", "未取得完整数据库响应"
            raise DatabaseError(code, message) from None
        finally:
            if connection is not None:
                # No commit, cursor draining or reconnect/replay, including cancellation.
                await connection.close()

    async def _objects(self, connection, tables, *, lock=False):
        evidence = {}
        for table in sorted(tables):
            self.validate_table(table)
            rows = await self._fetch(connection, """
                SELECT c.oid::bigint AS oid, c.relkind, c.relpersistence,
                       c.relrowsecurity, c.relforcerowsecurity, c.relhasrules,
                       c.reltuples AS estimated_rows, a.amname,
                       pg_catalog.has_table_privilege(c.oid, 'SELECT') AS can_select,
                       pg_catalog.has_table_privilege(c.oid,
                         'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,MAINTAIN') AS can_write,
                       c.relowner = (SELECT oid FROM pg_catalog.pg_roles
                                    WHERE rolname=current_user) AS owner,
                       EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i
                         WHERE i.inhparent=c.oid OR i.inhrelid=c.oid) AS inherited,
                       EXISTS (SELECT 1 FROM pg_catalog.pg_attribute x
                         WHERE x.attrelid=c.oid AND x.attnum>0 AND NOT x.attisdropped
                           AND (x.atttypid <> ALL(%s) OR x.attgenerated<>'')) AS unsafe_columns,
                       EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                         JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid
                         JOIN pg_catalog.pg_am ia ON ia.oid=ic.relam
                         WHERE i.indrelid=c.oid AND (ia.amname<>'btree'
                           OR i.indexprs IS NOT NULL OR i.indpred IS NOT NULL
                           OR NOT i.indisvalid OR NOT i.indisready
                           OR EXISTS (SELECT 1 FROM pg_catalog.pg_opclass op
                             JOIN pg_catalog.pg_namespace ns ON ns.oid=op.opcnamespace
                             WHERE op.oid=ANY(i.indclass) AND ns.nspname<>'pg_catalog')))
                         AS unsafe_indexes
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_catalog.pg_am a ON a.oid=c.relam
                WHERE n.nspname=%s AND c.relname=%s
                LIMIT 1
            """, (list(_TYPE_OIDS), self._settings.schema_name, table))
            if len(rows) != 1:
                raise DatabaseError("TABLE_NOT_FOUND", "授权表不存在或不可见")
            row = rows[0]
            if row["can_select"] is not True or row["can_write"] or row["owner"]:
                raise DatabaseError("PERMISSION_DENIED", "目标表必须仅授予当前账号只读权限")
            if (row["relkind"] != "r" or row["relpersistence"] != "p" or row["amname"] != "heap"
                    or any(row[k] for k in ("relrowsecurity", "relforcerowsecurity", "relhasrules",
                                           "inherited", "unsafe_columns", "unsafe_indexes"))):
                raise DatabaseError("UNSUPPORTED_TABLE", "只支持无RLS/继承/生成列的普通heap基础表、"
                                    "内建标量类型和简单内建btree索引")
            evidence[table] = row
        if lock:
            # Catalog validation precedes lock acquisition. ONLY avoids inherited targets.
            # Revalidate OIDs and properties under retained ACCESS SHARE locks before EXPLAIN.
            for table in sorted(tables):
                await self._fetch(
                    connection, pgsql.SQL("LOCK TABLE ONLY {}.{} IN ACCESS SHARE MODE")
                                  .format(pgsql.Identifier(self._settings.schema_name),
                                          pgsql.Identifier(table)))
            locked = await self._objects(connection, tables)
            if any(locked[t]["oid"] != evidence[t]["oid"] for t in tables):
                raise DatabaseError("OBJECT_CHANGED", "目标表在取证期间发生变化")
            return locked
        return evidence

    async def check(self):
        async with self._connection() as connection:
            version = await self._session(connection)
            return {"connection_ok": True, "database": self.database,
                    "schema": self._settings.schema_name, "dialect": self.dialect,
                    "server_version": version, "readonly_identity_verified": True}

    async def list_tables(self):
        allowed = self.authorized_table_candidates
        result = {"database": self.database, "dialect": self.dialect,
                  "schema": self._settings.schema_name, "tables": []}
        if not allowed:
            return result
        async with self._connection() as connection:
            rows = await self._fetch(connection, """
                SELECT c.relname AS name, 'BASE TABLE' AS type
                FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname=ANY(%s) AND c.relkind='r'
                  AND pg_catalog.has_table_privilege(c.oid, 'SELECT')
                ORDER BY c.relname LIMIT %s
            """, (self._settings.schema_name, list(allowed), self._settings.max_metadata_rows + 1))
            # The list advertises objects that actually meet the supported-object contract.
            await self._objects(connection, [row["name"] for row in rows])
            result["tables"] = rows
        return self._bounded_result(result)

    async def describe_table(self, table):
        self.validate_table(table)
        async with self._connection() as connection:
            return await self.describe_for_query(connection, table)

    async def describe_for_query(self, connection, table):
        async with asyncio.timeout(self._settings.metadata_timeout_seconds):
            await self._objects(connection, [table], lock=True)
            return await self._describe(connection, table)

    async def _describe(self, connection, table):
        table = self.validate_table(table)
        all_rows = []
        result = {"database": self.database, "dialect": self.dialect,
                  "schema": self._settings.schema_name, "table": table}
        result["columns"] = await self._fetch(connection, """
            SELECT a.attname AS name, pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                   CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS nullable,
                   a.attnum AS position
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY a.attnum LIMIT %s
        """, (self._settings.schema_name, table, self._settings.max_metadata_rows + 1), all_rows)
        result["indexes"] = await self._fetch(connection, """
            SELECT ic.relname AS name, i.indisunique AS unique, a.attname AS column,
                   k.ordinality::integer AS position, am.amname AS type
            FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class c ON c.oid=i.indrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid
            JOIN pg_catalog.pg_am am ON am.oid=ic.relam
            CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ordinality)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
            WHERE n.nspname=%s AND c.relname=%s
            ORDER BY ic.relname,k.ordinality LIMIT %s
        """, (self._settings.schema_name, table, self._settings.max_metadata_rows + 1), all_rows)
        rows = await self._fetch(connection, """
            SELECT f.conname AS name, a.attname AS column_name, b.attname AS referenced_column,
                   t.relname AS referenced_table, k.ordinality::integer AS position,
                   cardinality(f.conkey) AS component_count
            FROM pg_catalog.pg_constraint f
            JOIN pg_catalog.pg_class c ON c.oid=f.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_catalog.pg_class t ON t.oid=f.confrelid
            JOIN pg_catalog.pg_namespace tn ON tn.oid=t.relnamespace
            CROSS JOIN LATERAL unnest(f.conkey, f.confkey)
              WITH ORDINALITY k(source_num,target_num,ordinality)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.source_num
            JOIN pg_catalog.pg_attribute b ON b.attrelid=t.oid AND b.attnum=k.target_num
            WHERE n.nspname=%s AND c.relname=%s AND f.contype='f'
              AND tn.nspname=%s AND t.relname=ANY(%s) AND t.relkind='r'
              AND pg_catalog.has_table_privilege(t.oid, 'SELECT')
            ORDER BY f.conname,k.ordinality LIMIT %s
        """, (self._settings.schema_name, table, self._settings.schema_name,
              list(self._settings.allowed_tables), self._settings.max_metadata_rows + 1), all_rows)
        grouped = {}
        for row in rows:
            grouped.setdefault(row["name"], []).append(row)
        result["foreign_keys"] = []
        for name, parts in grouped.items():
            count = len(parts)
            if (any(p["component_count"] != count for p in parts)
                    or [p["position"] for p in parts] != list(range(1, count + 1))
                    or len({p["referenced_table"] for p in parts}) != 1):
                raise DatabaseError("INVALID_METADATA", "外键元数据不完整")
            result["foreign_keys"].append({"name": name,
                "columns": [p["column_name"] for p in parts],
                "referenced_table": parts[0]["referenced_table"],
                "referenced_columns": [p["referenced_column"] for p in parts]})
        result["foreign_keys_scope"] = "current_schema_authorized_tables"
        self._authorize()
        return self._bounded_result(result)

    async def _plan(self, connection, sql, checked, limits, *, paired=False):
        version = await self._session(connection, isolation=(
            "repeatable read" if paired else "read committed"
        ))
        objects = await self._objects(connection, checked.tables, lock=True)
        self._authorize()
        cursor = connection.cursor(row_factory=tuple_row)
        await cursor.execute("EXPLAIN (FORMAT JSON, VERBOSE TRUE, ANALYZE FALSE) " + sql, None)
        row = await cursor.fetchone()
        if row is None or len(row) != 1 or await cursor.fetchone() is not None:
            raise DatabaseError("INVALID_PLAN", "未返回唯一 PostgreSQL JSON 计划")
        await cursor.close()
        self._authorize()
        plan = row[0]
        if len(json.dumps(plan, ensure_ascii=False).encode()) > limits.max_plan_bytes:
            raise DatabaseError("RESULT_LIMIT", "执行计划超过字节预算")
        assessment = analyze_postgres_plan(
            plan, limits, checked.aliases,
            relation_rows={table: value["estimated_rows"] for table, value in objects.items()},
            expected_schema=self._settings.schema_name,
        )
        return plan, assessment, version

    async def explain_checked(self, sql, limits):
        checked = self.check_sql(sql, limits)
        result = {"check": checked, "plan": None, "server_version": None, "assessment": None}
        if checked.decision != "ALLOW":
            return result
        async with self._connection(limits.timeout_seconds) as connection:
            plan, assessment, version = await self._plan(connection, sql, checked, limits)
            result.update(plan=plan, assessment=assessment, server_version=version)
        return result

    async def execute_checked(self, sql, analysis_limits, query_limits, *, before_select=None):
        return (await self._execute((sql,), analysis_limits, query_limits,
                                    before_select=before_select))[0]

    async def compare_checked(self, original, candidate, analysis_limits, query_limits,
                              *, before_select=None):
        return await self._execute((original, candidate), analysis_limits, query_limits,
                                   before_select=before_select)

    async def _execute(self, statements, analysis_limits, query_limits, *, before_select=None):
        started = time.monotonic()
        paired = len(statements) == 2
        outcomes = [{"check": c, "assessment": None, "server_version": None, "result": None,
                     "decision": c.decision, "execution_status": "not_started", "error": None,
                     "select_duration_ms": None}
                    for sql in statements for c in (self.check_sql(sql, analysis_limits),)]
        if any(o["decision"] != "ALLOW" for o in outcomes):
            return outcomes
        outcome, phase = outcomes[0], "analysis"
        try:
            remaining = query_limits.operation_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise DatabaseError("TIMEOUT", "查询总预算耗尽，业务SQL未派发")
            async with self._connection(remaining, paired=paired) as connection:
                for sql, outcome in zip(statements, outcomes, strict=True):
                    phase = "analysis"
                    deadline = asyncio.get_running_loop().time() + analysis_limits.timeout_seconds
                    async with asyncio.timeout_at(deadline):
                        checked = self.check_sql(sql, analysis_limits)
                        outcome.update(check=checked, decision=checked.decision)
                        if checked.decision != "ALLOW":
                            return outcomes
                        await self._fetch(connection, "SELECT pg_catalog.set_config("
                                          "'statement_timeout', %s, true)",
                                          (str(max(1, math.ceil(analysis_limits.timeout_seconds
                                                               * 1000))),))
                        _, assessment, version = await self._plan(
                            connection, sql, checked, analysis_limits, paired=paired,
                        )
                        outcome.update(assessment=assessment, server_version=version,
                                       decision=assessment.decision)
                        if assessment.decision != "ALLOW":
                            return outcomes
                        await self._objects(connection, checked.tables)
                        if before_select is not None:
                            await before_select(connection)
                        await self._session(connection, isolation=(
                            "repeatable read" if paired else "read committed"
                        ))
                    await self._fetch(connection, "SELECT pg_catalog.set_config("
                                      "'statement_timeout', %s, true)",
                                      (str(max(1, math.ceil(query_limits.execution_timeout_seconds
                                                           * 1000))),))
                    self._authorize()
                    phase = "execution"
                    outcome["execution_status"] = "unknown"
                    select_started = time.monotonic()
                    async with asyncio.timeout(query_limits.execution_timeout_seconds):
                        # A NO SCROLL, WITHOUT HOLD server cursor streams only requested rows.
                        cursor = connection.cursor(name="db_agent_result", row_factory=tuple_row,
                                                   scrollable=False, withhold=False)
                        await cursor.execute(sql, None)
                        result = await read_postgres_result(cursor, query_limits)
                        self._authorize()
                        if not result["truncated"]:
                            await cursor.close()
                        outcome.update(result=result,
                                       execution_status="truncated" if result["truncated"]
                                       else "completed",
                                       select_duration_ms=round((time.monotonic()-select_started)
                                                                * 1000, 3))
                    if outcome["execution_status"] != "completed":
                        return outcomes
        except (DatabaseError, ResultError) as exc:
            if exc.code == "PERMISSION_DENIED":
                outcome["decision"] = "BLOCK"
            elif phase == "analysis":
                outcome["decision"] = "UNKNOWN"
            outcome.update(error={"code": exc.code, "message": exc.message}, result=None)
            if outcome["execution_status"] != "not_started":
                outcome["execution_status"] = "unknown"
        except TimeoutError:
            if phase == "analysis":
                outcome["decision"] = "UNKNOWN"
            outcome.update(error={"code": "TIMEOUT", "message": "查询阶段超过预算，连接已清理"},
                           result=None)
            if outcome["execution_status"] != "not_started":
                outcome["execution_status"] = "unknown"
        return outcomes
