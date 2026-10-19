"""Replaceable delivery/scan providers; absent integrations fail closed."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol
from uuid import UUID

import aiosmtplib

from app.services.brand_assets import BrandLogo, attach_inline_logo, coerce_logo
from app.services.email_templates import (
    branded_email,
    render_customer_notice,
    render_insurer_forward,
    render_staff_invitation,
)


class DeliveryConfig(Protocol):
    smtp_host: str | None
    smtp_port: int
    smtp_from: str
    smtp_username: str | None
    smtp_password: str | None
    smtp_start_tls: bool
    smtp_use_tls: bool


class ProviderUnavailable(Exception):
    pass


class Delivery(Protocol):
    async def send_otp(self, email: str, code: str) -> None: ...
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
    """Deliver authentication and transactional messages through configured SMTP.

    There is deliberately no sandbox/local-mailbox fallback: absent SMTP is an
    explicit delivery failure and OTPs are never written to logs or test sinks.
    """

    def __init__(
        self,
        settings: DeliveryConfig,
        brand_loader: Callable[[], Awaitable[str]] | None = None,
        logo_loader: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.brand_loader = brand_loader
        self.logo_loader = logo_loader

    async def _brand(self) -> str:
        brand = (await self.brand_loader()) if self.brand_loader is not None else "Insurance"
        return brand.strip()[:80] or "Insurance"

    async def _logo(self) -> BrandLogo | None:
        if self.logo_loader is None:
            return None
        try:
            return coerce_logo(await self.logo_loader())
        except Exception:  # A logo lookup must never block delivery.
            return None

    async def _mail(self, address: str, subject: str, body: str) -> None:
        host = (self.settings.smtp_host or "").strip().lower().rstrip(".")
        sender_domain = self.settings.smtp_from.rpartition("@")[2].lower()
        local_or_sandbox = (
            not host
            or host in {"localhost", "mailpit", "mailhog", "smtp", "testserver"}
            or host.endswith((".local", ".localhost", ".test", ".invalid"))
            or any(marker in host for marker in ("mailtrap", "mailpit", "mailhog", "sandbox"))
            or sender_domain.endswith((".local", ".localhost", ".test", ".invalid"))
        )
        if local_or_sandbox:
            raise ProviderUnavailable("A real SMTP provider and sender address are required")
        if not (self.settings.smtp_start_tls or self.settings.smtp_use_tls):
            raise ProviderUnavailable("SMTP transport must use TLS")
        brand = await self._brand()
        logo = await self._logo()
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = address
        message["Subject"] = f"{subject} · {brand}"
        text, html = branded_email(
            subject,
            body,
            brand=brand,
            public_url=getattr(self.settings, "public_app_url", ""),
            support_email=getattr(self.settings, "support_email", "support@example.invalid"),
            has_logo=logo is not None,
        )
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        attach_inline_logo(message, logo)
        await aiosmtplib.send(
            message,
            hostname=self.settings.smtp_host,
            port=self.settings.smtp_port,
            username=self.settings.smtp_username or None,
            password=self.settings.smtp_password or None,
            start_tls=self.settings.smtp_start_tls,
            use_tls=self.settings.smtp_use_tls,
            timeout=20,
        )

    async def send_otp(self, email: str, code: str) -> None:
        await self._mail(
            email,
            "Sign-in code",
            f"Your one-time sign-in code is {code}. It expires in 5 minutes. "
            "If you did not request this, ignore this message.",
        )

    async def send_invitation(self, email: str, url: str) -> None:
        subject, body = render_staff_invitation(url)
        await self._mail(email, subject, body)

    async def send_password_reset(self, email: str, url: str) -> None:
        subject, body = render_staff_invitation(url, reset=True)
        await self._mail(email, subject, body)

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
        address = email if email_enabled else None
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
        brand = await self._brand()
        logo = await self._logo()
        text, html = branded_email(
            subject,
            body,
            brand=brand,
            public_url=getattr(self.settings, "public_app_url", ""),
            support_email=getattr(self.settings, "support_email", "support@example.invalid"),
            has_logo=logo is not None,
        )
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        attach_inline_logo(message, logo)
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
            username=self.settings.smtp_username or None,
            password=self.settings.smtp_password or None,
            start_tls=self.settings.smtp_start_tls,
            use_tls=self.settings.smtp_use_tls,
            timeout=20,
        )
        return True

    async def send_insurer(self, email: str, ticket: str, reason: str) -> bool:
        if not self.settings.smtp_host:
            return False
        from app.services.integrations import minimize_pii

        # Legacy escalation notices carry no policy/customer details; never invent them.
        subject, body = render_insurer_forward(
            ticket_no=ticket,
            subject="Escalation for review",
            policy_number="Not provided in this escalation notice",
            insurance_type="Not provided",
            details=minimize_pii(reason),
            locale="en",
        )
        await self._mail(email, subject, body)
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
