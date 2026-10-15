"""Argon2id staff passwords and separately authenticated TOTP secrets."""

from __future__ import annotations

import asyncio
import os
from typing import Final

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.security.crypto import decode_key
from app.settings import Settings

_DUMMY_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=2$lEjv7SDA7Mf29OSWzayrsA$"
    "+3H5gqiXv77B0V8opvrgyDnEPdi4PWYKTp937s9gCZ4"
)
_TOTP_AAD = b"insurance-platform:staff:totp:v1"


class StaffPasswords:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

    @staticmethod
    def validate(password: str) -> None:
        if not (
            12 <= len(password) <= 128
            and any(c.isalpha() for c in password)
            and any(c.isdigit() for c in password)
        ):
            raise ValueError("Password must be 12-128 characters and include letters and digits")

    async def hash(self, password: str) -> str:
        self.validate(password)
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        # Unknown or invited staff still incur an Argon2 verification.
        try:
            return await asyncio.to_thread(
                self._hasher.verify, password_hash or _DUMMY_HASH, password
            )
        except (VerifyMismatchError, InvalidHashError):
            return False


class TotpProtector:
    def __init__(self, settings: Settings) -> None:
        self._cipher = AESGCM(decode_key(settings.app_encryption_key))

    def encrypt(self, secret: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, secret.encode("ascii"), _TOTP_AAD)

    def decrypt(self, data: bytes) -> str:
        if len(data) < 29:
            raise ValueError("Corrupt TOTP secret")
        return self._cipher.decrypt(data[:12], data[12:], _TOTP_AAD).decode("ascii")

    @staticmethod
    def new_secret() -> str:
        return pyotp.random_base32()

    @staticmethod
    def verify(secret: str, code: str) -> bool:
        return (
            len(code) == 6
            and code.isascii()
            and code.isdecimal()
            and bool(pyotp.TOTP(secret).verify(code, valid_window=1))
        )

    @staticmethod
    def uri(secret: str, email: str) -> str:
        return str(
            pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Insurance Platform")
        )
