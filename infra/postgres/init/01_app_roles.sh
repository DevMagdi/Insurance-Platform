#!/bin/sh
# Executed once by the official postgres entrypoint on a NEW dev data volume.
set -eu
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}"
: "${APP_WORKER_DB_PASSWORD:?APP_WORKER_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v runtime_password="$APP_DB_PASSWORD" -v worker_password="$APP_WORKER_DB_PASSWORD" \
    -v db_name="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE app_runtime LOGIN NOBYPASSRLS PASSWORD %L', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') \gexec
ALTER ROLE app_runtime NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
GRANT CONNECT ON DATABASE :"db_name" TO app_runtime;
-- The restricted worker login is provisioned here, next to app_runtime, so its
-- password never depends on the environment of the migrating process. The
-- 006 migration still promotes the role on volumes that predate this script.
SELECT format('CREATE ROLE app_worker LOGIN PASSWORD %L', :'worker_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') \gexec
ALTER ROLE app_worker NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
GRANT CONNECT ON DATABASE :"db_name" TO app_worker;
SQL
