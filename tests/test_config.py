import os
import traceback

import pytest

from db_agent.config import ConfigurationError, load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in os.environ:
        if name.upper().startswith("DB_AGENT_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def valid_environment(monkeypatch):
    values = {
        "DB_AGENT_API_KEY": "synthetic-test-key",
        "DB_AGENT_OPENAI_BASE_URL": "https://model.example.test/v1",
        "DB_AGENT_MODEL": "test-model",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_missing_settings_reports_only_project_field_names():
    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == (
        "配置缺失或无效：DB_AGENT_API_KEY, DB_AGENT_MODEL, DB_AGENT_OPENAI_BASE_URL"
    )


def test_global_openai_settings_are_not_fallbacks(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "unrelated-model")

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert "DB_AGENT_API_KEY" in str(exc.value)
    assert "DB_AGENT_OPENAI_BASE_URL" in str(exc.value)
    assert "DB_AGENT_MODEL" in str(exc.value)
    assert "unrelated" not in str(exc.value)


def test_dotenv_takes_priority_over_existing_shell_settings(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "DB_AGENT_API_KEY=dotenv-synthetic-key\n"
        "DB_AGENT_OPENAI_BASE_URL=http://localhost:8000/v1\n"
        "DB_AGENT_MODEL=dotenv-model\n"
        "UNRELATED_SETTING=ignored\n",
        encoding="utf-8",
    )
    dotenv_settings = load_settings()
    assert dotenv_settings.api_key.get_secret_value() == "dotenv-synthetic-key"
    assert dotenv_settings.openai_base_url == "http://localhost:8000/v1"
    assert dotenv_settings.model == "dotenv-model"

    monkeypatch.setenv("DB_AGENT_API_KEY", "environment-synthetic-key")
    monkeypatch.setenv("DB_AGENT_OPENAI_BASE_URL", "https://env.example.test/v1")
    monkeypatch.setenv("DB_AGENT_MODEL", "environment-model")
    settings = load_settings()
    assert settings.api_key.get_secret_value() == "dotenv-synthetic-key"
    assert settings.openai_base_url == "http://localhost:8000/v1"
    assert settings.model == "dotenv-model"


def test_environment_fills_fields_missing_from_dotenv(valid_environment, tmp_path):
    (tmp_path / ".env").write_text("DB_AGENT_MODEL=dotenv-model\n", encoding="utf-8")

    settings = load_settings()

    assert settings.model == "dotenv-model"
    assert settings.api_key.get_secret_value() == valid_environment["DB_AGENT_API_KEY"]
    assert settings.openai_base_url == valid_environment["DB_AGENT_OPENAI_BASE_URL"]


def test_global_openai_settings_in_dotenv_are_not_fallbacks(tmp_path):
    (tmp_path / ".env").write_text(
        "DB_AGENT_API_KEY=synthetic-test-key\n"
        "DB_AGENT_MODEL=test-model\n"
        "OPENAI_BASE_URL=https://unrelated.example.test/v1\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == "配置缺失或无效：DB_AGENT_OPENAI_BASE_URL"


def test_valid_settings_preserve_url_path_and_hide_api_key(valid_environment):
    settings = load_settings()

    assert settings.openai_base_url == "https://model.example.test/v1"
    assert settings.model == "test-model"
    assert settings.request_timeout_seconds == 30
    assert settings.run_timeout_seconds == 60
    assert settings.max_output_tokens == 1024
    assert "synthetic-test-key" not in repr(settings)
    assert "synthetic-test-key" not in str(settings)
    assert "synthetic-test-key" not in settings.model_dump_json()


@pytest.mark.parametrize("name", ["API_KEY", "OPENAI_BASE_URL", "MODEL"])
@pytest.mark.parametrize("value", ["", "   "])
def test_required_values_cannot_be_blank(valid_environment, monkeypatch, name, value):
    monkeypatch.setenv(f"DB_AGENT_{name}", value)

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == f"配置缺失或无效：DB_AGENT_{name}"


@pytest.mark.parametrize(
    "value",
    [
        "not-a-url",
        "ftp://model.example.test/v1",
        "https://",
        "https:model.example.test/v1",
        "https:///model.example.test/v1",
        "https://model.example.test/a b",
        "https://model.example.test/v1\nsecret-marker",
        "https://@model.example.test/v1",
        "https://user:secret-marker@model.example.test/v1",
        "https://secret-marker@model.example.test/v1",
        "https://model.example.test/v1?key=secret-marker",
        "https://model.example.test/v1#secret-marker",
        "https://model.example.test/v1?",
        "https://model.example.test/v1#",
    ],
)
def test_invalid_urls_and_url_credentials_are_rejected(
    valid_environment, monkeypatch, value
):
    monkeypatch.setenv("DB_AGENT_OPENAI_BASE_URL", value)

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == "配置缺失或无效：DB_AGENT_OPENAI_BASE_URL"
    rendered_error = "".join(traceback.format_exception(exc.value))
    assert "secret-marker" not in rendered_error
    assert "synthetic-test-key" not in rendered_error
    assert "ValidationError" not in rendered_error


@pytest.mark.parametrize("name", ["REQUEST_TIMEOUT_SECONDS", "RUN_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity", "bad", "601"])
def test_timeouts_must_be_finite_bounded_positive_numbers(
    valid_environment, monkeypatch, name, value
):
    monkeypatch.setenv(f"DB_AGENT_{name}", value)

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == f"配置缺失或无效：DB_AGENT_{name}"


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "NaN", "16385"])
def test_output_tokens_must_be_a_bounded_positive_integer(
    valid_environment, monkeypatch, value
):
    monkeypatch.setenv("DB_AGENT_MAX_OUTPUT_TOKENS", value)

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == "配置缺失或无效：DB_AGENT_MAX_OUTPUT_TOKENS"


def test_invalid_dotenv_encoding_is_reported_without_contents(tmp_path):
    (tmp_path / ".env").write_bytes(b"DB_AGENT_API_KEY=secret-marker\xff")

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == "无法读取配置：.env / DB_AGENT_*"
    assert "secret-marker" not in "".join(traceback.format_exception(exc.value))
