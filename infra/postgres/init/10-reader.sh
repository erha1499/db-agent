#!/bin/bash
# Runs as the postgres OS user, only when the official image initializes a volume.
set -euo pipefail
if [[ ! "${DB_AGENT_POSTGRES_PASSWORD:-}" =~ ^[A-Za-z0-9_-]{24,128}$ ]]; then
    echo 'Reader password must contain 24-128 letters, digits, _ or -.' >&2
    exit 1
fi
psql --username postgres --dbname db_agent_pg --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
\getenv reader_password DB_AGENT_POSTGRES_PASSWORD
CREATE ROLE db_agent_reader LOGIN PASSWORD :'reader_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE ALL ON DATABASE db_agent_pg FROM PUBLIC;
GRANT CONNECT ON DATABASE db_agent_pg TO db_agent_reader;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres, template1 FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA business AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA business FROM PUBLIC;
GRANT USAGE ON SCHEMA business TO db_agent_reader;
ALTER ROLE db_agent_reader IN DATABASE db_agent_pg
    SET default_transaction_read_only = on;
ALTER ROLE db_agent_reader IN DATABASE db_agent_pg
    SET search_path = pg_catalog, business;
ALTER ROLE db_agent_reader IN DATABASE db_agent_pg SET timezone = 'UTC';
ALTER DEFAULT PRIVILEGES IN SCHEMA business REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
SQL
