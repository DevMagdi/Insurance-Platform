"""AES-256-GCM at-rest encryption and independently keyed HMAC lookup.

Keys come from a secret manager in production; rotating either key requires a
separately planned, audited re-encryption / re-indexing migration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from binascii import Error
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.security.national_id import validate_national_id

_NONCE_BYTES = 12
_AAD = b"insurance-platform:customers:national-id:v1"
_PAYLOAD_AAD = b"insurance-platform:ai-insights:payload:v1"


def decode_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, Error) as exc:
        raise ValueError("Key must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError("Key must decode to exactly 32 bytes")
    return key


class NationalIdProtector:
    def __init__(self, encryption_key: bytes, blind_index_key: bytes) -> None:
        if len(encryption_key) != 32 or len(blind_index_key) != 32:
            raise ValueError("Encryption and blind-index keys must each be 32 bytes")
        if hmac.compare_digest(encryption_key, blind_index_key):
            raise ValueError("Encryption and blind-index keys must be independent")
        self._cipher = AESGCM(encryption_key)
        self._index_key = blind_index_key

    def encrypt(self, national_id: str) -> bytes:
        plaintext = validate_national_id(national_id).encode("ascii")
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._cipher.encrypt(nonce, plaintext, _AAD)

    def decrypt(self, blob: bytes) -> str:
        if len(blob) < _NONCE_BYTES + 16:
            raise ValueError("Invalid encrypted national ID")
        return validate_national_id(
            self._cipher.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], _AAD).decode("ascii")
        )

    def blind_index(self, national_id: str) -> bytes:
        plaintext = validate_national_id(national_id).encode("ascii")
        return hmac.new(self._index_key, plaintext, hashlib.sha256).digest()


class PayloadProtector:
    """AES-256-GCM encryption for structured AI payloads (never plaintext PII)."""

    def __init__(self, encryption_key: bytes) -> None:
        if len(encryption_key) != 32:
            raise ValueError("Encryption key must be exactly 32 bytes")
        self._cipher = AESGCM(encryption_key)

    def encrypt_json(self, payload: dict[str, Any]) -> bytes:
        plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._cipher.encrypt(nonce, plaintext, _PAYLOAD_AAD)

    def decrypt_json(self, blob: bytes) -> dict[str, Any]:
        if len(blob) < _NONCE_BYTES + 16:
            raise ValueError("Invalid encrypted payload")
        plaintext = self._cipher.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], _PAYLOAD_AAD)
        value = json.loads(plaintext.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Encrypted payload must be a JSON object")
        return value
