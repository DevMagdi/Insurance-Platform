"""AES-256-GCM at-rest encryption and independently keyed HMAC lookup.

Keys come from a secret manager in production. The encryption key is a
versioned :class:`~app.security.keyring.Keyring` so it can be rotated online
(``app.scripts.rotate_keys``); rotating the blind-index key re-indexes rows in a
maintenance window because lookups depend on it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from binascii import Error

from app.security.keyring import Keyring, parse_previous_keys
from app.security.national_id import validate_national_id

_NONCE_BYTES = 12
_AAD = b"insurance-platform:customers:national-id:v1"


def decode_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, Error) as exc:
        raise ValueError("Key must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError("Key must decode to exactly 32 bytes")
    return key


def build_keyring(primary: str, primary_id: int = 1, previous: str = "") -> Keyring:
    """Keyring from ``APP_ENCRYPTION_KEY`` / ``_KEY_ID`` / ``_PREVIOUS_KEYS`` values."""
    keys = {primary_id: decode_key(primary)}
    for key_id, encoded in parse_previous_keys(previous).items():
        if key_id == primary_id:
            raise ValueError("A previous key cannot reuse the primary key id")
        keys[key_id] = decode_key(encoded)
    return Keyring(primary_id, keys)


def idempotency_root(keyring: Keyring, explicit: str | None = None) -> bytes:
    """Stable de-duplication HMAC root that survives encryption-key rotation."""
    return decode_key(explicit) if explicit else keyring.key(min(keyring.key_ids()))


class NationalIdProtector:
    def __init__(self, encryption_key: bytes | Keyring, blind_index_key: bytes) -> None:
        keyring = (
            encryption_key
            if isinstance(encryption_key, Keyring)
            else Keyring.single(encryption_key)
        )
        if len(blind_index_key) != 32:
            raise ValueError("Encryption and blind-index keys must each be 32 bytes")
        if keyring.contains_key(blind_index_key):
            raise ValueError("Encryption and blind-index keys must be independent")
        self.keyring = keyring
        self._index_key = blind_index_key

    def encrypt(self, national_id: str) -> bytes:
        plaintext = validate_national_id(national_id).encode("ascii")
        return self.keyring.encrypt(plaintext, _AAD)

    def decrypt(self, blob: bytes) -> str:
        if len(blob) < _NONCE_BYTES + 16:
            raise ValueError("Invalid encrypted national ID")
        return validate_national_id(self.keyring.decrypt(blob, _AAD).decode("ascii"))

    def needs_rotation(self, blob: bytes) -> bool:
        return self.keyring.needs_rotation(blob)

    def blind_index(self, national_id: str) -> bytes:
        plaintext = validate_national_id(national_id).encode("ascii")
        return hmac.new(self._index_key, plaintext, hashlib.sha256).digest()
