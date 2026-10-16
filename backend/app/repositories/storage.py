"""Database-backed storage inventory for the first-party staff console.

The listings are produced by ``app_security.storage_inventory``: a bounded,
permission-gated SECURITY DEFINER function that returns metadata only. Attachment,
export and recording bytes stay behind their scoped download routes, so the
inventory can never become a side channel for customer documents.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Logical folders of the single platform database.
KINDS: tuple[str, ...] = ("attachment", "export", "recording")
FOLDERS: tuple[str, ...] = tuple(f"{kind}s/" for kind in KINDS)


@dataclass(frozen=True)
class StorageInventoryRow:
    kind: str
    object_id: UUID
    object_key: str
    size_bytes: int
    last_modified: datetime | None
    checksum_sha256: str | None
    details: dict[str, Any]


@dataclass(frozen=True)
class StorageInventoryPage:
    rows: tuple[StorageInventoryRow, ...]
    is_truncated: bool
    next_page_token: str | None


def encode_cursor(kind: str, object_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{kind}:{object_id}".encode()).decode().rstrip("=")


def decode_cursor(token: str) -> tuple[str, UUID] | None:
    """Opaque keyset cursor; malformed input is rejected instead of guessed."""
    if len(token) > 128:
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        kind, _, raw = base64.urlsafe_b64decode(padded.encode()).decode("ascii").partition(":")
        return kind, UUID(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


class StorageInventoryRepository:
    """Read the platform's own tables; never touch a bucket or return file bytes."""

    async def totals(self, db: AsyncSession) -> dict[str, Any]:
        rows = (await db.execute(text("SELECT * FROM app_security.storage_totals()"))).all()
        per_kind = {
            str(row.kind): {
                "objects": int(row.objects),
                "bytes": int(row.bytes),
                "attention": int(row.attention),
                "missing_content": int(row.missing_content),
            }
            for row in rows
        }
        return {
            "objects": sum(item["objects"] for item in per_kind.values()),
            "bytes": sum(item["bytes"] for item in per_kind.values()),
            "pending_scans": per_kind.get("attachment", {}).get("attention", 0),
            "attachment_metadata_without_content": (
                per_kind.get("attachment", {}).get("missing_content", 0)
            ),
            "by_kind": per_kind,
        }

    async def page(
        self, db: AsyncSession, kind: str, after: UUID | None, limit: int
    ) -> StorageInventoryPage:
        rows = (
            await db.execute(
                text("SELECT * FROM app_security.storage_inventory(:kind, :after, :limit)"),
                {"kind": kind, "after": after, "limit": limit + 1},
            )
        ).all()
        # One extra row tells us whether another page exists without a second query.
        page_rows = rows[:limit]
        records = tuple(
            StorageInventoryRow(
                kind=str(row.kind),
                object_id=row.object_id,
                object_key=str(row.object_key),
                size_bytes=int(row.size_bytes),
                last_modified=row.last_modified,
                checksum_sha256=row.checksum_sha256,
                details=dict(row.details or {}),
            )
            for row in page_rows
        )
        truncated = len(rows) > limit
        next_token = (
            encode_cursor(records[-1].kind, records[-1].object_id)
            if truncated and records
            else None
        )
        return StorageInventoryPage(
            rows=records, is_truncated=truncated, next_page_token=next_token
        )
