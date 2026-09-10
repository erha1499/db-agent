"""Typed metadata tools. Database targets and permissions are server-side settings."""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from db_agent.analysis import SqlAnalysisService
from db_agent.db import MetadataConnector


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DescribeTableArguments(NoArguments):
    table: str = Field(
        strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )


class AnalyzeSqlArguments(NoArguments):
    sql: str = Field(strict=True, min_length=1, max_length=65536)


def analysis_tool(service: SqlAnalysisService) -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=service.analyze,
        name="analyze_sql",
        description=(
            "对完整 MySQL SQL 做权限与静态预检，通过后获取普通 EXPLAIN JSON 并返回诊断证据。"
            "不执行业务 SQL；BLOCK/REVIEW/UNKNOWN 是业务结论，不能绕过。"
            "只接收 sql，不接收身份、数据库或授权参数。改写后需重新调用。"
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
            description="读取一张授权表的真实字段和索引。table 仅接受表名，不接受库名或 SQL。",
            args_schema=DescribeTableArguments,
            handle_validation_error="工具参数无效；table 必须是支持的单个表名。",
        ),
    ]
