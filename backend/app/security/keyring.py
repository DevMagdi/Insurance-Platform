"""Versioned AES-256-GCM keyring enabling online encryption-key rotation.

Ciphertext format (v2): ``b"KR" || key_id (1 byte) || nonce (12) || ciphertext+tag``.
Legacy ciphertext from Phases 1-7 is ``nonce || ciphertext+tag`` with no header;
it remains readable with any configured key, so rotation is:

1. deploy with the NEW key as ``APP_ENCRYPTION_KEY`` / ``APP_ENCRYPTION_KEY_ID``
   and the old key listed in ``APP_ENCRYPTION_PREVIOUS_KEYS``;
2. run ``python -m app.scripts.rotate_keys`` to re-encrypt stored values;
3. remove the old key once the script reports zero stale rows.

AES-GCM authentication makes format detection unambiguous: a header collision
on a legacy nonce simply fails authentication and falls back to legacy parsing.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_HEADER = b"KR"
_NONCE = 12
_TAG = 16


def parse_previous_keys(value: str) -> dict[int, str]:
    """``"1:base64key,2:base64key"`` → ``{1: "base64key", 2: "base64key"}``."""
    keys: dict[int, str] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        key_id, sep, encoded = item.partition(":")
        if not sep or not key_id.isdecimal():
            raise ValueError("APP_ENCRYPTION_PREVIOUS_KEYS entries must be id:base64key")
        keys[int(key_id)] = encoded
    return keys


class Keyring:
    def __init__(self, primary_id: int, keys: Mapping[int, bytes]) -> None:
        if not 1 <= primary_id <= 255 or primary_id not in keys:
            raise ValueError("Primary encryption key id must be 1..255 and configured")
        for key_id, key in keys.items():
            if not 1 <= key_id <= 255 or len(key) != 32:
                raise ValueError("Every encryption key must be 32 bytes with id 1..255")
        self.primary_id = primary_id
        self._keys = dict(keys)
        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys.items()}

    @classmethod
    def single(cls, key: bytes, key_id: int = 1) -> Keyring:
        return cls(key_id, {key_id: key})

    @property
    def primary_key(self) -> bytes:
        return self._keys[self.primary_id]

    def key_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def key(self, key_id: int) -> bytes:
        return self._keys[key_id]

    def raw_keys(self) -> tuple[bytes, ...]:
        return tuple(self._keys.values())

    def contains_key(self, candidate: bytes) -> bool:
        return any(hmac.compare_digest(candidate, key) for key in self._keys.values())

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        cipher = self._ciphers[self.primary_id]
        return _HEADER + bytes([self.primary_id]) + nonce + cipher.encrypt(nonce, plaintext, aad)

    def key_id_of(self, blob: bytes) -> int | None:
        """The key id a v2 blob claims (None for legacy blobs)."""
        if len(blob) >= 3 + _NONCE + _TAG and blob[:2] == _HEADER and blob[2] in self._ciphers:
            return blob[2]
        return None

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if len(blob) < _NONCE + _TAG:
            raise ValueError("Invalid encrypted value")
        key_id = self.key_id_of(blob)
        if key_id is not None:
            body = blob[3:]
            try:
                return self._ciphers[key_id].decrypt(body[:_NONCE], body[_NONCE:], aad)
            except InvalidTag:
                pass  # Legacy nonce that happens to start with the header.
        ordered = [self.primary_id, *(k for k in self._ciphers if k != self.primary_id)]
        for candidate in ordered:
            try:
                return self._ciphers[candidate].decrypt(blob[:_NONCE], blob[_NONCE:], aad)
            except InvalidTag:
                continue
        raise InvalidTag  # same contract as AESGCM for callers and tests

    def needs_rotation(self, blob: bytes) -> bool:
        return self.key_id_of(blob) != self.primary_id

    def rotate(self, blob: bytes, aad: bytes) -> bytes:
        return self.encrypt(self.decrypt(blob, aad), aad)
