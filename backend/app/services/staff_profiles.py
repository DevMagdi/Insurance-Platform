"""Encrypted staff identity and safe, bounded profile-photo normalization."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import warnings
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.staff_profiles import StaffProfileRepository
from app.security.blobs import AVATAR, STAFF_PROFILE, BlobCipher

AVATAR_MAX_BYTES = 512 * 1024
AVATAR_MAX_PIXELS = 16_000_000


def normalize_avatar(encoded: str) -> bytes:
    """Never serve arbitrary uploaded bytes, SVG, EXIF/GPS or an image polyglot."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Photo must be valid base64") from exc
    if not raw or len(raw) > AVATAR_MAX_BYTES:
        raise ValueError("Photo must be at most 512 KiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if (
                    image.format not in {"JPEG", "PNG", "WEBP"}
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("Choose a single JPEG, PNG or WebP photo")
                if image.width * image.height > AVATAR_MAX_PIXELS:
                    raise ValueError("Photo dimensions are too large")
                image.load()  # validates truncated/corrupt pixel data before persistence
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((512, 512))
                # A new canvas drops all embedded metadata, including PNG text and EXIF.
                rgba = oriented.convert("RGBA")
                clean = Image.new("RGB", rgba.size, "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=85, optimize=True)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Photo is not a valid JPEG, PNG or WebP image") from exc


class StaffProfileService:
    def __init__(self, blobs: BlobCipher, repository: StaffProfileRepository | None = None) -> None:
        self.blobs = blobs
        self.repository = repository or StaffProfileRepository()

    async def normalized_avatar(self, encoded: str) -> bytes:
        return await asyncio.to_thread(normalize_avatar, encoded)

    async def profile(self, db: AsyncSession, user_id: UUID) -> dict[str, Any]:
        row = await self.repository.profile(db, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Staff profile unavailable")
        metadata: dict[str, Any] = dict(row["metadata"])
        has_avatar = metadata.pop("has_avatar", False)
        personal = (
            json.loads(
                self.blobs.decrypt(
                    row["personal_ciphertext"], purpose=STAFF_PROFILE, resource_id=user_id
                )
            )
            if row["personal_ciphertext"] is not None
            else None
        )
        if personal is not None:
            # The administrative name-edit flow may have updated the directory name.
            personal["full_name"] = metadata["full_name"]
            nid = personal["national_id"]
            year = (1900 if nid[0] == "2" else 2000) + int(nid[1:3])
            personal["date_of_birth"] = f"{year:04}-{nid[3:5]}-{nid[5:7]}"
        return {
            **metadata,
            "personal": personal,
            "avatar_url": f"/api/staff/users/{user_id}/avatar" if has_avatar else None,
        }

    async def avatar(self, db: AsyncSession, user_id: UUID) -> bytes:
        encrypted = await self.repository.avatar(db, user_id)
        if encrypted is None:
            raise HTTPException(status_code=404, detail="Staff photo unavailable")
        return self.blobs.decrypt(encrypted, purpose=AVATAR, resource_id=user_id)
