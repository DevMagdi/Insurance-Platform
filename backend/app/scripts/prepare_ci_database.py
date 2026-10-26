"""Provision restricted application roles for an ephemeral CI PostgreSQL database.

This helper is intended only for disposable local/CI databases. Production roles
are managed by infrastructure automation and must never be initialized this way.
"""

from __future__ import annotations

import os

import psycopg
from psycopg import sql


def main() -> None:
    admin_url = os.environ["DATABASE_ADMIN_URL"].replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    database = os.environ.get("POSTGRES_DB", "insurance")
    roles = (
        ("app_runtime", os.environ["APP_DB_PASSWORD"], "NOBYPASSRLS"),
        ("app_worker", os.environ["APP_WORKER_DB_PASSWORD"], "NOBYPASSRLS"),
    )
    with psycopg.connect(admin_url, autocommit=True) as connection, connection.cursor() as cursor:
        for name, password, rls in roles:
            exists = cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
            statement = (
                sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB {} PASSWORD {}")
                if exists
                else sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB {} PASSWORD {}"
                )
            )
            cursor.execute(
                statement.format(sql.Identifier(name), sql.SQL(rls), sql.Literal(password))
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(name)
                )
            )
    print("Provisioned ephemeral app_runtime and app_worker logins")


if __name__ == "__main__":
    main()
