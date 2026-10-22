"""Re-seal inbound call recordings written before revision 038.

Until revision 038 the worker encrypted an inbound recording and transcript
against a *derived* identifier -- uuid5 over the company and the provider's
call id -- because ``record_request_call`` generated the primary key itself
and only revealed it after the insert. Every reader rebuilds the decryption
context from ``call_logs.id``, so those rows were stored intact and could
never be opened: the staff download route answers 503, the transcript stays
blank and ``rotate_keys`` cannot re-encrypt them.

Revision 038 makes the worker name the row, which fixes new calls. Rows
already on disk still carry the old binding, and nothing about them is lost:
the derived identifier is a pure function of two columns that are still
there. This script recomputes it, opens each blob with the old context and
re-seals it under the row's real id.

Usage (admin/migration DB role, the same environment ``rotate_keys`` needs)::

    python -m app.scripts.repair_call_recording_aad --check
    python -m app.scripts.repair_call_recording_aad

``--check`` only reports. Both modes are idempotent and restartable: a row
that already opens under its own id is left untouched, so running the script
twice, or against a database that was never affected, changes nothing.
"""

from __future__ import annotations

import argparse
import os
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from app.security.blobs import RECORDING, BlobCipher
from app.security.crypto import build_keyring
from app.security.integration_crypto import SecretCipher
from app.services.integrations import call_transcript_aad

# Exactly the expression the pre-038 worker used, kept verbatim: this is the
# only thing that can still name those ciphertexts.
LEGACY_NAME = "insurance-platform:inbound-call:{company_id}:{provider_call_id}"

SELECT_ROWS = """
    SELECT l.id, l.provider_call_id, c.company_id, l.recording_content,
           l.transcript_ciphertext
    FROM public.call_logs l
    JOIN public.complaints c
      ON c.id = l.complaint_id AND c.created_at = l.complaint_created_at
    WHERE l.direction = 'inbound'
      AND l.provider_call_id IS NOT NULL
      AND (l.recording_content IS NOT NULL OR l.transcript_ciphertext IS NOT NULL)
      AND l.id > %s
    ORDER BY l.id
    LIMIT %s
"""
UPDATE_ROW = """
    UPDATE public.call_logs
    SET recording_content = %s, transcript_ciphertext = %s
    WHERE id = %s
"""


def legacy_call_id(company_id: UUID, provider_call_id: str) -> UUID:
    """The identifier the pre-038 worker bound the ciphertexts to."""
    return uuid5(
        NAMESPACE_URL,
        LEGACY_NAME.format(company_id=company_id, provider_call_id=provider_call_id),
    )


def _reseal_recording(blobs: BlobCipher, blob: bytes, old: UUID, new: UUID) -> bytes | None:
    """Return the recording re-sealed under ``new``, or None to leave it alone."""
    try:
        blobs.decrypt(blob, purpose=RECORDING, resource_id=new)
    except ValueError:
        pass
    else:
        return None  # Already bound to its own row.
    audio = blobs.decrypt(blob, purpose=RECORDING, resource_id=old)
    return blobs.encrypt(audio, purpose=RECORDING, resource_id=new)


def _reseal_transcript(cipher: SecretCipher, blob: bytes, old: UUID, new: UUID) -> bytes | None:
    """Same for the transcript, which is sealed with its own context string."""
    try:
        cipher.decrypt(blob, associated_data=call_transcript_aad(new))
    except ValueError:
        pass
    else:
        return None
    text = cipher.decrypt(blob, associated_data=call_transcript_aad(old))
    return cipher.encrypt(text, associated_data=call_transcript_aad(new))


def repair(
    conn: psycopg.Connection[Any],
    blobs: BlobCipher,
    cipher: SecretCipher,
    *,
    check: bool,
    batch: int,
) -> tuple[int, int, int]:
    """Returns (rows_seen, rows_needing_repair, rows_repaired)."""
    seen = stale = repaired = 0
    cursor_key: Any = "00000000-0000-0000-0000-000000000000"
    while True:
        with conn.transaction():
            rows = conn.execute(SELECT_ROWS, (cursor_key, batch)).fetchall()
            if not rows:
                break
            for call_id, provider_call_id, company_id, recording, transcript in rows:
                cursor_key = call_id
                seen += 1
                old = legacy_call_id(company_id, provider_call_id)
                if old == call_id:
                    continue  # Written by revision 038 or later.
                try:
                    new_recording = (
                        _reseal_recording(blobs, bytes(recording), old, call_id)
                        if recording is not None
                        else None
                    )
                    new_transcript = (
                        _reseal_transcript(cipher, bytes(transcript), old, call_id)
                        if transcript is not None
                        else None
                    )
                except ValueError:
                    # Neither context opens it. Re-encrypting is impossible and
                    # overwriting would destroy evidence, so report and move on.
                    print(f"call_logs {call_id}: unreadable under either key, left untouched")
                    continue
                if new_recording is None and new_transcript is None:
                    continue
                stale += 1
                if check:
                    continue
                conn.execute(
                    UPDATE_ROW,
                    (
                        new_recording if new_recording is not None else recording,
                        new_transcript if new_transcript is not None else transcript,
                        call_id,
                    ),
                )
                repaired += 1
    return seen, stale, repaired


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="report affected rows only")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args(argv)
    url = os.environ["DATABASE_ADMIN_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    keyring = build_keyring(
        os.environ["APP_ENCRYPTION_KEY"],
        int(os.environ.get("APP_ENCRYPTION_KEY_ID", "1")),
        os.environ.get("APP_ENCRYPTION_PREVIOUS_KEYS", ""),
    )
    with psycopg.connect(url, prepare_threshold=None) as conn:
        seen, stale, repaired = repair(
            conn,
            BlobCipher(keyring),
            SecretCipher(keyring),
            check=args.check,
            batch=args.batch,
        )
    print(f"inbound calls inspected={seen} needing repair={stale} repaired={repaired}")
    return 1 if args.check and stale else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
