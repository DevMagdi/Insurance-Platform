#!/bin/sh
# Restore drill: restore a backup into a throw-away database on the same cluster
# and prove it is usable AND still secure:
#   * checksum matches, pg_restore completes without ignored errors;
#   * schema is at the expected Alembic revision (EXPECTED_REVISION, optional);
#   * row-level security is still enabled on complaints and the runtime role
#     cannot bypass it (as app_runtime with no session context: zero rows);
#   * key tables are non-empty when MIN_COMPLAINTS is set.
#
#   pg_restore_drill.sh backups/insurance-20261016T000000Z.dump
# Connection via libpq variables as the migrator/superuser. Encrypted (.gpg)
# dumps are decrypted with the local gpg keyring first.
set -eu

DUMP=${1:?usage: pg_restore_drill.sh <backup.dump[.gpg]>}
DRILL_DB=${DRILL_DB:-restore_drill_$(date -u +%Y%m%d%H%M%S)}
ADMIN_DB=${PGDATABASE:-insurance}

if [ -f "$DUMP.sha256" ]; then
    (cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256")
fi
case "$DUMP" in
    *.gpg)
        PLAIN="${DUMP%.gpg}.drill"
        gpg --batch --yes --output "$PLAIN" --decrypt "$DUMP"
        trap 'rm -f "$PLAIN"' EXIT
        DUMP=$PLAIN
        ;;
esac

q() { psql --no-password -v ON_ERROR_STOP=1 -At -d "$1" -c "$2"; }

echo "drill: creating ${DRILL_DB}" >&2
q "$ADMIN_DB" "CREATE DATABASE \"${DRILL_DB}\" TEMPLATE template0 ENCODING 'UTF8'"
cleanup_db() {
    if [ "${KEEP_DRILL_DB:-0}" != "1" ]; then
        q "$ADMIN_DB" "DROP DATABASE IF EXISTS \"${DRILL_DB}\" WITH (FORCE)" >/dev/null || true
    fi
}
trap 'cleanup_db' EXIT

START=$(date +%s)
pg_restore --no-password --exit-on-error --jobs="${RESTORE_JOBS:-2}" --dbname="$DRILL_DB" "$DUMP"
ELAPSED=$(( $(date +%s) - START ))

REVISION=$(q "$DRILL_DB" "SELECT version_num FROM alembic_version")
if [ -n "${EXPECTED_REVISION:-}" ] && [ "$REVISION" != "$EXPECTED_REVISION" ]; then
    echo "drill: revision ${REVISION} != expected ${EXPECTED_REVISION}" >&2
    exit 1
fi
RLS=$(q "$DRILL_DB" "SELECT relrowsecurity FROM pg_class WHERE oid = 'public.complaints'::regclass")
[ "$RLS" = "t" ] || { echo "drill: RLS disabled on complaints after restore" >&2; exit 1; }
BYPASS=$(q "$DRILL_DB" "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = 'app_runtime'")
[ "$BYPASS" = "f" ] || { echo "drill: app_runtime can bypass RLS" >&2; exit 1; }

TOTAL=$(q "$DRILL_DB" "SELECT count(*) FROM public.complaints")
# SET ROLE drops superuser privileges, so the policies apply exactly as for the API.
LEAKED=$(psql --no-password -v ON_ERROR_STOP=1 -At -d "$DRILL_DB" \
    -c "BEGIN" -c "SET LOCAL ROLE app_runtime" -c "SELECT count(*) FROM public.complaints" -c "ROLLBACK" \
    | grep -E '^[0-9]+$' | tail -1)
[ "$LEAKED" = "0" ] || { echo "drill: runtime role saw ${LEAKED} complaints without context" >&2; exit 1; }
if [ -n "${MIN_COMPLAINTS:-}" ] && [ "$TOTAL" -lt "$MIN_COMPLAINTS" ]; then
    echo "drill: only ${TOTAL} complaints restored (< ${MIN_COMPLAINTS})" >&2
    exit 1
fi

echo "drill: ok revision=${REVISION} complaints=${TOTAL} rls=on runtime_without_context=0 restore_seconds=${ELAPSED}"
