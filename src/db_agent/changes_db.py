"""Fixed parameterized DML, exact local target, atomic receipt and fresh lock barrier."""

from contextlib import asynccontextmanager

import aiomysql

from db_agent.changes import Row, digest
from db_agent.db import DatabaseError

TABLES = ("inventory", "change_receipts", "change_schema_guard")


class ChangeConnector:
    def __init__(self, target, guard):
        self.target = target
        self.guard = guard

    async def statement(self, connection, sql, args=None):
        self.guard()
        async with connection.cursor() as cursor:
            await cursor.execute(sql, args)
            rows = await cursor.fetchmany(5)
            if len(rows) > 4:
                raise DatabaseError("CHANGE_EVIDENCE_LIMIT", "变更证据超限。")
            self.guard()
            return rows, cursor.rowcount

    async def check_target(self, connection):
        rows, _ = await self.statement(
            connection,
            "SELECT DATABASE(), CURRENT_USER(), @@server_uuid, VERSION(), @@session.sql_mode",
        )
        database, user, server_uuid, version, mode = rows[0]
        if (
            database != self.target.database
            or user != "db_agent_changer@%"
            or server_uuid != self.target.server_uuid
            or not version.startswith("8.4.")
            or "STRICT_ALL_TABLES" not in mode.split(",")
        ):
            raise DatabaseError("CHANGE_TARGET", "实际变更目标、账号、版本或严格模式不一致。")
        rows, _ = await self.statement(
            connection,
            "SELECT TABLE_NAME,TABLE_TYPE,ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('inventory','change_receipts')",
        )
        if sorted(rows) != [
            ("change_receipts", "BASE TABLE", "InnoDB"),
            ("inventory", "BASE TABLE", "InnoDB"),
        ]:
            raise DatabaseError("CHANGE_ENGINE", "变更与回执必须是 InnoDB 基础表。")
        definitions = []
        for table in TABLES:
            rows, _ = await self.statement(connection, f"SHOW CREATE TABLE `{table}`")
            definitions.append(rows[0][1])
        rows, _ = await self.statement(connection, "SHOW CREATE FUNCTION change_trigger_count")
        definitions.append(rows[0][2])
        if not rows[0][2] or digest(definitions) != self.target.schema_digest:
            raise DatabaseError("CHANGE_SCHEMA", "变更目标结构已变化。")

        rows, _ = await self.statement(connection, "SELECT trigger_count FROM change_schema_guard")
        if rows != ((0,),):
            raise DatabaseError("CHANGE_TRIGGERS", "目标存在触发器或缺少完整取证，拒绝变更。")

    @asynccontextmanager
    async def connection(self):
        self.guard()
        connection = await aiomysql.connect(
            host=self.target.host,
            port=self.target.port,
            db=self.target.database,
            user=self.target.user,
            password=self.target.password.get_secret_value(),
            autocommit=False,
            connect_timeout=3,
            charset="utf8mb4",
            local_infile=False,
        )
        try:
            await self.statement(
                connection,
                "SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,"
                "NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO'",
            )
            await self.statement(connection, "SET SESSION innodb_lock_wait_timeout=2")
            await self.statement(connection, "SET SESSION lock_wait_timeout=2")
            await self.statement(connection, "SET SESSION max_execution_time=3000")
            await self.statement(
                connection, "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
            )
            await self.statement(connection, "START TRANSACTION READ WRITE")
            await self.check_target(connection)
            yield connection
        finally:
            # Never retry/implicitly reconnect. Closing an uncommitted connection is
            # cleanup, not proof of server-side rollback or cancellation.
            connection.close()

    def transaction(self, connection):
        self.guard()
        if not connection.server_status & 1 or connection.server_status & 8192:
            raise DatabaseError("CHANGE_TRANSACTION", "未确认写入事务状态。")

    async def row(self, connection, item_id):
        rows, _ = await self.statement(
            connection, "SELECT quantity,version FROM inventory WHERE id=%s FOR UPDATE", (item_id,)
        )
        if len(rows) != 1:
            raise DatabaseError("CHANGE_ROW", "必须命中一条已存在的库存记录。")
        return Row(quantity=rows[0][0], version=rows[0][1]).model_dump()

    async def receipt(self, connection, identifier):
        rows, _ = await self.statement(
            connection,
            "SELECT digest,item_id,before_quantity,after_quantity,before_version,after_version "
            "FROM change_receipts WHERE change_id=%s",
            (identifier,),
        )
        return list(rows[0]) if rows else None

    @staticmethod
    def expected(item):
        p = item["plan"]
        return [
            item["digest"],
            p["item_id"],
            p["before"]["quantity"],
            p["after"]["quantity"],
            p["before"]["version"],
            p["after"]["version"],
        ]

    async def read(self, item_id):
        async with self.connection() as connection:
            result = await self.row(connection, item_id)
            await self.check_target(connection)
            await connection.rollback()
            self.guard()
            return result

    async def execute(self, item):
        p = item["plan"]
        commit_sent = False
        async with self.connection() as connection:
            try:
                before = await self.row(connection, p["item_id"])
                receipt = await self.receipt(connection, p["id"])
                # Locks pin both tables before schema and transaction rechecks.
                await self.check_target(connection)
                self.transaction(connection)
                if receipt is not None:
                    await connection.rollback()
                    return {"outcome": "unknown", "code": "CHANGE_RECEIPT_EXISTS"}
                if before != p["before"]:
                    await connection.rollback()
                    return {"outcome": "rejected", "code": "CHANGE_DRIFT", "current": before}
                _, count = await self.statement(
                    connection,
                    "UPDATE inventory SET quantity=%s,version=%s "
                    "WHERE id=%s AND quantity=%s AND version=%s",
                    (
                        p["after"]["quantity"],
                        p["after"]["version"],
                        p["item_id"],
                        before["quantity"],
                        before["version"],
                    ),
                )
                if count != 1:
                    raise DatabaseError("CHANGE_ROW_COUNT", "实际变更行数不为1。")
                self.transaction(connection)
                await self.statement(
                    connection,
                    "INSERT INTO change_receipts "
                    "(change_id,digest,item_id,before_quantity,after_quantity,"
                    "before_version,after_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (p["id"], *self.expected(item)),
                )
                after = await self.row(connection, p["item_id"])
                if after != p["after"] or await self.receipt(connection, p["id"]) != self.expected(
                    item
                ):
                    raise DatabaseError("CHANGE_POSTCONDITION", "事务内后值或回执不一致。")
                self.transaction(connection)
                commit_sent = True
                await connection.commit()
                return {
                    "outcome": "committed",
                    "code": "COMMIT_ACKNOWLEDGED",
                    "current": after,
                    "receipt_verified": True,
                }
            except Exception:
                if commit_sent:
                    return {"outcome": "unknown", "code": "COMMIT_UNCONFIRMED"}
                try:
                    await connection.rollback()
                except Exception:
                    return {"outcome": "unknown", "code": "ROLLBACK_UNCONFIRMED"}
                return {"outcome": "rolled_back", "code": "ROLLBACK_ACKNOWLEDGED"}

    async def reconcile(self, item):
        try:
            async with self.connection() as connection:
                # Service lock first proves the original local dispatcher has exited.
                # Row lock then waits for its server transaction to finish. A fresh
                # READ COMMITTED receipt read follows the barrier, never an old snapshot.
                current = await self.row(connection, item["plan"]["item_id"])
                receipt = await self.receipt(connection, item["plan"]["id"])
                await self.check_target(connection)
                self.transaction(connection)
                await connection.rollback()
                self.guard()
                if receipt == self.expected(item):
                    return {
                        "outcome": "committed",
                        "code": "RECEIPT_CONFIRMED",
                        "current": current,
                        "receipt_verified": True,
                    }
                if receipt is None and item["status"] != "committed":
                    return {
                        "outcome": "not_committed",
                        "code": "ABSENT_AFTER_BARRIER",
                        "current": current,
                        "receipt_verified": False,
                    }
                return {"outcome": "unknown", "code": "RECEIPT_MISMATCH", "current": current}
        except Exception:
            return {"outcome": "unknown", "code": "RECONCILIATION_UNAVAILABLE"}
