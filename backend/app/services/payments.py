"""Fawry / Paymob payment-gateway signatures (Phase 2 integrations).

Fail-closed like every other provider in this codebase: without a configured
secret nothing verifies, and the API answers 503 rather than pretending a
payment happened. Only the cryptographic checks live here; HTTP calls to the
gateways are made by the deployment that owns the merchant credentials.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal


def paymob_hmac(secret: str, fields: dict[str, str]) -> str:
    """Paymob transaction-callback HMAC-SHA512 over its documented field order."""
    order = (
        "amount_cents",
        "created_at",
        "currency",
        "error_occured",
        "has_parent_transaction",
        "id",
        "integration_id",
        "is_3d_secure",
        "is_auth",
        "is_capture",
        "is_refunded",
        "is_standalone_payment",
        "is_voided",
        "order",
        "owner",
        "pending",
        "source_data_pan",
        "source_data_sub_type",
        "source_data_type",
        "success",
    )
    concatenated = "".join(fields.get(name, "") for name in order)
    return hmac.new(secret.encode(), concatenated.encode(), hashlib.sha512).hexdigest()


def verify_paymob(secret: str | None, fields: dict[str, str], received: str | None) -> bool:
    if not secret or not received:
        return False
    return hmac.compare_digest(paymob_hmac(secret, fields), received.lower())


def fawry_signature(merchant_code: str, merchant_ref: str, amount: Decimal, secret: str) -> str:
    """FawryPay charge signature: SHA-256 over merchant|reference|amount|secret."""
    payload = f"{merchant_code}{merchant_ref}{amount:.2f}{secret}"
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_fawry(
    secret: str | None,
    merchant_code: str,
    merchant_ref: str,
    amount: Decimal,
    received: str | None,
) -> bool:
    if not secret or not merchant_code or not received:
        return False
    expected = fawry_signature(merchant_code, merchant_ref, amount, secret)
    return hmac.compare_digest(expected, received.lower())
