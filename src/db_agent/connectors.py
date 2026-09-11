"""Select one concrete connector using trusted, validated server configuration."""

from db_agent.config import DatabaseSettings, PostgreSQLSettings
from db_agent.db import MetadataConnector


def create_connector(settings: DatabaseSettings, **kwargs):
    if isinstance(settings, PostgreSQLSettings):
        from db_agent.postgres import PostgreSQLConnector

        return PostgreSQLConnector(settings, **kwargs)
    if type(settings) is not DatabaseSettings:
        raise TypeError("unsupported database settings")
    return MetadataConnector(settings, **kwargs)
