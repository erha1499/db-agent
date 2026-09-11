"""No I/O: configuration, dispatch and source identity cannot cross database kinds."""

import os

import pytest

from db_agent.config import (
    ConfigurationError,
    DatabaseSettings,
    PostgreSQLSettings,
    load_database_settings,
)
from db_agent.connectors import create_connector
from db_agent.conversations import source_scope
from db_agent.db import DatabaseError, MetadataConnector
from db_agent.postgres import PostgreSQLConnector


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in os.environ:
        if key.startswith("DB_AGENT_"):
            monkeypatch.delenv(key)


def test_default_mysql_remains_separate(monkeypatch):
    monkeypatch.setenv("DB_AGENT_MYSQL_PASSWORD", "synthetic-reader")
    mysql = load_database_settings()
    assert type(mysql) is DatabaseSettings
    assert type(create_connector(mysql)) is MetadataConnector


def test_postgres_does_not_fall_back_to_mysql_credentials(monkeypatch):
    monkeypatch.setenv("DB_AGENT_DATABASE_KIND", "postgresql")
    monkeypatch.setenv("DB_AGENT_MYSQL_PASSWORD", "synthetic-reader")
    with pytest.raises(ConfigurationError, match="DB_AGENT_POSTGRES_PASSWORD"):
        load_database_settings()


def test_postgres_dotenv_selection_precedes_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_AGENT_DATABASE_KIND", "mysql")
    (tmp_path / ".env").write_text(
        "DB_AGENT_DATABASE_KIND=postgresql\nDB_AGENT_POSTGRES_PASSWORD=synthetic\n"
        "DB_AGENT_POSTGRES_SCHEMA=analytics\nDB_AGENT_POSTGRES_ALLOWED_TABLES=[\"orders\"]\n"
    )
    settings = load_database_settings()
    assert isinstance(settings, PostgreSQLSettings)
    assert settings.schema_name == "analytics"
    assert settings.allowed_tables == ("orders",)
    assert type(create_connector(settings)) is PostgreSQLConnector


@pytest.mark.parametrize("kind", ["sqlite", "postgres", "MYSQL", "", "mysql,postgresql"])
def test_unknown_kind_is_configuration_error(monkeypatch, kind):
    monkeypatch.setenv("DB_AGENT_DATABASE_KIND", kind)
    with pytest.raises(ConfigurationError, match="DB_AGENT_DATABASE_KIND"):
        load_database_settings()


@pytest.mark.parametrize("changes", [
    {"user": "postgres"}, {"user": "root"}, {"database": "template1"},
    {"schema_name": "pg_catalog"}, {"schema_name": "information_schema"},
    {"schema_name": "a" * 64}, {"allowed_tables": ("a" * 64,)},
    {"host": "localhost,other"}, {"host": "/tmp"}, {"user": "role other"},
])
def test_pg_settings_reject_ambiguous_or_administrative_names(changes):
    with pytest.raises(ValueError):
        PostgreSQLSettings(_env_file=None, password="synthetic", **changes)


def test_source_scope_binds_kind_schema_user_and_complete_allowlist():
    mysql = DatabaseSettings(_env_file=None, password="synthetic", port=15432,
                             database="db_agent_pg", allowed_tables=("orders",))
    pg = PostgreSQLSettings(_env_file=None, password="synthetic", allowed_tables=("orders",))
    variants = [mysql, pg, pg.model_copy(update={"schema_name": "other"}),
                pg.model_copy(update={"user": "other_reader"}),
                pg.model_copy(update={"allowed_tables": ("orders", "customers")})]
    assert len({source_scope(settings) for settings in variants}) == len(variants)
    assert source_scope(pg) == source_scope(pg.model_copy(update={"password": "changed"}))


def test_factory_keeps_server_only_veto_and_scope_override():
    calls = []

    def revoke():
        calls.append(True)
        raise DatabaseError("PERMISSION_DENIED", "revoked")

    for cls in (DatabaseSettings, PostgreSQLSettings):
        connector = create_connector(cls(_env_file=None, password="synthetic"),
                                     authorization_check=revoke, knowledge_scope="server-scope")
        with pytest.raises(DatabaseError, match="revoked"):
            _ = connector.knowledge_scope
    assert len(calls) == 2
