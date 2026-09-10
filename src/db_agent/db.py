"""受控 MySQL 元数据连接器，不提供用户 SQL 或查询执行入口。"""

import asyncio
import json
import math
import re
from contextlib import asynccontextmanager

import aiomysql

from db_agent.config import DatabaseSettings

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


class DatabaseError(RuntimeError):
    """可向 CLI 或模型返回的数据库错误，不包含驱动原文或连接信息。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class MetadataConnector:
    """每个实例一次只处理一个元数据请求，超时预算包括排队。"""

    def __init__(self, settings: DatabaseSettings):
        self._settings = settings.model_copy(deep=True)
        self._lock = asyncio.Lock()

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
        table = self.validate_table(table)
        result = {"database": self._settings.database, "table": table}
        all_rows = []
        async with self._connection() as connection:
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
            return self._bounded_result(result)

    @asynccontextmanager
    async def _connection(self):
        try:
            async with asyncio.timeout(self._settings.metadata_timeout_seconds):
                async with self._lock:
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
                            (max(1, math.ceil(self._settings.metadata_timeout_seconds * 1000)),),
                            [],
                        )
                        yield connection
                    finally:
                        if connection is not None:
                            # 失败/取消时直接关连接，不让流式 cursor.close 排空剩余结果。
                            connection.close()
        except TimeoutError:
            raise DatabaseError("TIMEOUT", "元数据请求超过时间预算，已停止等待并清理连接") from None
        except aiomysql.Error as exc:
            error_number = exc.args[0] if exc.args else None
            if error_number in {1044, 1045, 1142, 1143, 1227}:
                code, message = "PERMISSION_DENIED", "数据库认证或权限校验失败"
            elif error_number in {1205, 1317, 3024}:
                code, message = "TIMEOUT", "数据库元数据请求超时或被中断，连接已清理"
            elif error_number in {2002, 2003, 2006, 2013, 2055}:
                code, message = "CONNECTION_ERROR", "无法连接数据库或连接已中断"
            else:
                code, message = "DATABASE_ERROR", "数据库元数据请求失败"
            raise DatabaseError(code, message) from None
        except OSError:
            raise DatabaseError("CONNECTION_ERROR", "无法连接数据库或连接已中断") from None
        except DatabaseError:
            raise
        except Exception:
            raise DatabaseError("DATABASE_ERROR", "数据库元数据请求未返回可识别结果") from None

    async def _fetch(self, connection, query: str, params: tuple, all_rows: list) -> list:
        cursor = await connection.cursor()
        await cursor.execute(query, params)
        rows = []
        while (row := await cursor.fetchone()) is not None:
            all_rows.append(row)
            if len(all_rows) > self._settings.max_metadata_rows:
                raise DatabaseError("RESULT_LIMIT", "元数据超过结果行数上限，请缩小授权范围")
            self._bounded_result(all_rows)
            rows.append(row)
        await cursor.close()
        return rows

    def _bounded_result(self, result):
        # Match the JSON serialization used by LangChain's ToolMessage formatter.
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        if size > self._settings.max_metadata_bytes:
            raise DatabaseError("RESULT_LIMIT", "元数据超过结果字节上限，请缩小请求范围")
        return result
