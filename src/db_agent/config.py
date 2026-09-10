"""Read project-scoped model settings without exposing credential values."""

from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)


class ConfigurationError(ValueError):
    """A configuration failure safe to show in the CLI."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DB_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    api_key: SecretStr
    openai_base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=60, gt=0, le=600, allow_inf_nan=False)
    max_output_tokens: int = Field(default=1024, gt=0, le=16384)

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


def load_settings() -> Settings:
    """Load the current directory's .env first, with environment as fallback."""
    try:
        return Settings()
    except ValidationError as exc:
        fields = sorted(
            {
                f"DB_AGENT_{error['loc'][0].upper()}"
                for error in exc.errors(include_input=False, include_context=False)
                if error["loc"] and error["loc"][0] in Settings.model_fields
            }
        )
        raise ConfigurationError("配置缺失或无效：" + ", ".join(fields)) from None
    except (OSError, UnicodeError, SettingsError):
        raise ConfigurationError("无法读取配置：.env / DB_AGENT_*") from None
