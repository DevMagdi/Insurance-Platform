"""Replaceable delivery/scan providers; absent integrations fail closed."""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any, Protocol

import aiosmtplib


class DeliveryConfig(Protocol):
    smtp_host: str | None
    smtp_port: int
    smtp_from: str
    smtp_username: str | None
    smtp_password: str | None
    smtp_start_tls: bool
    dev_sms_mailbox: bool


class ProviderUnavailable(Exception):
    pass


class Delivery(Protocol):
    async def send_otp(self, mobile: str, code: str) -> None: ...
    async def send_invitation(self, email: str, url: str) -> None: ...
    async def send_password_reset(self, email: str, url: str) -> None: ...
    async def send_notice(
        self,
        mobile: str,
        email: str | None,
        kind: str,
        payload: dict[str, Any],
    ) -> bool: ...
    async def send_insurer(self, email: str, ticket: str, reason: str) -> bool: ...


class SMTPDelivery:
    """Local Mailpit sink for SMS in dev; SMTP email for invitations/notices.

    Production OTP cannot silently fall back to email or logs. Supply a real
    SMS implementation in production. Unconfigured notices stay pending.
    """

    def __init__(self, settings: DeliveryConfig) -> None:
        self.settings = settings

    async def _mail(self, address: str, subject: str, body: str) -> None:
        if not self.settings.smtp_host:
            raise ProviderUnavailable("SMTP is not configured")
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = address
        message["Subject"] = subject
        message.set_content(body)
        await aiosmtplib.send(
            message,
            hostname=self.settings.smtp_host,
            port=self.settings.smtp_port,
            username=self.settings.smtp_username,
            password=self.settings.smtp_password,
            start_tls=self.settings.smtp_start_tls,
        )

    async def send_otp(self, mobile: str, code: str) -> None:
        if not self.settings.dev_sms_mailbox:
            raise ProviderUnavailable("An SMS provider is required")
        # Only in local development; never log the OTP and never send to a real number.
        await self._mail(
            f"sms-{mobile[1:]}@sms.local.test",
            "Insurance sign-in code",
            f"Your one-time sign-in code is {code}. It expires in 5 minutes.",
        )

    async def send_invitation(self, email: str, url: str) -> None:
        await self._mail(
            email,
            "Set your Insurance Platform password",
            f"Your one-time invitation expires in 48 hours. Open: {url}",
        )

    async def send_password_reset(self, email: str, url: str) -> None:
        await self._mail(
            email,
            "Reset your Insurance Platform password",
            f"Your one-time password reset expires in 48 hours. Open: {url}",
        )

    async def send_notice(
        self,
        mobile: str,
        email: str | None,
        kind: str,
        payload: dict[str, Any],
    ) -> bool:
        if not self.settings.smtp_host:
            return False
        address = email
        if address is None and self.settings.dev_sms_mailbox:
            address = f"sms-{mobile[1:]}@sms.local.test"
        if address is None:
            return False
        await self._mail(
            address,
            f"Complaint {kind.replace('_', ' ')}",
            f"Ticket {payload['ticket_no']}: {payload['status']}.",
        )
        return True

    async def send_insurer(self, email: str, ticket: str, reason: str) -> bool:
        if not self.settings.smtp_host:
            return False
        # Do not send national IDs, attachments or unreviewed complaint bodies.
        await self._mail(
            email,
            f"Insurance complaint {ticket}",
            f"Ticket {ticket} was escalated. Reason: {reason}",
        )
        return True


class VirusScanner(Protocol):
    async def scan(self, storage_key: str) -> str | None: ...


class UnconfiguredVirusScanner:
    async def scan(self, storage_key: str) -> str | None:
        # Never treat unscanned content as clean.
        return None
