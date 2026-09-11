"""Human-only management entry point; deliberately absent from Agent tools."""

import argparse
import asyncio
import json
import sys

from db_agent.config import load_analysis_settings, load_database_settings
from db_agent.connectors import create_connector
from db_agent.knowledge import MAX_DOCUMENT_BYTES, KnowledgeError, KnowledgeStore


def add_knowledge_commands(commands):
    root = commands.add_parser("knowledge", help="管理本机确认的业务知识，不调用模型")
    actions = root.add_subparsers(dest="knowledge_command", required=True)
    create = actions.add_parser("create", help="从 UTF-8 JSON 标准输入保存不可变草稿")
    create.add_argument("--stdin", required=True, action="store_true")
    actions.add_parser("list", help="列出当前数据源权限范围的知识与状态")
    show = actions.add_parser("show", help="查看完整草稿、来源、状态和确认摘要")
    show.add_argument("id")
    confirm = actions.add_parser("confirm", help="确认已审阅的确切草稿与当前结构")
    confirm.add_argument("id")
    confirm.add_argument("--digest", required=True)
    revoke = actions.add_parser("revoke", help="撤销草稿或已确认版本，不删除历史来源")
    revoke.add_argument("id")
    revoke.add_argument("--reason", required=True)


async def run_knowledge_command(args: argparse.Namespace) -> dict | list:
    store = KnowledgeStore()
    connector = create_connector(load_database_settings())
    limits = load_analysis_settings()
    if args.knowledge_command == "create":
        try:
            raw = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise KnowledgeError("KNOWLEDGE_INVALID") from None
        return store.create(raw, connector, limits)
    if args.knowledge_command == "list":
        return store.list(connector.knowledge_scope)
    if args.knowledge_command == "show":
        return store.get(args.id, connector.knowledge_scope)
    if args.knowledge_command == "revoke":
        return store.revoke(args.id, connector.knowledge_scope, args.reason)
    async with asyncio.timeout(15):
        return await store.confirm(args.id, args.digest, connector, limits)


def knowledge_command(args) -> int:
    print(json.dumps(asyncio.run(run_knowledge_command(args)), ensure_ascii=False, indent=2))
    return 0
