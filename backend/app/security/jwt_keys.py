"""Ed25519 JWT key loading, validation and verification-key rotation support."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import TypeAlias

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PrivateKey: TypeAlias = Ed25519PrivateKey
PublicKey: TypeAlias = Ed25519PublicKey
_KID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class JWTKeyRing:
    active_key_id: str
    private_key: PrivateKey
    verification_keys: dict[str, PublicKey]


def _decode_pem(value: str, label: str) -> bytes:
    try:
        padded = value.strip() + "=" * (-len(value.strip()) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{label} must be URL-safe base64-encoded PEM") from exc


def _private(value: str) -> PrivateKey:
    key = serialization.load_pem_private_key(_decode_pem(value, "JWT_PRIVATE_KEY_B64"), None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("JWT private key must be an Ed25519 key")
    return key


def _public(value: str) -> PublicKey:
    key = serialization.load_pem_public_key(_decode_pem(value, "JWT_PUBLIC_KEY_B64"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("JWT verification keys must be Ed25519 keys")
    return key


def _check_kid(value: str) -> str:
    if not _KID.fullmatch(value):
        raise ValueError(
            "JWT_KEY_ID must contain 1-64 letters, digits, dots, underscores or dashes"
        )
    return value


def build_keyring(
    *,
    active_key_id: str,
    private_key_b64: str | None,
    public_key_b64: str | None,
    previous_public_keys: str,
    development_secret: str,
    allow_derived_key: bool,
) -> JWTKeyRing:
    """Load the active pair and historical public keys; never derive prod keys.

    Previous keys use ``kid:base64url(PEM)`` entries separated by commas. Keep
    old public keys until every token they signed has expired before removing.
    """
    kid = _check_kid(active_key_id)
    if bool(private_key_b64) != bool(public_key_b64):
        raise ValueError("Configure both JWT_PRIVATE_KEY_B64 and JWT_PUBLIC_KEY_B64")
    if private_key_b64 and public_key_b64:
        private = _private(private_key_b64)
        public = _public(public_key_b64)
        if private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ) != public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw):
            raise ValueError("JWT private and public keys do not match")
    elif allow_derived_key:
        # Deterministic, non-production convenience for tests and legacy local
        # setups. Production configuration is rejected unless an explicit pair
        # is supplied; all emitted tokens still use EdDSA, never HS256.
        private = Ed25519PrivateKey.from_private_bytes(
            hashlib.sha256(development_secret.encode("utf-8")).digest()
        )
        public = private.public_key()
    else:
        raise ValueError("Production requires JWT_PRIVATE_KEY_B64 and JWT_PUBLIC_KEY_B64")

    verification: dict[str, PublicKey] = {kid: public}
    if previous_public_keys.strip():
        for entry in previous_public_keys.split(","):
            old_kid, separator, encoded = entry.strip().partition(":")
            if not separator or not encoded:
                raise ValueError("JWT_PREVIOUS_PUBLIC_KEYS entries must be kid:base64url-PEM")
            old_kid = _check_kid(old_kid)
            if old_kid in verification:
                raise ValueError("JWT key IDs must be unique")
            verification[old_kid] = _public(encoded)
    return JWTKeyRing(kid, private, verification)
