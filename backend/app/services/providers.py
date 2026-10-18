"""Replaceable delivery/scan providers; absent integrations fail closed."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol
from uuid import UUID

import aiosmtplib

from app.services.email_templates import render_customer_notice


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
        preferences: dict[str, Any] | None = None,
    ) -> bool: ...
    async def send_insurer(self, email: str, ticket: str, reason: str) -> bool: ...
    async def send_report(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_name: str,
        content_type: str,
        data: bytes,
    ) -> bool: ...


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
        preferences: dict[str, Any] | None = None,
    ) -> bool:
        if not self.settings.smtp_host:
            return False
        preferences = preferences or {}
        email_enabled = preferences.get("email", True) is not False
        text_enabled = any(
            preferences.get(channel, True) is not False for channel in ("sms", "whatsapp")
        )
        address = email if email_enabled else None
        if address is None and text_enabled and self.settings.dev_sms_mailbox:
            address = f"sms-{mobile[1:]}@sms.local.test"
        if address is None:
            return False
        subject, body = render_customer_notice(
            kind,
            str(payload["ticket_no"]),
            str(payload["status"]),
            str(payload.get("locale", "en")),
        )
        await self._mail(address, subject, body)
        return True

    async def send_report(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_name: str,
        content_type: str,
        data: bytes,
    ) -> bool:
        if not self.settings.smtp_host:
            return False
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment_name,
        )
        await aiosmtplib.send(
            message,
            hostname=self.settings.smtp_host,
            port=self.settings.smtp_port,
            username=self.settings.smtp_username,
            password=self.settings.smtp_password,
            start_tls=self.settings.smtp_start_tls,
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


@dataclass(frozen=True)
class ScanTarget:
    """One pending attachment, decrypted in the worker and never written to disk."""

    attachment_id: UUID
    original_filename: str
    mime_type: str
    size_bytes: int
    checksum_sha256: str
    content: bytes


class VirusScanner(Protocol):
    async def scan(self, target: ScanTarget) -> str | None: ...


class UnconfiguredVirusScanner:
    """Fail-closed placeholder: returns ``None`` so uploads stay untrusted."""

    async def scan(self, target: ScanTarget) -> str | None:
        # Never treat unscanned content as clean.
        return None


class ClamAVScanner:
    """ClamAV clamd INSTREAM scanner; only an explicit ``OK`` verdict is clean.

    ``scan`` returns ``"clean"``, ``"infected"`` or ``None``. ``None`` means the
    scanner could not give a verdict (daemon down, timeout, protocol error) and
    the attachment must stay untrusted; it is never a pass.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _endpoint(self) -> tuple[str, int, float]:
        host = str(self.config.get("host", "")).strip()
        if not host or not re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", host):
            raise ProviderUnavailable("ClamAV host is not configured")
        port = int(self.config.get("port") or 3310)
        if not 1 <= port <= 65535:
            raise ProviderUnavailable("ClamAV port is invalid")
        timeout = float(self.config.get("timeout_seconds") or 30)
        return host, port, min(max(timeout, 1.0), 300.0)

    @staticmethod
    async def _command(writer: asyncio.StreamWriter, chunk: bytes) -> None:
        writer.write(len(chunk).to_bytes(4, "big") + chunk)
        await writer.drain()

    async def ping(self) -> str:
        """Connection probe used by the admin connection test (no scan)."""
        host, port, timeout = self._endpoint()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            try:
                writer.write(b"zPING\0")
                await writer.drain()
                reply = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=timeout)
            finally:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await writer.wait_closed()
        except (
            TimeoutError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            raise ProviderUnavailable("ClamAV daemon is unreachable") from exc
        if not reply.startswith(b"PONG"):
            raise ProviderUnavailable("ClamAV daemon rejected the probe")
        return reply.decode("ascii", "replace").rstrip("\0")

    async def scan(self, target: ScanTarget) -> str | None:
        if not target.content:
            return None
        try:
            host, port, timeout = self._endpoint()
        except ProviderUnavailable:
            return None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            try:
                writer.write(b"zINSTREAM\0")
                await writer.drain()
                for start in range(0, len(target.content), 64 * 1024):
                    await self._command(writer, target.content[start : start + 64 * 1024])
                await self._command(writer, b"")
                reply = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=timeout)
            finally:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await writer.wait_closed()
        except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None
        verdict = reply.decode("ascii", "replace").rstrip("\0").strip()
        if verdict.endswith("OK"):
            return "clean"
        if "FOUND" in verdict:
            return "infected"
        return None
