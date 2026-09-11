"""Read project-scoped settings without exposing credential values."""

import json
import re
from typing import Annotated, ClassVar, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)


class ConfigurationError(ValueError):
    """A configuration failure safe to show in the CLI."""


class _ProjectSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DB_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 本地文件优先，避免已加载的 shell 变量覆盖用户对 .env 的修改。
        return init_settings, dotenv_settings, env_settings, file_secret_settings


class Settings(_ProjectSettings):
    api_key: SecretStr
    openai_base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=60, gt=0, le=600, allow_inf_nan=False)
    max_output_tokens: int = Field(default=1024, gt=0, le=16384)
    max_model_calls: int = Field(default=4, ge=1, le=10)
    max_tool_calls: int = Field(default=6, ge=1, le=20)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if not key:
            raise ValueError("must not be blank")
        return SecretStr(key)

    @field_validator("openai_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            url = AnyHttpUrl(value)
            parsed = urlsplit(value)
        except ValueError:
            raise ValueError("must be an HTTP or HTTPS URL") from None
        if not parsed.netloc or "\\" in value or any(char.isspace() for char in value):
            raise ValueError("must be an HTTP or HTTPS URL without whitespace")
        if parsed.username is not None or any(
            part is not None
            for part in (url.username, url.password, url.query, url.fragment)
        ):
            raise ValueError("must not contain credentials, a query, or a fragment")
        return value


def _validate_identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value) is None:
        raise ValueError("must be a supported ASCII MySQL identifier")
    return value


class DatabaseSettings(_ProjectSettings):
    """Independent credentials and limits for the database reader connection."""

    model_config = SettingsConfigDict(env_prefix="DB_AGENT_MYSQL_")
    kind: ClassVar[str] = "mysql"

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=13306, ge=1, le=65535)
    database: str = "db_agent"
    user: str = Field(default="db_agent_reader", min_length=1)
    password: SecretStr
    allowed_tables: Annotated[tuple[str, ...], NoDecode] = Field(default=(), max_length=100)
    connect_timeout_seconds: float = Field(default=3, ge=1, le=30, allow_inf_nan=False)
    metadata_timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    max_metadata_rows: int = Field(default=200, ge=1, le=1000)
    max_metadata_bytes: int = Field(default=32768, ge=1024, le=131072)

    @field_validator("database")
    @classmethod
    def validate_database(cls, value: str) -> str:
        _validate_identifier(value)
        if value.casefold() in {"information_schema", "mysql", "performance_schema", "sys"}:
            raise ValueError("system databases are not allowed")
        return value

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        if value.casefold() == "root":
            raise ValueError("the root account is not allowed")
        return value

    @field_validator("password", mode="before")
    @classmethod
    def validate_password(cls, value: object) -> SecretStr:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must not be blank")
        # Database passwords can contain meaningful leading or trailing whitespace.
        return SecretStr(value)

    @field_validator("allowed_tables", mode="before")
    @classmethod
    def parse_allowed_tables(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("must be a JSON array of table identifiers") from None
            if not isinstance(value, list):
                raise ValueError("must be a JSON array of table identifiers")
        return value

    @field_validator("allowed_tables")
    @classmethod
    def validate_allowed_tables(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for table in value:
            _validate_identifier(table)
        return value


class PostgreSQLSettings(DatabaseSettings):
    """Separate PostgreSQL reader configuration, never inferred from MySQL credentials."""

    model_config = SettingsConfigDict(env_prefix="DB_AGENT_POSTGRES_", validate_by_name=True)
    kind: ClassVar[str] = "postgresql"
    port: int = Field(default=15432, ge=1, le=65535)
    database: str = "db_agent_pg"
    schema_name: str = Field(default="business", validation_alias="DB_AGENT_POSTGRES_SCHEMA")

    @field_validator("database", "schema_name", "user")
    @classmethod
    def validate_pg_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
            raise ValueError("must be a simple PostgreSQL identifier of at most 63 bytes")
        if value.lower().startswith("pg_") or value.lower() in {
            "postgres", "root", "template0", "template1", "information_schema",
        }:
            raise ValueError("administrative and system names are not supported")
        return value

    @field_validator("allowed_tables")
    @classmethod
    def validate_pg_tables(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
                raise ValueError("table must be a simple identifier of at most 63 bytes")
        return value

    @field_validator("host")
    @classmethod
    def validate_pg_host(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", value):
            raise ValueError("a single explicit TCP host is required")
        return value


class _DatabaseSelection(_ProjectSettings):
    database_kind: Literal["mysql", "postgresql"] = "mysql"


class AnalysisSettings(_ProjectSettings):
    """Finite assessment budgets; these thresholds are policy, not runtime predictions."""

    model_config = SettingsConfigDict(env_prefix="DB_AGENT_ANALYSIS_")

    max_sql_bytes: int = Field(default=16384, ge=64, le=65536)
    max_ast_nodes: int = Field(default=512, ge=1, le=2048)
    max_ast_depth: int = Field(default=32, ge=1, le=64)
    max_tables: int = Field(default=8, ge=1, le=32)
    max_plan_bytes: int = Field(default=65536, ge=1024, le=262144)
    max_plan_nodes: int = Field(default=256, ge=1, le=1024)
    timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    review_scan_rows: int = Field(default=100000, ge=1, le=1000000000)
    review_join_rows: int = Field(default=1000000, ge=1, le=1000000000)
    review_sort_rows: int = Field(default=100000, ge=1, le=1000000000)


class QuerySettings(_ProjectSettings):
    """Independent budgets for one guarded SELECT and its returned result."""

    model_config = SettingsConfigDict(env_prefix="DB_AGENT_QUERY_")

    max_rows: int = Field(default=100, ge=1, le=1000)
    max_result_bytes: int = Field(default=32768, ge=1024, le=131072)
    max_columns: int = Field(default=64, ge=1, le=256)
    execution_timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    operation_timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)


_SettingsT = TypeVar("_SettingsT", bound=_ProjectSettings)


def _load_settings(settings_type: type[_SettingsT]) -> _SettingsT:
    prefix = settings_type.model_config["env_prefix"]
    try:
        return settings_type()
    except ValidationError as exc:
        fields = sorted(
            {
                f"{prefix}{error['loc'][0].upper()}"
                for error in exc.errors(include_input=False, include_context=False)
                if error["loc"] and error["loc"][0] in settings_type.model_fields
            }
        )
        raise ConfigurationError("配置缺失或无效：" + ", ".join(fields)) from None
    except (OSError, UnicodeError, SettingsError):
        raise ConfigurationError(f"无法读取配置：.env / {prefix}*") from None


def load_settings() -> Settings:
    """Load the current directory's .env first, with environment as fallback."""
    return _load_settings(Settings)


def load_database_settings() -> DatabaseSettings:
    """Load only reader database settings; model configuration is not required."""
    kind = _load_settings(_DatabaseSelection).database_kind
    return _load_settings(PostgreSQLSettings if kind == "postgresql" else DatabaseSettings)


def load_analysis_settings() -> AnalysisSettings:
    """Load SQL assessment policy without requiring model credentials."""
    return _load_settings(AnalysisSettings)


def load_query_settings() -> QuerySettings:
    """Load SELECT budgets without requiring model credentials."""
    return _load_settings(QuerySettings)
