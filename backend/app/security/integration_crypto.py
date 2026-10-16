"""AES-GCM envelopes for integration credentials and queued PII-bearing payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from cryptography.exceptions import InvalidTag

from app.security.keyring import Keyring

_NONCE_BYTES = 12


def derive_idempotency_key(master_key: bytes) -> bytes:
    if len(master_key) != 32:
        raise ValueError("Integration master key must be exactly 32 bytes")
    return hmac.new(
        master_key, b"insurance-platform:integration-idempotency:v1", hashlib.sha256
    ).digest()


class SecretCipher:
    """Purpose-bound encryption using the application's independent 256-bit key."""

    def __init__(self, key: bytes | Keyring) -> None:
        if isinstance(key, Keyring):
            self.keyring = key
        else:
            if len(key) != 32:
                raise ValueError("Integration encryption key must be exactly 32 bytes")
            self.keyring = Keyring.single(key)

    def encrypt(self, plaintext: bytes, *, associated_data: str) -> bytes:
        if not associated_data or len(associated_data) > 512:
            raise ValueError("Invalid encryption context")
        return self.keyring.encrypt(plaintext, associated_data.encode("utf-8"))

    def decrypt(self, ciphertext: bytes, *, associated_data: str) -> bytes:
        if len(ciphertext) < _NONCE_BYTES + 16:
            raise ValueError("Invalid encrypted value")
        try:
            return self.keyring.decrypt(ciphertext, associated_data.encode("utf-8"))
        except InvalidTag as exc:
            raise ValueError("Encrypted value failed authentication") from exc

    def rotate(self, ciphertext: bytes, *, associated_data: str) -> bytes:
        return self.keyring.rotate(ciphertext, associated_data.encode("utf-8"))

    def encrypt_json(self, value: dict[str, Any], *, associated_data: str) -> bytes:
        serialized = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.encrypt(serialized, associated_data=associated_data)

    def decrypt_json(self, ciphertext: bytes, *, associated_data: str) -> dict[str, Any]:
        try:
            value = json.loads(self.decrypt(ciphertext, associated_data=associated_data))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise ValueError("Encrypted JSON is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("Encrypted JSON must contain an object")
        return value
