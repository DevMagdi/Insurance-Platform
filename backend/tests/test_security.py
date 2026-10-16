"""PII invariants without accessing a live database."""

from __future__ import annotations

import base64
from datetime import date

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from app.security.crypto import NationalIdProtector, decode_key
from app.security.national_id import validate_national_id
from app.settings import Settings

# Fictional but syntactically valid; tests never depend on a real person's ID.
VALID_ID = "29901010112345"


def test_egyptian_national_id_validates_digits_birth_date_and_governorate() -> None:
    assert validate_national_id(VALID_ID) == VALID_ID
    assert validate_national_id("30002298800001") == "30002298800001"  # leap day, abroad
    for bad in (
        "2990101011234",  # 13 digits
        "2990101011234a",  # not ASCII digits
        "٢٩٩٠١٠١٠١١٢٣٤٥",  # Unicode decimal digits are NOT allowed
        "49901010112345",  # invalid century
        "29902300112345",  # invalid day
        "30002298800001 ",  # trailing space
        "29901019912345",  # unknown governorate
    ):
        with pytest.raises(ValueError):
            validate_national_id(bad)
    future_year = date.today().year + 1
    if future_year < 2100:
        with pytest.raises(ValueError):
            validate_national_id(f"3{future_year % 100:02d}01010112345")


def test_national_id_is_randomized_authenticated_ciphertext_and_stable_blind_index() -> None:
    protector = NationalIdProtector(b"a" * 32, b"b" * 32)
    first = protector.encrypt(VALID_ID)
    second = protector.encrypt(VALID_ID)
    assert first != second
    assert VALID_ID.encode() not in first
    assert protector.decrypt(first) == VALID_ID
    assert protector.blind_index(VALID_ID) == protector.blind_index(VALID_ID)
    assert len(protector.blind_index(VALID_ID)) == 32
    with pytest.raises(InvalidTag):
        protector.decrypt(bytes([first[0] ^ 1]) + first[1:])
    with pytest.raises(InvalidTag):
        NationalIdProtector(b"c" * 32, b"b" * 32).decrypt(first)


def test_keys_must_be_independent_and_valid_base64() -> None:
    with pytest.raises(ValueError, match="independent"):
        NationalIdProtector(b"a" * 32, b"a" * 32)
    with pytest.raises(ValueError):
        decode_key("placeholder")
    assert decode_key(base64.urlsafe_b64encode(b"b" * 32).decode()) == b"b" * 32


def test_runtime_settings_reject_placeholder_or_reused_secrets() -> None:
    good = {
        "DATABASE_URL": "postgresql+asyncpg://app_runtime@localhost/insurance",
        "APP_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"a" * 32).decode(),
        "APP_BLIND_INDEX_KEY": base64.urlsafe_b64encode(b"b" * 32).decode(),
        "JWT_SIGNING_KEY": "jwt-signing-secret-for-disposable-tests-only",
        # Production (the default environment) requires CAPTCHA on public forms.
        "TURNSTILE_SITE_KEY": "1x00000000000000000000AA",
        "TURNSTILE_SECRET_KEY": "1x0000000000000000000000000000000AA",
    }
    # No object-storage endpoint is configurable: binaries live in PostgreSQL.
    assert not hasattr(Settings(**good), "s3_bucket")
    with pytest.raises(ValidationError):
        Settings(**{**good, "APP_ENCRYPTION_KEY": "replace-with-generated-secret"})
    with pytest.raises(ValidationError):
        Settings(**{**good, "APP_BLIND_INDEX_KEY": good["APP_ENCRYPTION_KEY"]})
    with pytest.raises(ValidationError):
        Settings(**{**good, "JWT_SIGNING_KEY": "short"})
