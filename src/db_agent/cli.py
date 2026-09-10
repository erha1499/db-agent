"""项目配置检查与模型调用入口。"""

import argparse
import asyncio
import json
import sys
import time

from db_agent.agent import AgentResponseError, run_agent
from db_agent.analysis import SqlAnalysisService
from db_agent.config import (
    ConfigurationError,
    load_analysis_settings,
    load_database_settings,
    load_query_settings,
    load_settings,
)
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.query import QueryService
from db_agent.records import RunRecord


async def run_database_command(args, connector: MetadataConnector, record: RunRecord) -> dict:
    started = time.monotonic()
    code, status = "CANCELLED", "error"
    try:
        if args.db_command == "check":
            result = await connector.check()
        elif args.db_command == "tables":
            result = await connector.list_tables()
        elif args.db_command == "analyze":
            result = await SqlAnalysisService(connector, args.analysis_settings, record).analyze(
                args.sql
            )
        elif args.db_command == "query":
            result = await QueryService(
                connector, args.analysis_settings, args.query_settings, record
            ).execute(args.sql)
        else:
            result = await connector.describe_table(args.table)
        code, status = None, "ok"
        if args.db_command == "query" and result["status"] == "error":
            code, status = result["error"]["code"], "error"
        return result
    except DatabaseError as exc:
        code = exc.code
        raise
    finally:
        record.emit(
            "database_finished",
            operation=args.db_command,
            status=status,
            code=code,
            duration_ms=round((time.monotonic() - started) * 1000),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库 Agent：配置检查与数据库问答")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="校验配置，仅显示配置状态")
    commands.add_parser("check", help="调用一次模型，检查连通性")
    chat = commands.add_parser("chat", help="进行一次带元数据、诊断和只读查询工具的独立问答")
    chat.add_argument("prompt", help="问题或需要解释的 SQL")
    db = commands.add_parser("db", help="直接检查数据库、预检或受控查询，不调用模型")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("check", help="检查只读数据库连接")
    db_commands.add_parser("tables", help="列出授权的业务表")
    describe = db_commands.add_parser("describe", help="读取一张授权表的字段和索引")
    describe.add_argument("table", help="单个表名")
    for name, help_text in (
        ("analyze", "SQL 静态预检与普通 EXPLAIN 诊断"),
        ("query", "执行通过完整预检的只读 SELECT"),
    ):
        command = db_commands.add_parser(name, help=help_text)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("sql", nargs="?", help="一条完整 SQL；含敏感字面值时建议使用 --stdin")
        source.add_argument(
            "--stdin", action="store_true", help="从标准输入读取有长度限制的 UTF-8 SQL"
        )
    args = parser.parse_args(argv)
    if args.command == "chat" and not args.prompt.strip():
        parser.error("问题不能为空")

    try:
        if args.command == "db":
            if args.db_command in {"analyze", "query"}:
                args.analysis_settings = load_analysis_settings()
                if args.db_command == "query":
                    args.query_settings = load_query_settings()
                if args.stdin:
                    try:
                        raw = sys.stdin.buffer.read(args.analysis_settings.max_sql_bytes + 1)
                        args.sql = raw.decode("utf-8")
                    except (OSError, UnicodeError):
                        raise ConfigurationError("无法读取 UTF-8 SQL 标准输入。") from None
            connector = MetadataConnector(load_database_settings())
            with RunRecord() as record:
                result = asyncio.run(run_database_command(args, connector, record))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.db_command == "query":
                return {"ok": 0, "rejected": 3, "error": 1}[result["status"]]
            return 0
        settings = load_settings()
        if args.command == "config":
            for name in ("OPENAI_BASE_URL", "API_KEY", "MODEL"):
                print(f"DB_AGENT_{name}: 已配置并通过校验（值已隐藏）")
            return 0
        prompt = (
            "这是模型连通性检查，请仅回复 DB_AGENT_OK。" if args.command == "check" else args.prompt
        )
        connector = MetadataConnector(load_database_settings()) if args.command == "chat" else None
        analysis_settings = load_analysis_settings() if connector else None
        query_settings = load_query_settings() if connector else None
        with RunRecord() as record:
            answer = asyncio.run(run_agent(
                prompt, settings, connector, record, analysis_settings, query_settings
            ))
            if args.command == "check":
                if answer != "DB_AGENT_OK":
                    raise AgentResponseError("模型已响应，但未返回预期的连通性确认文本")
                print("模型连通性检查通过（数据库连接请使用 db check 单独验证）。")
            else:
                print(answer)
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except TimeoutError:
        print("运行超过总时间预算，已停止等待；数据库连接会关闭。", file=sys.stderr)
    except DatabaseError as exc:
        print(f"数据库操作失败（{exc.code}）：{exc.message}", file=sys.stderr)
    except AgentResponseError as exc:
        print(str(exc), file=sys.stderr)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        # 网关异常可能含请求信息、凭据或响应原文，CLI 不直接输出。
        print(
            f"模型调用失败（{type(exc).__name__}），请检查配置、网络或服务状态。",
            file=sys.stderr,
        )
    return 1
