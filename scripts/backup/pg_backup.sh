#!/bin/sh
# Logical PostgreSQL backup (custom format) with checksum, optional encryption,
# optional off-site upload and local retention. POSIX sh: runs on the host, in
# the postgres:16-alpine container or in a Kubernetes CronJob.
#
# Connection: standard libpq variables (PGHOST, PGPORT, PGUSER, PGPASSWORD,
# PGDATABASE) or DATABASE_URL-style PGURI. Use the migrator/superuser role: the
# runtime role cannot read every row (RLS) and would produce a partial dump.
#
#   BACKUP_DIR=/var/backups/insurance   (default ./backups)
#   BACKUP_RETENTION_DAYS=14            local copies to keep (off-site has its own lifecycle)
#   BACKUP_GPG_RECIPIENT=ops@example    encrypt with gpg public key (recommended off-host)
#   BACKUP_S3_URI=s3://bucket/prefix    upload with the aws CLI (SSE enforced by bucket policy)
set -eu

BACKUP_DIR=${BACKUP_DIR:-./backups}
RETENTION=${BACKUP_RETENTION_DAYS:-14}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DB=${PGDATABASE:-insurance}
mkdir -p "$BACKUP_DIR"
umask 077

OUT="$BACKUP_DIR/${DB}-${STAMP}.dump"
echo "pg_backup: dumping ${DB} -> ${OUT}" >&2
if [ -n "${PGURI:-}" ]; then
    pg_dump --format=custom --compress=6 --no-password --dbname="$PGURI" --file="$OUT"
else
    pg_dump --format=custom --compress=6 --no-password --file="$OUT" "$DB"
fi
# A dump that pg_restore cannot list is not a backup.
pg_restore --list "$OUT" >/dev/null

if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
    gpg --batch --yes --trust-model always --recipient "$BACKUP_GPG_RECIPIENT" \
        --output "$OUT.gpg" --encrypt "$OUT"
    rm -f "$OUT"
    OUT="$OUT.gpg"
fi

(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

if [ -n "${BACKUP_S3_URI:-}" ]; then
    aws s3 cp "$OUT" "${BACKUP_S3_URI%/}/$(basename "$OUT")" --only-show-errors
    aws s3 cp "$OUT.sha256" "${BACKUP_S3_URI%/}/$(basename "$OUT").sha256" --only-show-errors
fi

# Local retention only touches files this script produced.
find "$BACKUP_DIR" -maxdepth 1 -type f -name "${DB}-*.dump*" -mtime +"$RETENTION" -exec rm -f {} + 2>/dev/null || true

echo "pg_backup: ok $(wc -c < "$OUT") bytes" >&2
echo "$OUT"
