"""Argon2id staff passwords and separately authenticated TOTP secrets."""

from __future__ import annotations

import asyncio
from typing import Final

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.settings import Settings

_DUMMY_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=2$lEjv7SDA7Mf29OSWzayrsA$"
    "+3H5gqiXv77B0V8opvrgyDnEPdi4PWYKTp937s9gCZ4"
)
_TOTP_AAD = b"insurance-platform:staff:totp:v1"

# Commonly leaked/default credentials (top-password lists, keyboard walks,
# seasonal choices, and common organization defaults). Normalize case, spacing,
# punctuation and basic leetspeak before comparison; reject numeric suffixes too.
_COMMON_PASSWORDS: Final[frozenset[str]] = frozenset(
    """
    123456 123456789 12345678 12345 1234567 1234567890 111111 000000 123123 112233
    password password1 password12 password123 password1234 password12345 passw0rd p@ssword
    qwerty qwerty123 qwertyuiop qwerty1 1q2w3e4r 1qaz2wsx  qazwsx asdfghjkl zxcvbnm
    abc123 abc12345 abcd1234 letmein letmein1 welcome welcome1 welcome123 admin admin123
    admin1234 administrator root root123 toor master master123 login login123 user user123
    guest guest123 test test123 test1234 changeme changeme1 secret secret123 superman batman
    dragon monkey football baseball soccer basketball sunshine princess iloveyou trustno1
    whatever freedom hello hello123 shadow michael mustang access flower hunter hunter2
    computer internet service server support default password! pass123 pass1234 pass12345
    123qwe 1234qwer 1q2w3e 1q2w3e4r5t 654321 987654321 9876543210 121212 222222 777777
    888888 999999 159753 147258369 741852963 1q2w3e4r! qwerty! qwerty123! admin! iloveyou!
    letmein! welcome! password2024 password2025 password2026 welcome2024 welcome2025 welcome2026
    admin2024 admin2025 admin2026 summer summer2024 summer2025 summer2026 winter spring autumn
    football1 baseball1 sunshine1 princess1 dragon1 monkey1 shadow1 master1 secret1 trustno1!
    P@ssw0rd Passw0rd Pa$$w0rd P@ssword1 Qwerty123! Admin@123 Welcome@123 ChangeMe123
    """.split()  # noqa: SIM905 - compact readable curated denylist
)
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)


def _is_common(password: str) -> bool:
    normalized = "".join(char for char in password.casefold().translate(_LEET) if char.isalnum())
    if not normalized:
        return False
    if normalized in _COMMON_PASSWORDS:
        return True
    # Passwords like Password!2026 and welcome123 are routine variants.
    return any(
        normalized.startswith(common)
        and any(char.isdigit() for char in normalized[len(common) :])
        and 1 <= len(normalized) - len(common) <= 4
        for common in _COMMON_PASSWORDS
    )


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
        if _is_common(password):
            raise ValueError("Choose a password that is not commonly used")

    def hash_sync(self, password: str) -> str:
        """Blocking variant for one-shot management commands (seed/bootstrap)."""
        self.validate(password)
        return self._hasher.hash(password)

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
        self.keyring = settings.encryption_keyring()

    def encrypt(self, secret: str) -> bytes:
        return self.keyring.encrypt(secret.encode("ascii"), _TOTP_AAD)

    def decrypt(self, data: bytes) -> str:
        if len(data) < 29:
            raise ValueError("Corrupt TOTP secret")
        return self.keyring.decrypt(data, _TOTP_AAD).decode("ascii")

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
