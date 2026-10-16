"""Online re-encryption after an ``APP_ENCRYPTION_KEY`` rotation.

Procedure (docs/RUNBOOK.md#encryption-key-rotation):

1. Generate a new key; deploy with ``APP_ENCRYPTION_KEY=<new>``,
   ``APP_ENCRYPTION_KEY_ID=<n+1>`` and ``APP_ENCRYPTION_PREVIOUS_KEYS=<n>:<old>``.
   Set ``APP_IDEMPOTENCY_KEY=<original key>`` the first time you rotate so
   integration de-duplication digests stay stable.
2. Run ``python -m app.scripts.rotate_keys`` (migration/admin DB role). It walks
   every encrypted column in small committed batches, re-encrypting only values
   that are not yet under the primary key. It is idempotent and restartable.
3. Run it again with ``--check``; when it reports 0 stale values, remove the old
   key from ``APP_ENCRYPTION_PREVIOUS_KEYS`` and redeploy.

Values whose AAD depends on other columns are rebuilt with the same context
functions the application uses, so authenticated context binding is preserved.
This includes the private binaries stored in PostgreSQL (attachments, export
artifacts and call recordings): they are re-encrypted row by row like any other
column, so no second store has to be drained separately.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from app.security.blobs import ATTACHMENT, EXPORT, RECORDING, BlobCipher
from app.security.crypto import _AAD as NATIONAL_ID_AAD
from app.security.crypto import build_keyring
from app.security.keyring import Keyring
from app.security.passwords import _TOTP_AAD
from app.services.integrations import (
    ai_insight_aad,
    call_transcript_aad,
    config_aad,
    job_aad,
    webhook_secret_aad,
)


@dataclass(frozen=True)
class Column:
    table: str
    key: str  # primary-key column(s), comma separated
    column: str
    context: tuple[str, ...]  # extra columns needed to rebuild the AAD
    aad: Callable[[dict[str, Any]], bytes]


def _text(value: str) -> bytes:
    return value.encode("utf-8")


COLUMNS: tuple[Column, ...] = (
    Column("customers", "id", "national_id_ciphertext", (), lambda _r: NATIONAL_ID_AAD),
    Column("staff_users", "id", "totp_secret_ciphertext", (), lambda _r: _TOTP_AAD),
    Column(
        "integrations_config",
        "id",
        "config_ciphertext",
        ("company_id", "provider"),
        lambda r: _text(config_aad(r["company_id"], r["provider"])),
    ),
    Column(
        "webhooks",
        "id",
        "signing_secret_ciphertext",
        (),
        lambda r: _text(webhook_secret_aad(r["id"])),
    ),
    Column(
        "integration_jobs",
        "id",
        "payload_ciphertext",
        ("idempotency_digest",),
        lambda r: _text(job_aad(bytes(r["idempotency_digest"]))),
    ),
    Column(
        "ai_insights",
        "id",
        "payload_ciphertext",
        ("integration_job_id", "kind"),
        lambda r: _text(ai_insight_aad(r["integration_job_id"], r["kind"])),
    ),
    Column(
        "call_logs",
        "id",
        "transcript_ciphertext",
        (),
        lambda r: _text(call_transcript_aad(r["id"])),
    ),
    Column(
        "attachments",
        "id",
        "content",
        (),
        lambda r: BlobCipher.associated_data(ATTACHMENT, r["id"]),
    ),
    Column(
        "export_jobs",
        "id",
        "content",
        (),
        lambda r: BlobCipher.associated_data(EXPORT, r["id"]),
    ),
    Column(
        "call_logs",
        "id",
        "recording_content",
        (),
        lambda r: BlobCipher.associated_data(RECORDING, r["id"]),
    ),
)


def _columns_present(conn: psycopg.Connection[Any], spec: Column) -> bool:
    needed = {spec.column, *spec.context, *spec.key.split(",")}
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (spec.table,),
    ).fetchall()
    return needed <= {row[0] for row in rows}


def rotate_column(
    conn: psycopg.Connection[Any], keyring: Keyring, spec: Column, *, check: bool, batch: int
) -> tuple[int, int]:
    """Returns (stale_found, rotated)."""
    if not _columns_present(conn, spec):
        return 0, 0
    # Identifiers come from the constant COLUMNS table above, never from input.
    select = (
        f"SELECT {spec.key}, {spec.column}{''.join(', ' + c for c in spec.context)} "
        f"FROM public.{spec.table} WHERE {spec.column} IS NOT NULL "
        f"AND {spec.key} > %s ORDER BY {spec.key} LIMIT %s"
    )
    update = f"UPDATE public.{spec.table} SET {spec.column} = %s WHERE {spec.key} = %s"
    stale = rotated = 0
    cursor_key: Any = "00000000-0000-0000-0000-000000000000"
    while True:
        with conn.transaction():
            rows = conn.execute(select, (cursor_key, batch)).fetchall()
            if not rows:
                break
            names = [spec.key, spec.column, *spec.context]
            for raw in rows:
                row = dict(zip(names, raw, strict=True))
                cursor_key = row[spec.key]
                blob = bytes(row[spec.column])
                if not keyring.needs_rotation(blob):
                    continue
                stale += 1
                if check:
                    continue
                conn.execute(update, (keyring.rotate(blob, spec.aad(row)), row[spec.key]))
                rotated += 1
    return stale, rotated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="report stale values only")
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args(argv)
    url = os.environ["DATABASE_ADMIN_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    keyring = build_keyring(
        os.environ["APP_ENCRYPTION_KEY"],
        int(os.environ.get("APP_ENCRYPTION_KEY_ID", "1")),
        os.environ.get("APP_ENCRYPTION_PREVIOUS_KEYS", ""),
    )
    total_stale = 0
    with psycopg.connect(url, prepare_threshold=None) as conn:
        for spec in COLUMNS:
            stale, rotated = rotate_column(conn, keyring, spec, check=args.check, batch=args.batch)
            total_stale += stale - rotated
            print(f"{spec.table}.{spec.column}: stale={stale} rotated={rotated}")
    print(f"remaining stale values: {total_stale}")
    return 1 if args.check and total_stale else 0


if __name__ == "__main__":
    sys.exit(main())
