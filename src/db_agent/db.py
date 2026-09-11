"""受控 MySQL 连接器：元数据、普通 EXPLAIN 与强制预检的只读 SELECT。"""

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

import aiomysql

from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings
from db_agent.plans import analyze_plan
from db_agent.policy import SqlCheck, check_sql
from db_agent.results import ResultError, read_query_result

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_READ_ONLY_TRANSACTION_STATUS = 0x2001  # MySQL IN_TRANS | IN_TRANS_READONLY.
_SUPPORTED_SQL_MODES = frozenset(
    {
        "ALLOW_INVALID_DATES", "ERROR_FOR_DIVISION_BY_ZERO", "NO_AUTO_VALUE_ON_ZERO",
        "NO_DIR_IN_CREATE", "NO_ENGINE_SUBSTITUTION", "NO_UNSIGNED_SUBTRACTION",
        "NO_ZERO_DATE", "NO_ZERO_IN_DATE", "ONLY_FULL_GROUP_BY", "STRICT_ALL_TABLES",
        "STRICT_TRANS_TABLES", "TIME_TRUNCATE_FRACTIONAL", "TRADITIONAL",
    }
)


def _reject_json_constant(value: str):
    raise ValueError("non-finite JSON number")


class DatabaseError(RuntimeError):
    """可向 CLI 或模型返回的数据库错误，不包含驱动原文或连接信息。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class MetadataConnector:
    """每个实例顺序处理受控数据库请求，总超时预算包括排队。"""

    def __init__(
        self, settings: DatabaseSettings, *, authorization_check: Callable[[], None] | None = None,
    ):
        self._settings = settings.model_copy(deep=True)
        self._lock = asyncio.Lock()
        self._authorization_check = authorization_check

    def _authorize(self):
        if self._authorization_check:
            self._authorization_check()

    @property
    def database(self) -> str:
        return self._settings.database

    @property
    def knowledge_scope(self) -> str:
        """Opaque source/account/allowlist binding; never exposes credentials."""
        from db_agent.conversations import source_scope

        return source_scope(self._settings)

    @property
    def authorized_table_candidates(self) -> tuple[str, ...]:
        """Configured candidates only; existence and structure require metadata reads."""
        return tuple(sorted(self.validate_table(name) for name in self._settings.allowed_tables))

    def check_sql(self, sql: str, limits: AnalysisSettings) -> SqlCheck:
        """Check the current trusted scope without accessing the database."""
        self._authorize()
        return check_sql(sql, self.database, self._settings.allowed_tables, limits)

    async def explain_checked(self, sql: str, limits: AnalysisSettings) -> dict:
        """Recheck every call, then obtain a bounded MySQL 8.4 version-1 JSON plan."""
        started = time.monotonic()
        checked = self.check_sql(sql, limits)
        result = {"check": checked, "plan": None, "server_version": None}
        if checked.decision != "ALLOW":
            return result
        remaining = limits.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise DatabaseError("TIMEOUT", "SQL 分析超过时间预算，未请求执行计划")
        async with self._connection(timeout_seconds=remaining) as connection:
            plan, version = await self._collect_plan(connection, sql, checked, limits)
            result.update(plan=plan, server_version=version)
            return result

    async def execute_checked(
        self, sql: str, analysis_limits: AnalysisSettings, query_limits: QuerySettings,
        *, before_select=None,
    ) -> dict:
        """Plan and execute the current SQL in one connection; no cached approval input."""
        started = time.monotonic()
        checked = self.check_sql(sql, analysis_limits)
        outcome = {
            "check": checked, "assessment": None, "server_version": None, "result": None,
            "decision": checked.decision, "execution_status": "not_started", "error": None,
        }
        if checked.decision != "ALLOW":
            return outcome
        phase = "analysis"
        try:
            remaining = query_limits.operation_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise DatabaseError("TIMEOUT", "查询操作超过总时间预算，尚未派发业务 SQL")
            async with self._connection(timeout_seconds=remaining) as connection:
                analysis_deadline = (
                    asyncio.get_running_loop().time() + analysis_limits.timeout_seconds
                )
                async with asyncio.timeout_at(analysis_deadline):
                    await self._fetch(connection, "SET SESSION time_zone = '+00:00'", (), [])
                    await self._fetch(
                        connection, "SET SESSION transaction_isolation = 'READ-COMMITTED'", (), [],
                    )
                    await self._fetch(
                        connection, "SET SESSION lock_wait_timeout = %s",
                        (max(1, math.ceil(min(analysis_limits.timeout_seconds, remaining))),), [],
                    )
                    await self._fetch(
                        connection, "SET SESSION max_execution_time = %s",
                        (max(1, math.ceil(analysis_limits.timeout_seconds * 1000)),), [],
                    )
                    await self._fetch(connection, "START TRANSACTION READ ONLY", (), [])
                    self._require_readonly_transaction(connection)
                    plan, version = await self._collect_plan(
                        connection, sql, checked, analysis_limits, require_innodb=True,
                    )
                    assessment = analyze_plan(plan, analysis_limits, checked.aliases)
                    outcome.update(
                        assessment=assessment, server_version=version, decision=assessment.decision,
                    )
                    if assessment.decision != "ALLOW":
                        return outcome
                    # Recheck object kind on the same transaction before executing.
                    await self._validate_analysis_tables(connection, checked, require_innodb=True)
                await self._fetch(
                    connection, "SET SESSION max_execution_time = %s",
                    (max(1, math.ceil(query_limits.execution_timeout_seconds * 1000)),), [],
                )
                # aiomysql refreshes server_status on this OK packet, not SELECT EOF.
                self._require_readonly_transaction(connection)
                cursor = await connection.cursor(aiomysql.SSCursor)
                if before_select is not None:
                    # The veto shares the original analysis deadline; it adds no budget.
                    async with asyncio.timeout_at(analysis_deadline):
                        await before_select(connection)
                    self._require_readonly_transaction(connection)
                self._authorize()
                phase = "execution"
                async with asyncio.timeout(query_limits.execution_timeout_seconds):
                    outcome["execution_status"] = "unknown"
                    # None preserves literal % characters; the SQL is never rewritten.
                    await cursor.execute(sql, None)
                    result = await read_query_result(cursor, query_limits)
                    self._authorize()
                    if result["server_statement_status"] == "completed":
                        await cursor.close()
                    outcome.update(
                        result=result,
                        execution_status="truncated" if result["truncated"] else "completed",
                    )
        except (DatabaseError, ResultError) as exc:
            if exc.code == "PERMISSION_DENIED":
                outcome["decision"] = "BLOCK"
            elif phase == "analysis":
                outcome["decision"] = "UNKNOWN"
            outcome["error"] = {"code": exc.code, "message": exc.message}
            outcome["result"] = None
            if outcome["execution_status"] != "not_started":
                outcome["execution_status"] = "unknown"
        except TimeoutError:
            if phase == "analysis":
                outcome["decision"] = "UNKNOWN"
            outcome["error"] = {
                "code": "TIMEOUT", "message": "查询阶段超过时间预算，已停止等待并清理连接。",
            }
            outcome["result"] = None
            if outcome["execution_status"] != "not_started":
                outcome["execution_status"] = "unknown"
        # CancelledError is intentionally not caught; _connection still closes the socket.
        return outcome

    @staticmethod
    def _require_readonly_transaction(connection) -> None:
        status = getattr(connection, "server_status", None)
        if not isinstance(status, int) or status & _READ_ONLY_TRANSACTION_STATUS != (
            _READ_ONLY_TRANSACTION_STATUS
        ):
            raise DatabaseError("TRANSACTION_STATE", "未能确认当前连接处于显式只读事务")

    async def _collect_plan(
        self, connection, sql: str, checked: SqlCheck, limits: AnalysisSettings,
        *, require_innodb: bool = False,
    ) -> tuple[dict, str]:
        session = await self._fetch(
            connection,
            "SELECT VERSION() AS server_version, DATABASE() AS database_name, "
            "@@SESSION.sql_mode AS sql_mode",
            (), [],
        )
        version = self._validate_analysis_session(session)
        for statement in (
            "SET SESSION explain_json_format_version = 1",
            "SET SESSION end_markers_in_json = OFF",
        ):
            await self._fetch(connection, statement, (), [])
        formats = await self._fetch(
            connection,
            "SELECT @@SESSION.explain_json_format_version AS json_format_version, "
            "@@SESSION.end_markers_in_json AS end_markers",
            (), [],
        )
        if (
            len(formats) != 1 or formats[0].get("json_format_version") != 1
            or formats[0].get("end_markers") != 0
        ):
            raise DatabaseError("UNSUPPORTED_PLAN_FORMAT", "未能确认 MySQL JSON 执行计划版本 1")
        await self._validate_analysis_tables(connection, checked, require_innodb=require_innodb)
        return await self._read_plan(connection, sql, limits), version

    async def _validate_analysis_tables(
        self, connection, checked: SqlCheck, *, require_innodb: bool = False,
    ) -> None:
        for table in checked.tables:
            self.validate_table(table)
            engine_column = ", ENGINE AS engine" if require_innodb else ""
            rows = await self._fetch(
                connection,
                f"SELECT TABLE_TYPE AS type{engine_column} FROM information_schema.TABLES "
                "WHERE CAST(TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY) "
                "AND CAST(TABLE_NAME AS BINARY) = CAST(%s AS BINARY) LIMIT 1",
                (self.database, table), [],
            )
            if not rows:
                raise DatabaseError("TABLE_NOT_FOUND", "授权表不存在或当前数据库账号不可见")
            if rows[0].get("type") != "BASE TABLE":
                raise DatabaseError("UNSUPPORTED_TABLE", "SQL 分析仅支持基础表，不支持视图等对象")
            if require_innodb and rows[0].get("engine") != "InnoDB":
                raise DatabaseError("UNSUPPORTED_ENGINE", "查询执行仅支持 InnoDB 基础表")

    def _validate_analysis_session(self, rows: list) -> str:
        if len(rows) != 1 or rows[0].get("database_name") != self.database:
            raise DatabaseError("DATABASE_MISMATCH", "SQL 分析连接的目标数据库不匹配")
        version = rows[0].get("server_version")
        version_match = (
            re.fullmatch(r"(8\.4\.\d+)(?:[-+][A-Za-z0-9._-]+)?", version)
            if isinstance(version, str) else None
        )
        if version_match is None:
            raise DatabaseError("UNSUPPORTED_SERVER", "SQL 分析当前仅支持 MySQL 8.4")
        if "mariadb" in version.casefold():
            raise DatabaseError("UNSUPPORTED_SERVER", "SQL 分析当前仅支持 MySQL 8.4")
        modes = rows[0].get("sql_mode")
        if not isinstance(modes, str) or not {
            mode.strip().upper() for mode in modes.split(",") if mode.strip()
        }.issubset(_SUPPORTED_SQL_MODES):
            raise DatabaseError("UNSUPPORTED_SQL_MODE", "当前会话 SQL 模式与静态解析规则不兼容")
        return version_match.group(1)

    async def _read_plan(self, connection, sql: str, limits: AnalysisSettings) -> dict:
        cursor = await connection.cursor()
        # None avoids driver %-interpolation of literal SQL LIKE patterns.
        await cursor.execute("EXPLAIN FORMAT=JSON " + sql, None)
        row = await cursor.fetchone()
        if not isinstance(row, dict) or set(row) != {"EXPLAIN"}:
            raise DatabaseError("INVALID_PLAN", "数据库未返回支持的 JSON 执行计划")
        raw = row["EXPLAIN"]
        if not isinstance(raw, str):
            raise DatabaseError("INVALID_PLAN", "数据库未返回支持的 JSON 执行计划")
        if len(raw.encode("utf-8")) > limits.max_plan_bytes:
            raise DatabaseError("RESULT_LIMIT", "执行计划超过字节上限")
        try:
            plan = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            raise DatabaseError("INVALID_PLAN", "数据库返回的 JSON 执行计划无法解析") from None
        if not isinstance(plan, dict) or not isinstance(plan.get("query_block"), dict):
            raise DatabaseError("UNSUPPORTED_PLAN_FORMAT", "数据库返回的执行计划不是支持的版本 1")
        pending, nodes = [plan], 0
        while pending:
            value = pending.pop()
            nodes += 1
            if nodes > limits.max_plan_nodes:
                raise DatabaseError("RESULT_LIMIT", "执行计划超过 JSON 值节点上限")
            if isinstance(value, (dict, list)):
                pending.extend(value.values() if isinstance(value, dict) else value)
            if nodes + len(pending) > limits.max_plan_nodes:
                raise DatabaseError("RESULT_LIMIT", "执行计划超过 JSON 值节点上限")
        if await cursor.fetchone() is not None:
            raise DatabaseError("INVALID_PLAN", "数据库返回了多行执行计划")
        await cursor.close()
        return plan

    def validate_table(self, table: str) -> str:
        if not isinstance(table, str) or not _IDENTIFIER.fullmatch(table):
            raise DatabaseError("INVALID_TABLE", "仅支持不带库名前缀的简单表名")
        if table not in self._settings.allowed_tables:
            raise DatabaseError("PERMISSION_DENIED", "目标表不在当前授权范围内")
        return table

    async def check(self) -> dict:
        async with self._connection() as connection:
            rows = await self._fetch(
                connection,
                "SELECT 1 AS connection_ok, VERSION() AS server_version, "
                "DATABASE() AS database_name",
                (),
                [],
            )
            if (
                len(rows) != 1
                or rows[0]["database_name"] != self._settings.database
                or rows[0]["connection_ok"] != 1
            ):
                raise DatabaseError("DATABASE_ERROR", "数据库连接校验未返回预期结果")
            return self._bounded_result(
                {
                    "connection_ok": rows[0]["connection_ok"] == 1,
                    "server_version": rows[0]["server_version"],
                    "database": rows[0]["database_name"],
                }
            )

    async def list_tables(self) -> dict:
        allowed = tuple(self.validate_table(table) for table in self._settings.allowed_tables)
        result = {"database": self._settings.database, "tables": []}
        if not allowed:
            return self._bounded_result(result)
        placeholders = ", ".join("%s" for _ in allowed)
        # 仅占位符数量来自配置；表名全部作为值传入，并使用二进制精确比较。
        query = f"""
            SELECT TABLE_NAME AS name, TABLE_TYPE AS type
            FROM information_schema.TABLES
            WHERE CAST(TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)
              AND CAST(TABLE_NAME AS BINARY) IN ({placeholders})
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            LIMIT %s
        """
        async with self._connection() as connection:
            result["tables"] = await self._fetch(
                connection,
                query,
                (self._settings.database, *allowed, self._settings.max_metadata_rows + 1),
                [],
            )
            return self._bounded_result(result)

    async def describe_table(self, table: str) -> dict:
        self.validate_table(table)
        async with self._connection() as connection:
            return await self._describe_on_connection(connection, table)

    async def describe_for_query(self, connection, table: str) -> dict:
        """Internal service hook: metadata on the already guarded SELECT connection."""
        async with asyncio.timeout(self._settings.metadata_timeout_seconds):
            return await self._describe_on_connection(connection, table)

    async def _describe_on_connection(self, connection, table: str) -> dict:
        table = self.validate_table(table)
        allowed = tuple(self.validate_table(name) for name in self._settings.allowed_tables)
        result = {"database": self._settings.database, "table": table}
        all_rows = []
        tables = await self._fetch(
            connection,
            """
                SELECT TABLE_TYPE AS type
                FROM information_schema.TABLES
                WHERE CAST(TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)
                  AND CAST(TABLE_NAME AS BINARY) = CAST(%s AS BINARY)
                LIMIT 1
            """,
            (self._settings.database, table),
            [],
        )
        if not tables:
            raise DatabaseError("TABLE_NOT_FOUND", "授权表不存在或当前数据库账号不可见")
        if tables[0]["type"] != "BASE TABLE":
            raise DatabaseError("UNSUPPORTED_TABLE", "当前仅支持基础表，不支持视图等对象")
        result["columns"] = await self._fetch(
            connection,
            """
                SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type,
                       IS_NULLABLE AS nullable, ORDINAL_POSITION AS position
                FROM information_schema.COLUMNS
                WHERE CAST(TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)
                  AND CAST(TABLE_NAME AS BINARY) = CAST(%s AS BINARY)
                ORDER BY ORDINAL_POSITION
                LIMIT %s
            """,
            (self._settings.database, table, self._settings.max_metadata_rows + 1),
            all_rows,
        )
        if not result["columns"]:
            raise DatabaseError("TABLE_NOT_FOUND", "授权表不存在或当前数据库账号不可见")
        indexes = await self._fetch(
            connection,
            """
                SELECT INDEX_NAME AS name, NON_UNIQUE AS non_unique,
                       COLUMN_NAME AS column_name, SEQ_IN_INDEX AS position,
                       INDEX_TYPE AS type
                FROM information_schema.STATISTICS
                WHERE CAST(TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)
                  AND CAST(TABLE_NAME AS BINARY) = CAST(%s AS BINARY)
                ORDER BY INDEX_NAME, SEQ_IN_INDEX
                LIMIT %s
            """,
            (
                self._settings.database,
                table,
                self._settings.max_metadata_rows - len(all_rows) + 1,
            ),
            all_rows,
        )
        result["indexes"] = [
            {
                "name": row["name"],
                "unique": row["non_unique"] == 0,
                "column": row["column_name"],
                "position": row["position"],
                "type": row["type"],
            }
            for row in indexes
        ]
        placeholders = ", ".join("%s" for _ in allowed)
        foreign_key_rows = await self._fetch(
            connection,
            f"""
                SELECT k.CONSTRAINT_NAME AS name, k.COLUMN_NAME AS column_name,
                       k.ORDINAL_POSITION AS position,
                       k.REFERENCED_TABLE_SCHEMA AS referenced_schema,
                       k.REFERENCED_TABLE_NAME AS referenced_table,
                       k.REFERENCED_COLUMN_NAME AS referenced_column,
                       t.TABLE_TYPE AS referenced_type,
                       COUNT(*) OVER (
                           PARTITION BY CAST(k.CONSTRAINT_NAME AS BINARY)
                       ) AS component_count
                FROM information_schema.KEY_COLUMN_USAGE AS k
                INNER JOIN information_schema.TABLES AS t
                  ON CAST(t.TABLE_SCHEMA AS BINARY) = CAST(k.REFERENCED_TABLE_SCHEMA AS BINARY)
                 AND CAST(t.TABLE_NAME AS BINARY) = CAST(k.REFERENCED_TABLE_NAME AS BINARY)
                WHERE CAST(k.TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)
                  AND CAST(k.TABLE_NAME AS BINARY) = CAST(%s AS BINARY)
                  AND CAST(k.REFERENCED_TABLE_SCHEMA AS BINARY) = CAST(%s AS BINARY)
                  AND CAST(k.REFERENCED_TABLE_NAME AS BINARY) IN ({placeholders})
                  AND t.TABLE_TYPE = 'BASE TABLE'
                ORDER BY CAST(k.CONSTRAINT_NAME AS BINARY), k.ORDINAL_POSITION
                LIMIT %s
            """,
            (
                self._settings.database, table, self._settings.database, *allowed,
                self._settings.max_metadata_rows - len(all_rows) + 1,
            ),
            all_rows,
        )
        result["foreign_keys"] = self._foreign_keys(foreign_key_rows, result["columns"])
        # Absence within this scope does not reveal relationships to hidden targets.
        result["foreign_keys_scope"] = "current_database_authorized_tables"
        return self._bounded_result(result)

    def _foreign_keys(self, rows: list, columns: list) -> list[dict]:
        source_columns = {column["name"] for column in columns}
        grouped = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or any(
                    not isinstance(row.get(key), str) or not _IDENTIFIER.fullmatch(row[key])
                    for key in ("name", "column_name", "referenced_table", "referenced_column")
                )
                or row.get("referenced_schema") != self._settings.database
                or row["referenced_table"] not in self._settings.allowed_tables
                or row.get("referenced_type") != "BASE TABLE"
                or row["column_name"] not in source_columns
                or type(row.get("position")) is not int or row["position"] < 1
                or type(row.get("component_count")) is not int or row["component_count"] < 1
            ):
                raise DatabaseError("INVALID_METADATA", "外键元数据不完整或不符合当前授权范围")
            grouped.setdefault(row["name"], []).append(row)
        result = []
        for name, components in sorted(grouped.items()):
            components.sort(key=lambda row: row["position"])
            count = len(components)
            if (
                [row["position"] for row in components] != list(range(1, count + 1))
                or any(row["component_count"] != count for row in components)
                or len({row["referenced_table"] for row in components}) != 1
                or len({row["column_name"].lower() for row in components}) != count
                or len({row["referenced_column"].lower() for row in components}) != count
            ):
                raise DatabaseError("INVALID_METADATA", "外键元数据不完整或不符合当前授权范围")
            result.append({
                "name": name,
                "columns": [row["column_name"] for row in components],
                "referenced_table": components[0]["referenced_table"],
                "referenced_columns": [row["referenced_column"] for row in components],
            })
        return result

    @asynccontextmanager
    async def _connection(self, timeout_seconds: float | None = None):
        budget = (
            self._settings.metadata_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        try:
            async with asyncio.timeout(budget):
                async with self._lock:
                    self._authorize()
                    connection = None
                    try:
                        connection = await aiomysql.connect(
                            host=self._settings.host,
                            port=self._settings.port,
                            db=self._settings.database,
                            user=self._settings.user,
                            password=self._settings.password.get_secret_value(),
                            connect_timeout=self._settings.connect_timeout_seconds,
                            autocommit=True,
                            charset="utf8mb4",
                            cursorclass=aiomysql.SSDictCursor,
                            local_infile=False,
                            echo=False,
                        )
                        # 固定会话设置；仅承诺 MySQL 支持的只读 SELECT 时限。
                        await self._fetch(
                            connection,
                            "SET SESSION max_execution_time = %s",
                            (max(1, math.ceil(budget * 1000)),),
                            [],
                        )
                        yield connection
                    finally:
                        if connection is not None:
                            # 失败/取消时直接关连接，不让流式 cursor.close 排空剩余结果。
                            connection.close()
        except TimeoutError:
            raise DatabaseError("TIMEOUT", "数据库请求超过时间预算，已停止等待并清理连接") from None
        except aiomysql.Error as exc:
            error_number = exc.args[0] if exc.args else None
            if error_number in {1044, 1045, 1142, 1143, 1227}:
                code, message = "PERMISSION_DENIED", "数据库认证或权限校验失败"
            elif error_number in {1205, 1317, 3024}:
                code, message = "TIMEOUT", "数据库请求超时或被中断，连接已清理"
            elif error_number in {2002, 2003, 2006, 2013, 2055}:
                code, message = "CONNECTION_ERROR", "无法连接数据库或连接已中断"
            elif error_number == 1064:
                code, message = "SYNTAX_ERROR", "数据库拒绝 SQL 语法"
            elif error_number in {1052, 1054, 1146}:
                code, message = "SQL_REFERENCE_ERROR", "SQL 的表或列引用无效"
            else:
                code, message = "DATABASE_ERROR", "数据库请求失败"
            raise DatabaseError(code, message) from None
        except OSError:
            raise DatabaseError("CONNECTION_ERROR", "无法连接数据库或连接已中断") from None
        except (DatabaseError, ResultError):
            raise
        except Exception:
            raise DatabaseError("DATABASE_ERROR", "数据库请求未返回可识别结果") from None

    async def _fetch(self, connection, query: str, params: tuple, all_rows: list) -> list:
        cursor = await connection.cursor()
        self._authorize()
        await cursor.execute(query, params)
        rows = []
        while (row := await cursor.fetchone()) is not None:
            all_rows.append(row)
            if len(all_rows) > self._settings.max_metadata_rows:
                raise DatabaseError("RESULT_LIMIT", "元数据超过结果行数上限，请缩小授权范围")
            self._bounded_result(all_rows)
            rows.append(row)
        await cursor.close()
        self._authorize()
        return rows

    def _bounded_result(self, result):
        # Match the JSON serialization used by LangChain's ToolMessage formatter.
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        if size > self._settings.max_metadata_bytes:
            raise DatabaseError("RESULT_LIMIT", "元数据超过结果字节上限，请缩小请求范围")
        return result
