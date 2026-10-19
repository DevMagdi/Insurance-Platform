"""Real account mail via the workplace connection, with explicit SMTP fallback.

Sandbox test recipients are NEVER used for invitations or password resets.
A configured provider failure is not silently rerouted to a development sink.
"""

from __future__ import annotations

import ipaddress
import secrets
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.staff_profiles import StaffProfileRepository
from app.security.integration_crypto import SecretCipher
from app.services.email_templates import branded_email, render_staff_invitation
from app.services.integration_providers import EmailSendRequest, build_provider
from app.services.integrations import config_aad
from app.services.providers import Delivery, ProviderUnavailable
from app.settings import Settings


def is_local_mail_host(value: str) -> bool:
    host = value.strip().lower().rstrip(".")
    if not host or host in {"localhost", "mailpit", "mailhog", "smtp", "testserver"}:
        return True
    if host.endswith((".local", ".localhost", ".test", ".invalid")) or any(
        marker in host for marker in ("mailtrap", "mailpit", "mailhog", "sandbox")
    ):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return "." not in host


def invitation_url(settings: Settings, token: str) -> str:
    parsed = urlsplit(settings.public_app_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderUnavailable("Set PUBLIC_APP_URL to the application's public HTTP(S) URL")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ProviderUnavailable("PUBLIC_APP_URL must be a bare application origin")
    if settings.is_production and (parsed.scheme != "https" or is_local_mail_host(parsed.hostname)):
        raise ProviderUnavailable("Production invitations require a public HTTPS application URL")
    # Fragments are not sent to proxies/access logs or as a Referer. The SPA also
    # accepts legacy ?token= links and immediately removes tokens from the URL.
    return f"{settings.public_app_url.rstrip('/')}/invite#token={token}"


class StaffMailService:
    def __init__(self, settings: Settings, fallback: Delivery) -> None:
        self.settings = settings
        self.fallback = fallback
        self.cipher = SecretCipher(settings.encryption_keyring())
        self.repository = StaffProfileRepository()

    def _validate_transport(self, config: dict[str, Any]) -> None:
        if not self.settings.is_production:
            return
        sender = str(config.get("from_email", ""))
        domain = sender.rpartition("@")[2]
        if not domain or is_local_mail_host(domain):
            raise ProviderUnavailable("Configure a verified real sender address")
        if config.get("host"):
            if is_local_mail_host(str(config["host"])):
                raise ProviderUnavailable(
                    "Development/sandbox SMTP cannot deliver production invites"
                )
            tls = str(config.get("start_tls", "true")).lower() in {"true", "1", "yes"}
            implicit = str(config.get("use_tls", "false")).lower() in {"true", "1", "yes"}
            if not tls and not implicit:
                raise ProviderUnavailable("Production account email requires TLS")

    async def send(self, db: AsyncSession, email: str, url: str, *, reset: bool = False) -> None:
        connection = await self.repository.email_connection(db)
        if connection is not None:
            if connection["connection_status"] == "failed":
                raise ProviderUnavailable("Reconnect the workplace email provider")
            provider = connection["provider"]
            config = self.cipher.decrypt_json(
                connection["config_ciphertext"], associated_data=config_aad(None, provider)
            )
            self._validate_transport(config)
            subject, body = render_staff_invitation(url, reset=reset)
            brand_loader = getattr(self.fallback, "brand_loader", None)
            brand = (await brand_loader()) if brand_loader is not None else "Insurance"
            brand = brand.strip()[:80] or "Insurance"
            text, html = branded_email(
                subject,
                body,
                brand=brand,
                public_url=self.settings.public_app_url,
                support_email=self.settings.support_email,
                locale="ar",
            )
            await build_provider(provider, config).send(
                EmailSendRequest(
                    recipients=(email,),
                    subject=f"{subject} · {brand}",
                    text_body=text,
                    html_body=html,
                    locale="ar",
                ),
                # This key is not derived from, and must never disclose, the bearer token.
                idempotency_key="staff-account-" + secrets.token_urlsafe(16),
            )
            return
        self._validate_transport(
            {
                "host": self.settings.smtp_host or "localhost",
                "from_email": self.settings.smtp_from,
                "start_tls": self.settings.smtp_start_tls,
                "use_tls": self.settings.smtp_use_tls,
            }
        )
        if reset:
            await self.fallback.send_password_reset(email, url)
        else:
            await self.fallback.send_invitation(email, url)
