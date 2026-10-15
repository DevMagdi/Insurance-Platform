#!/bin/sh
# Local development only. Production uses scoped service accounts and private buckets.
set -eu
attempt=0
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo 'MinIO did not become ready within 60 seconds' >&2
        exit 1
    fi
    sleep 2
done
mc mb --ignore-existing "local/$S3_BUCKET"
mc anonymous set none "local/$S3_BUCKET"
