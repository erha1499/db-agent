"""Provision only the new fixed synthetic target. Never inspect original .env/root."""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
from pathlib import Path

import aiomysql
from dotenv import dotenv_values

from db_agent.changes import ChangeTarget, digest
from db_agent.changes_db import TABLES

COMPOSE = Path("infra/changes/compose.yaml")
ENV = Path("outputs/changes/compose.env")
TARGET = Path("outputs/changes/target.json")


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())


async def bind(password):
    connection = await aiomysql.connect(
        host="127.0.0.1",
        port=13316,
        user="db_agent_changer",
        password=password,
        db="db_agent_changes",
        connect_timeout=3,
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT @@server_uuid")
            server_uuid = (await cursor.fetchone())[0]
            definitions = []
            for table in TABLES:
                await cursor.execute(f"SHOW CREATE TABLE `{table}`")
                definitions.append((await cursor.fetchone())[1])
            await cursor.execute("SHOW CREATE FUNCTION change_trigger_count")
            definitions.append((await cursor.fetchone())[2])
        target = ChangeTarget(
            password=password, server_uuid=server_uuid, schema_digest=digest(definitions)
        )
        payload = target.model_dump(mode="json", exclude={"password"})
        payload["password"] = password
        private_write(TARGET, json.dumps(payload))
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="创建独立 db-agent-changes 合成目标，端口13316")
    parser.add_argument("--create", action="store_true", required=True)
    parser.parse_args()
    if ENV.exists() or TARGET.exists() or Path("outputs/changes/reader.json").exists():
        parser.exit(2, "配置已存在；停止，不覆盖、不改密、不自动重建。\n")
    # Refuse to adopt existing resources or a container belonging to another task.
    for cmd in (
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=db-agent-changes"],
        ["docker", "volume", "ls", "-q", "--filter", "name=^db-agent-changes_mysql_data$"],
    ):
        if subprocess.check_output(cmd, text=True).strip():
            parser.exit(2, "独立目标资源已存在；停止，请人工核对。\n")
    private_write(
        ENV,
        f"CHANGE_ROOT_PASSWORD={secrets.token_urlsafe(32)}\n"
        f"CHANGE_PASSWORD={secrets.token_urlsafe(32)}\n"
        f"CHANGE_READER_PASSWORD={secrets.token_urlsafe(32)}\n",
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--env-file",
            str(ENV),
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "180",
        ],
        check=True,
    )
    values = dotenv_values(ENV)
    asyncio.run(bind(values["CHANGE_PASSWORD"]))
    private_write(
        Path("outputs/changes/reader.json"),
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 13316,
                "database": "db_agent_changes",
                "user": "db_agent_change_reader",
                "password": values["CHANGE_READER_PASSWORD"],
                "allowed_tables": ["inventory"],
            }
        ),
    )
    print("独立合成目标已创建并绑定：mysql 127.0.0.1:13316/db_agent_changes；凭据仅存忽略目录。")


if __name__ == "__main__":
    main()
