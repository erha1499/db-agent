"""项目配置检查与模型调用入口。"""

import argparse
import asyncio
import sys

from db_agent.agent import AgentResponseError, run_agent
from db_agent.config import ConfigurationError, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库 Agent：LangChain 初始化入口")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="校验配置，仅显示配置状态")
    commands.add_parser("check", help="调用一次模型，检查连通性")
    chat = commands.add_parser("chat", help="进行一次独立的模型问答")
    chat.add_argument("prompt", help="问题或需要解释的 SQL")
    args = parser.parse_args(argv)
    if args.command == "chat" and not args.prompt.strip():
        parser.error("问题不能为空")

    try:
        settings = load_settings()
        if args.command == "config":
            for name in ("OPENAI_BASE_URL", "API_KEY", "MODEL"):
                print(f"DB_AGENT_{name}: 已配置并通过校验（值已隐藏）")
            return 0
        prompt = (
            "这是模型连通性检查，请仅回复 DB_AGENT_OK。"
            if args.command == "check"
            else args.prompt
        )
        answer = asyncio.run(run_agent(prompt, settings))
        if args.command == "check":
            if answer != "DB_AGENT_OK":
                raise AgentResponseError("模型已响应，但未返回预期的连通性确认文本")
            print("模型连通性检查通过（数据库能力尚未接入）。")
        else:
            print(answer)
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except TimeoutError:
        print("模型调用超过总时间预算，已停止等待。", file=sys.stderr)
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
