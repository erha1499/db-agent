"""Typed metadata tools. Database targets and permissions are server-side settings."""

from collections.abc import Callable
from copy import deepcopy

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from db_agent.analysis import SqlAnalysisService
from db_agent.db import MetadataConnector
from db_agent.presentation import QueryExecution
from db_agent.query import QueryService


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DescribeTableArguments(NoArguments):
    table: str = Field(
        strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )


class AnalyzeSqlArguments(NoArguments):
    sql: str = Field(strict=True, min_length=1, max_length=65536)


def analysis_tool(
    service: SqlAnalysisService, on_analysis: Callable[[QueryExecution], None] | None = None,
) -> StructuredTool:
    async def analyze(sql: str) -> dict:
        report = await service.analyze(sql)
        if on_analysis is not None:
            on_analysis(QueryExecution(sql, deepcopy(report)))
        return report

    return StructuredTool.from_function(
        coroutine=analyze,
        name="analyze_sql",
        description=(
            "对当前可信数据源方言的完整 SQL 做权限与静态预检，通过后获取普通 EXPLAIN JSON。"
            "不执行业务 SQL；BLOCK/REVIEW/UNKNOWN 是业务结论，不能绕过。"
            "只接收 sql，不接收身份、数据库或授权参数。改写后需重新调用。"
            "标识符/别名用英文 ASCII；聚合仅 COUNT/SUM/AVG/MIN/MAX，"
            "不用 COALESCE/ROUND 等未支持函数，空 SUM 保留 NULL。"
        ),
        args_schema=AnalyzeSqlArguments,
        handle_validation_error="工具参数无效；只接受长度受限的完整 SQL 字符串 sql。",
    )


def query_tool(
    service: QueryService, on_execution: Callable[[QueryExecution], None] | None = None,
) -> StructuredTool:
    async def execute(sql: str) -> dict:
        report = await service.execute(sql)
        if on_execution is not None:
            # Capture from the service before its response enters model context;
            # never reconstruct evidence from a model message or tool-call args.
            on_execution(QueryExecution(sql, deepcopy(report)))
        return report

    return StructuredTool.from_function(
        coroutine=execute,
        name="execute_query",
        description=(
            "当用户要求查询实际数据时，执行一条当前可信数据源方言的完整 SELECT 并返回受限结果。"
            "内部重新进行权限、SQL 与 EXPLAIN 预检，只有 ALLOW 执行；不接受旧报告或授权参数。"
            "生成查询支持单 SELECT、显式 INNER/LEFT JOIN ON；"
            "支持 searched CASE WHEN；不支持 IF、simple CASE、子查询、CTE、UNION、窗口或 DISTINCT。"
            "先检查 status/execution_status，只有 result 才是实际查询数据。"
            "rows 按 columns 位置对应；truncated=true 表示部分结果，不能据此推断总量。"
            "标识符/别名用英文 ASCII；聚合仅 COUNT/SUM/AVG/MIN/MAX，"
            "不用 COALESCE/ROUND 等未支持函数，空 SUM 保留 NULL。"
            "不支持写入、变更、任意参数或目标数据库。"
        ),
        args_schema=AnalyzeSqlArguments,
        handle_validation_error="工具参数无效；只接受长度受限的完整 SQL 字符串 sql。",
    )


def metadata_tools(connector: MetadataConnector) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            coroutine=connector.list_tables,
            name="list_tables",
            description="列出当前数据源明确授权的实际业务表；不读取表内数据。",
            args_schema=NoArguments,
            handle_validation_error="工具参数无效；list_tables 不接受参数。",
        ),
        StructuredTool.from_function(
            coroutine=connector.describe_table,
            name="describe_table",
            description=(
                "读取一张授权表的字段、索引及同库授权表之间声明的完整外键列对。"
                "复合外键按列位置配对，声明不证明历史数据完整；空集仅表示当前范围未返回。"
                "table 仅接受表名，不接受库名或 SQL。"
            ),
            args_schema=DescribeTableArguments,
            handle_validation_error="工具参数无效；table 必须是支持的单个表名。",
        ),
    ]
