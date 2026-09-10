"""用 LangChain 创建 Agent；数据库领域工具在后续里程碑接入。"""

import asyncio

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langsmith import tracing_context

from db_agent.config import Settings

SYSTEM_PROMPT = """你是面向研发人员的数据库学习与诊断助手，默认使用中文回答。
当前处于项目初始化阶段，没有数据库连接、元数据、EXPLAIN 或 SQL 执行工具。
你可以解释概念和讨论 SQL，但必须说明具体数据库结构、执行计划和结果尚未验证。
不能声称已经查询数据库、完成预检、验证性能或执行变更。
"""


class AgentResponseError(RuntimeError):
    """模型没有返回完整的文本回答。"""


async def run_agent(prompt: str, settings: Settings) -> str:
    """执行一次独立会话，框架负责消息调度，不保留历史。"""
    if not prompt.strip():
        raise ValueError("问题不能为空")

    # 显式映射项目配置，不读取全局 OPENAI_* 凭据，也不启用第三方追踪。
    with tracing_context(enabled=False):
        model = ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.openai_base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
            max_tokens=settings.max_output_tokens,
            streaming=False,
            use_responses_api=False,
        )
        try:
            async with asyncio.timeout(settings.run_timeout_seconds):
                agent = create_agent(
                    model=model,
                    tools=[],
                    system_prompt=SYSTEM_PROMPT,
                    middleware=[ModelCallLimitMiddleware(run_limit=1, exit_behavior="error")],
                )
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"recursion_limit": 4},
                )
        finally:
            await model.root_async_client.close()
            model.root_client.close()

    message = result["messages"][-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        raise AgentResponseError("模型返回了当前未支持的消息或工具调用")
    if message.response_metadata.get("finish_reason") == "length":
        raise AgentResponseError("模型输出达到 token 上限，请缩小问题或调整输出预算")
    answer = message.text.strip()
    if not answer:
        raise AgentResponseError("模型未返回文本回答")
    return answer
