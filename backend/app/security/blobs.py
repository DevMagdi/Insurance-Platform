"""Purpose-bound AES-256-GCM envelopes for private binaries held in PostgreSQL.

Attachments, generated export artifacts and call recordings are encrypted with the
application keyring (``APP_ENCRYPTION_KEY``) *before* they reach the database, so a
database dump, a replica or a stolen disk is useless without the deployment secret.
The authenticated context binds every blob to the row that owns it, so presenting
one row's ciphertext as another row's content fails authentication instead of
returning attacker-chosen bytes. Key rotation stays online because the keyring
header records which key produced each blob (see ``app.scripts.rotate_keys``).
"""

from __future__ import annotations

from uuid import UUID

from cryptography.exceptions import InvalidTag

from app.security.keyring import Keyring

# Blob purposes; the string is part of the AEAD context and must never be repurposed.
ATTACHMENT = "attachment"
EXPORT = "export"
RECORDING = "recording"
STAFF_PROFILE = "staff-profile"
AVATAR = "staff-avatar"
PURPOSES = frozenset({ATTACHMENT, EXPORT, RECORDING, STAFF_PROFILE, AVATAR})

# "KR" magic (2) + key id (1) + nonce (12) + GCM tag (16): the smallest stored blob.
ENCRYPTION_OVERHEAD_BYTES = 31


class BlobCipher:
    """Encrypt/decrypt single-row binaries with a purpose and row-bound context."""

    def __init__(self, key: Keyring | bytes) -> None:
        if isinstance(key, Keyring):
            self.keyring = key
        elif len(key) == 32:
            self.keyring = Keyring.single(key)
        else:
            raise ValueError("Blob encryption key must be exactly 32 bytes")

    @staticmethod
    def associated_data(purpose: str, resource_id: UUID) -> bytes:
        if purpose not in PURPOSES:
            raise ValueError("Unknown blob purpose")
        return f"insurance-platform:{purpose}:{resource_id}:v1".encode()

    def encrypt(self, content: bytes, *, purpose: str, resource_id: UUID) -> bytes:
        if not content:
            raise ValueError("Refusing to store empty content")
        return self.keyring.encrypt(content, self.associated_data(purpose, resource_id))

    def decrypt(self, content: bytes, *, purpose: str, resource_id: UUID) -> bytes:
        try:
            return self.keyring.decrypt(content, self.associated_data(purpose, resource_id))
        except InvalidTag as exc:
            raise ValueError("Stored content failed authentication") from exc

    def needs_rotation(self, content: bytes) -> bool:
        return self.keyring.needs_rotation(content)
