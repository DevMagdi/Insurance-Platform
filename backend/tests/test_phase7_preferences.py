"""Notification preferences remain opt-out and are honored before delivery."""

from __future__ import annotations

import base64

import pytest

from app.services.providers import SMTPDelivery
from app.settings import Settings
from app.worker import enabled_notification_channels


def test_notification_preferences_are_opt_out_by_channel() -> None:
    assert enabled_notification_channels(None) == ("email", "whatsapp", "sms")
    assert enabled_notification_channels({"email": False, "sms": True}) == ("whatsapp", "sms")
    assert enabled_notification_channels({"email": False, "whatsapp": False, "sms": False}) == ()
    assert enabled_notification_channels('{"email": false}') == ("whatsapp", "sms")


@pytest.fixture
def delivery_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="development",
        SMTP_HOST="smtp.example.com",
        SMTP_FROM="notifications@example.com",
        SMTP_START_TLS=True,
    )


async def test_smtp_notice_respects_disabled_email_and_text_channels(
    delivery_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[object] = []

    async def fake_send(message: object, **_: object) -> None:
        sent.append(message)

    monkeypatch.setattr("app.services.providers.aiosmtplib.send", fake_send)
    provider = SMTPDelivery(delivery_settings)
    payload = {"ticket_no": "CMP-2026-000123", "status": "resolved"}
    assert not await provider.send_notice(
        "+201123456789",
        "customer@example.test",
        "status_changed",
        payload,
        {"email": False, "whatsapp": False, "sms": False},
    )
    assert sent == []
    assert not await provider.send_notice(
        "+201123456789",
        "customer@example.test",
        "status_changed",
        payload,
        {"email": False, "whatsapp": True, "sms": False},
    )
    assert sent == []
    assert await provider.send_notice(
        "+201123456789",
        "customer@example.test",
        "status_changed",
        payload,
        {"email": True, "whatsapp": False, "sms": False},
    )
    assert len(sent) == 1
    assert "customer@example.test" in str(sent[0])
