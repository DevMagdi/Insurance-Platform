"""Recovery of inbound call recordings sealed before revision 038."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.scripts.repair_call_recording_aad import legacy_call_id, repair
from app.security.blobs import RECORDING, BlobCipher
from app.security.integration_crypto import SecretCipher
from app.services.integrations import call_transcript_aad

AUDIO = b"RIFF....the caller speaking"
TRANSCRIPT = b"The caller asked about the delay on their claim"


class _StubConnection:
    """Just enough psycopg for the batched loop: one page, then nothing."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._pages = [rows, []]
        self.updates: list[tuple[Any, ...]] = []

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        if sql.strip().startswith("UPDATE"):
            self.updates.append(params)
            return SimpleNamespace(fetchall=list)
        page = self._pages.pop(0) if self._pages else []
        return SimpleNamespace(fetchall=lambda: page)


def _broken_row(blobs: BlobCipher, cipher: SecretCipher) -> tuple[tuple[Any, ...], Any, Any]:
    """A row as the pre-038 worker wrote it: blobs bound to a derived id."""
    company, provider_call_id, row_id = uuid4(), "CA0123456789", uuid4()
    sealed_against = legacy_call_id(company, provider_call_id)
    recording = blobs.encrypt(AUDIO, purpose=RECORDING, resource_id=sealed_against)
    transcript = cipher.encrypt(TRANSCRIPT, associated_data=call_transcript_aad(sealed_against))
    return (row_id, provider_call_id, company, recording, transcript), row_id, sealed_against


def test_repair_reseals_a_legacy_recording_against_its_own_row() -> None:
    blobs, cipher = BlobCipher(b"b" * 32), SecretCipher(b"p" * 32)
    row, row_id, sealed_against = _broken_row(blobs, cipher)
    assert sealed_against != row_id
    # This is the defect: the stored row cannot open its own recording.
    with pytest.raises(ValueError):
        blobs.decrypt(bytes(row[3]), purpose=RECORDING, resource_id=row_id)

    conn = _StubConnection([row])
    seen, stale, repaired = repair(conn, blobs, cipher, check=False, batch=50)

    assert (seen, stale, repaired) == (1, 1, 1)
    new_recording, new_transcript, updated_id = conn.updates[0]
    assert updated_id == row_id
    assert blobs.decrypt(new_recording, purpose=RECORDING, resource_id=row_id) == AUDIO
    assert cipher.decrypt(new_transcript, associated_data=call_transcript_aad(row_id)) == TRANSCRIPT


def test_repair_check_mode_reports_without_writing() -> None:
    blobs, cipher = BlobCipher(b"b" * 32), SecretCipher(b"p" * 32)
    row, _row_id, _ = _broken_row(blobs, cipher)
    conn = _StubConnection([row])

    seen, stale, repaired = repair(conn, blobs, cipher, check=True, batch=50)

    assert (seen, stale, repaired) == (1, 1, 0)
    assert conn.updates == []


def test_repair_leaves_a_correctly_sealed_row_untouched() -> None:
    """Running twice, or on a database written by revision 038, is a no-op."""
    blobs, cipher = BlobCipher(b"b" * 32), SecretCipher(b"p" * 32)
    company, provider_call_id = uuid4(), "CA0123456789"
    row_id = legacy_call_id(company, provider_call_id)
    row = (
        row_id,
        provider_call_id,
        company,
        blobs.encrypt(AUDIO, purpose=RECORDING, resource_id=row_id),
        cipher.encrypt(TRANSCRIPT, associated_data=call_transcript_aad(row_id)),
    )
    conn = _StubConnection([row])

    seen, stale, repaired = repair(conn, blobs, cipher, check=False, batch=50)

    assert (seen, stale, repaired) == (1, 0, 0)
    assert conn.updates == []


def test_repair_reports_and_skips_a_row_no_key_can_open(capsys: pytest.CaptureFixture[str]) -> None:
    """A blob from another key is evidence: report it, never overwrite it."""
    blobs, cipher = BlobCipher(b"b" * 32), SecretCipher(b"p" * 32)
    stranger = BlobCipher(b"z" * 32)
    company, provider_call_id, row_id = uuid4(), "CA0123456789", uuid4()
    row = (
        row_id,
        provider_call_id,
        company,
        stranger.encrypt(AUDIO, purpose=RECORDING, resource_id=uuid4()),
        None,
    )
    conn = _StubConnection([row])

    seen, stale, repaired = repair(conn, blobs, cipher, check=False, batch=50)

    assert (seen, stale, repaired) == (1, 0, 0)
    assert conn.updates == []
    assert "unreadable under either key" in capsys.readouterr().out
