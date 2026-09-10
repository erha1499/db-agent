import json
import os
import traceback

import pytest

from db_agent.config import (
    ConfigurationError,
    load_analysis_settings,
    load_database_settings,
    load_query_settings,
    load_settings,
)


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


def test_analysis_settings_are_independent_and_dotenv_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_AGENT_ANALYSIS_REVIEW_SCAN_ROWS", "300")
    (tmp_path / ".env").write_text("DB_AGENT_ANALYSIS_REVIEW_SCAN_ROWS=250\n")
    settings = load_analysis_settings()
    assert settings.review_scan_rows == 250
    assert settings.max_sql_bytes == 16384
    assert settings.timeout_seconds == 10


def test_query_settings_defaults_require_neither_model_nor_database_credentials():
    settings = load_query_settings()
    assert settings.max_rows == 100
    assert settings.max_result_bytes == 32768
    assert settings.max_columns == 64
    assert settings.execution_timeout_seconds == 5
    assert settings.operation_timeout_seconds == 15


def test_query_settings_dotenv_wins_and_environment_fills_missing_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_AGENT_QUERY_MAX_ROWS", "80")
    monkeypatch.setenv("DB_AGENT_QUERY_MAX_COLUMNS", "12")
    monkeypatch.setenv("DB_AGENT_MYSQL_MAX_METADATA_ROWS", "999")
    (tmp_path / ".env").write_text("DB_AGENT_QUERY_MAX_ROWS=7\nUNRELATED=ignored\n")
    settings = load_query_settings()
    assert settings.max_rows == 7
    assert settings.max_columns == 12
    assert settings.max_result_bytes == 32768


@pytest.mark.parametrize(("field", "value"), [
    ("MAX_ROWS", "0"), ("MAX_ROWS", "1001"), ("MAX_ROWS", "1.5"),
    ("MAX_RESULT_BYTES", "1023"), ("MAX_RESULT_BYTES", "131073"),
    ("MAX_COLUMNS", "0"), ("MAX_COLUMNS", "257"),
    ("EXECUTION_TIMEOUT_SECONDS", "0"), ("EXECUTION_TIMEOUT_SECONDS", "60.1"),
    ("EXECUTION_TIMEOUT_SECONDS", "nan"), ("EXECUTION_TIMEOUT_SECONDS", "inf"),
    ("OPERATION_TIMEOUT_SECONDS", "-1"), ("OPERATION_TIMEOUT_SECONDS", "120.1"),
    ("OPERATION_TIMEOUT_SECONDS", "-inf"), ("OPERATION_TIMEOUT_SECONDS", "nan"),
    ("MAX_ROWS", "private-invalid-budget"),
])
def test_query_settings_reject_invalid_budgets_without_exposing_values(monkeypatch, field, value):
    monkeypatch.setenv(f"DB_AGENT_QUERY_{field}", value)
    with pytest.raises(ConfigurationError) as caught:
        load_query_settings()
    assert str(caught.value) == f"配置缺失或无效：DB_AGENT_QUERY_{field}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAX_SQL_BYTES", "65537"),
        ("MAX_AST_NODES", "0"),
        ("MAX_AST_DEPTH", "65"),
        ("MAX_TABLES", "33"),
        ("MAX_PLAN_BYTES", "262145"),
        ("MAX_PLAN_NODES", "0"),
        ("TIMEOUT_SECONDS", "nan"),
        ("TIMEOUT_SECONDS", "inf"),
        ("REVIEW_SCAN_ROWS", "0"),
        ("REVIEW_JOIN_ROWS", "-1"),
        ("REVIEW_SORT_ROWS", "private-invalid-value"),
    ],
)
def test_analysis_settings_reject_invalid_budgets_without_echoing_values(monkeypatch, field, value):
    monkeypatch.setenv(f"DB_AGENT_ANALYSIS_{field}", value)
    with pytest.raises(ConfigurationError) as exc:
        load_analysis_settings()
    assert str(exc.value) == f"配置缺失或无效：DB_AGENT_ANALYSIS_{field}"


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
    assert settings.max_model_calls == 4
    assert settings.max_tool_calls == 6
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_MODEL_CALLS", "0"),
        ("MAX_MODEL_CALLS", "11"),
        ("MAX_MODEL_CALLS", "1.5"),
        ("MAX_TOOL_CALLS", "0"),
        ("MAX_TOOL_CALLS", "21"),
        ("MAX_TOOL_CALLS", "1.5"),
    ],
)
def test_model_and_tool_call_budgets_are_bounded_integers(
    valid_environment, monkeypatch, name, value
):
    monkeypatch.setenv(f"DB_AGENT_{name}", value)

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert str(exc.value) == f"配置缺失或无效：DB_AGENT_{name}"


@pytest.fixture
def valid_database_environment(monkeypatch):
    monkeypatch.setenv("DB_AGENT_MYSQL_PASSWORD", "synthetic-reader-password")


def test_database_settings_do_not_require_model_configuration(valid_database_environment):
    settings = load_database_settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 13306
    assert settings.database == "db_agent"
    assert settings.user == "db_agent_reader"
    assert settings.password.get_secret_value() == "synthetic-reader-password"
    assert settings.allowed_tables == ()
    assert settings.connect_timeout_seconds == 3
    assert settings.metadata_timeout_seconds == 5
    assert settings.max_metadata_rows == 200
    assert settings.max_metadata_bytes == 32768
    assert "synthetic-reader-password" not in repr(settings)
    assert "synthetic-reader-password" not in settings.model_dump_json()


def test_database_root_password_is_never_a_reader_password_fallback(monkeypatch):
    monkeypatch.setenv("DB_AGENT_MYSQL_ROOT_PASSWORD", "synthetic-root-secret")

    with pytest.raises(ConfigurationError) as exc:
        load_database_settings()

    assert str(exc.value) == "配置缺失或无效：DB_AGENT_MYSQL_PASSWORD"
    assert "synthetic-root-secret" not in str(exc.value)


def test_database_dotenv_takes_priority_with_environment_fallback(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "DB_AGENT_MYSQL_PASSWORD=synthetic-dotenv-password\n"
        "DB_AGENT_MYSQL_DATABASE=dotenv_db\n"
        'DB_AGENT_MYSQL_ALLOWED_TABLES=["customers","orders","order_items"]\n'
        "DB_AGENT_MYSQL_ROOT_PASSWORD=ignored-root-secret\n"
        "DB_AGENT_MODEL=ignored-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_AGENT_MYSQL_PASSWORD", "synthetic-env-password")
    monkeypatch.setenv("DB_AGENT_MYSQL_DATABASE", "env_db")
    monkeypatch.setenv("DB_AGENT_MYSQL_HOST", "mysql.example.test")
    monkeypatch.setenv("DB_AGENT_MYSQL_ALLOWED_TABLES", '["env_table"]')

    settings = load_database_settings()

    assert settings.password.get_secret_value() == "synthetic-dotenv-password"
    assert settings.database == "dotenv_db"
    assert settings.host == "mysql.example.test"
    assert settings.allowed_tables == ("customers", "orders", "order_items")
    assert not hasattr(settings, "root_password")
    assert "ignored-root-secret" not in repr(settings)


def test_database_password_preserves_significant_whitespace(monkeypatch):
    monkeypatch.setenv("DB_AGENT_MYSQL_PASSWORD", " synthetic-password ")

    assert load_database_settings().password.get_secret_value() == " synthetic-password "


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HOST", ""),
        ("HOST", "   "),
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "1.5"),
        ("PASSWORD", ""),
        ("PASSWORD", "   "),
        ("USER", ""),
        ("USER", "root"),
        ("USER", " ROOT "),
        ("DATABASE", ""),
        ("DATABASE", "1invalid"),
        ("DATABASE", "has-hyphen"),
        ("DATABASE", "has.dot"),
        ("DATABASE", "with space"),
        ("DATABASE", "数据库"),
        ("DATABASE", "a" * 65),
        ("DATABASE", "mysql"),
        ("DATABASE", "INFORMATION_SCHEMA"),
        ("DATABASE", "performance_schema"),
        ("DATABASE", "sys"),
        ("CONNECT_TIMEOUT_SECONDS", "0.5"),
        ("CONNECT_TIMEOUT_SECONDS", "31"),
        ("CONNECT_TIMEOUT_SECONDS", "NaN"),
        ("CONNECT_TIMEOUT_SECONDS", "Infinity"),
        ("METADATA_TIMEOUT_SECONDS", "0"),
        ("METADATA_TIMEOUT_SECONDS", "61"),
        ("METADATA_TIMEOUT_SECONDS", "NaN"),
        ("METADATA_TIMEOUT_SECONDS", "Infinity"),
        ("MAX_METADATA_ROWS", "0"),
        ("MAX_METADATA_ROWS", "1001"),
        ("MAX_METADATA_ROWS", "1.5"),
        ("MAX_METADATA_BYTES", "1023"),
        ("MAX_METADATA_BYTES", "131073"),
        ("MAX_METADATA_BYTES", "1024.5"),
    ],
)
def test_invalid_database_configuration_reports_only_field_names(
    valid_database_environment, monkeypatch, name, value
):
    monkeypatch.setenv(f"DB_AGENT_MYSQL_{name}", value)

    with pytest.raises(ConfigurationError) as exc:
        load_database_settings()

    assert str(exc.value) == f"配置缺失或无效：DB_AGENT_MYSQL_{name}"
    assert "synthetic-reader-password" not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "orders",
        '"orders"',
        "null",
        '{"orders": true}',
        '["secret-marker]',
        '["orders", 1]',
        '["orders", null]',
        '["1invalid"]',
        '["with-hyphen"]',
        '["db.table"]',
        '["with space"]',
        '["`quoted`"]',
        '["中文"]',
        '[""]',
        json.dumps(["a" * 65]),
        json.dumps([f"table_{index}" for index in range(101)]),
    ],
)
def test_allowed_tables_require_a_bounded_json_array_of_identifiers(
    valid_database_environment, monkeypatch, value
):
    monkeypatch.setenv("DB_AGENT_MYSQL_ALLOWED_TABLES", value)

    with pytest.raises(ConfigurationError) as exc:
        load_database_settings()

    assert str(exc.value) == "配置缺失或无效：DB_AGENT_MYSQL_ALLOWED_TABLES"
    rendered_error = "".join(traceback.format_exception(exc.value))
    assert "secret-marker" not in rendered_error
    assert "synthetic-reader-password" not in rendered_error
    assert "ValidationError" not in rendered_error


def test_database_configuration_accepts_supported_boundary_values(
    valid_database_environment, monkeypatch
):
    table_names = ["_" + "a" * 63, *[f"table_{index}" for index in range(99)]]
    monkeypatch.setenv("DB_AGENT_MYSQL_DATABASE", "_" + "a" * 63)
    monkeypatch.setenv("DB_AGENT_MYSQL_ALLOWED_TABLES", json.dumps(table_names))
    monkeypatch.setenv("DB_AGENT_MYSQL_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("DB_AGENT_MYSQL_METADATA_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("DB_AGENT_MYSQL_MAX_METADATA_ROWS", "1")
    monkeypatch.setenv("DB_AGENT_MYSQL_MAX_METADATA_BYTES", "1024")

    settings = load_database_settings()

    assert settings.allowed_tables == tuple(table_names)
    assert len(settings.database) == 64
    assert settings.connect_timeout_seconds == 1
    assert settings.metadata_timeout_seconds == 0.1
    assert settings.max_metadata_rows == 1
    assert settings.max_metadata_bytes == 1024
