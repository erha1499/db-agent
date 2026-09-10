#!/bin/bash
# Sourced by the official MySQL entrypoint on a new data volume.
(
    # The official entrypoint helpers access optional positional parameters.
    set -eo pipefail
    if [[ ! "${DB_AGENT_MYSQL_PASSWORD:-}" =~ ^[A-Za-z0-9_-]{24,128}$ ]]; then
        echo 'DB_AGENT_MYSQL_PASSWORD must contain 24-128 letters, digits, _ or -.' >&2
        exit 1
    fi

    printf "CREATE USER 'db_agent_reader'@'%%' IDENTIFIED BY '%s';\n" \
        "$DB_AGENT_MYSQL_PASSWORD" | docker_process_sql --database=mysql
    docker_process_sql --database=mysql <<'SQL'
GRANT SELECT, SHOW VIEW ON `db\_agent`.* TO 'db_agent_reader'@'%';
SQL
)
