"""Replaceable adapters for outbound email, WhatsApp, LLM and voice providers.

Only the mock LLM/voice adapters are suitable for local exercises. Production
implementations are intentionally fail-closed when credentials or providers are absent.
"""

from __future__ import annotations

import asyncio
import base64
import imaplib
import json
import mimetypes
import re
import ssl
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import aiosmtplib
import httpx
import jwt

from app.security.ssrf import UnsafeDestination, pin_public_url
from app.services.brand_assets import LOGO_CID, BrandLogo, attach_inline_logo
from app.services.email_templates import branded_email


class IntegrationProviderError(Exception):
    """Sanitized provider failure safe to persist as an error code."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        if not re.fullmatch(r"[a-z0-9_:-]{1,80}", code):
            raise ValueError("Provider error code must be safe for logs")
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderResult:
    provider_id: str | None = None


def _config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OutboundAttachment:
    filename: str
    mime_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class EmailSendRequest:
    recipients: tuple[str, ...]
    subject: str
    text_body: str
    locale: str = "en"
    attachments: tuple[OutboundAttachment, ...] = ()
    html_body: str | None = None
    # Inline CID brand logo; MIME/API adapters embed it so the logo renders
    # in clients that block remote images.
    logo: BrandLogo | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Even connection-test / direct-adapter messages get both branded alternatives.
        # Normal transactional paths supply the platform's current database brand.
        if self.html_body is None:
            import os

            text, html = branded_email(
                self.subject,
                self.text_body,
                locale=self.locale,
                public_url=os.getenv("PUBLIC_APP_URL", ""),
                support_email=os.getenv("SUPPORT_EMAIL", "support@example.invalid"),
                has_logo=self.logo is not None,
            )
            object.__setattr__(self, "text_body", text)
            object.__setattr__(self, "html_body", html)


@dataclass(frozen=True)
class IncomingEmail:
    sequence_id: str
    external_message_id: str
    sender: str
    subject: str
    body: str


@dataclass(frozen=True)
class WhatsAppSendRequest:
    recipient: str
    body: str
    template_name: str | None = None
    language: str = "en"
    parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class SMSProviderRequest:
    recipient: str
    body: str


@dataclass(frozen=True)
class AIInsightDraft:
    kind: str
    payload: dict[str, Any]
    confidence: float = 0.75


@dataclass(frozen=True)
class AIAnalysisResult:
    insights: tuple[AIInsightDraft, ...]
    model: str


@dataclass(frozen=True)
class VoiceCallRequest:
    mobile: str
    ticket_no: str
    script: str
    language: str
    consent_confirmed: bool
    scheduled_at: datetime | None = None


@dataclass(frozen=True)
class VoiceCallResult:
    provider_call_id: str | None
    transcript: str | None
    outcome: str
    recording: bytes | None = field(default=None, repr=False)


class EmailProvider(Protocol):
    async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult: ...


class InboundEmailProvider(Protocol):
    async def fetch_unseen(self, *, limit: int = 20) -> list[IncomingEmail]: ...
    async def mark_seen(self, sequence_id: str) -> None: ...


class WhatsAppProvider(Protocol):
    async def send(
        self, request: WhatsAppSendRequest, *, idempotency_key: str
    ) -> ProviderResult: ...


class LLMProvider(Protocol):
    async def analyze(self, text: str, *, locale: str) -> AIAnalysisResult: ...


class VoiceProvider(Protocol):
    async def follow_up(
        self, request: VoiceCallRequest, *, idempotency_key: str
    ) -> VoiceCallResult: ...

    async def answer_inbound(self, event: dict[str, Any]) -> VoiceCallResult: ...


class SpeechToTextProvider(Protocol):
    async def transcribe(self, audio: bytes, *, language: str) -> str: ...


class TextToSpeechProvider(Protocol):
    async def synthesize(self, text: str, *, language: str) -> bytes: ...


class VoiceDialogueProvider(Protocol):
    async def respond(self, prompt: str, *, language: str) -> str: ...


class SMTPEmailProvider:
    """SMTP transport used directly or with SendGrid/SES SMTP relay credentials."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult:
        host = str(self.config.get("host", ""))
        from_email = str(self.config.get("from_email", ""))
        if not host or not from_email:
            raise IntegrationProviderError("email_configuration_missing", retryable=False)
        message = EmailMessage()
        message["From"] = from_email
        message["To"] = ", ".join(request.recipients)
        message["Subject"] = request.subject
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = make_msgid(idstring=idempotency_key[:24])
        message["X-Idempotency-Key"] = idempotency_key[:128]
        message.set_content(request.text_body)
        message.add_alternative(
            request.html_body
            or branded_email(
                request.subject,
                request.text_body,
                locale=request.locale,
                has_logo=request.logo is not None,
            )[1],
            subtype="html",
        )
        attach_inline_logo(message, request.logo)
        for item in request.attachments:
            maintype, _, subtype = item.mime_type.partition("/")
            if not maintype or not subtype:
                raise IntegrationProviderError("attachment_mime_invalid", retryable=False)
            message.add_attachment(
                item.content,
                maintype=maintype,
                subtype=subtype,
                filename=item.filename,
            )
        try:
            await aiosmtplib.send(
                message,
                hostname=host,
                port=int(self.config.get("port", 587)),
                username=self.config.get("username"),
                password=self.config.get("password"),
                start_tls=_config_bool(
                    self.config.get("start_tls"),
                    not _config_bool(self.config.get("use_tls"), False),
                ),
                use_tls=_config_bool(self.config.get("use_tls"), False),
                timeout=20,
            )
        except (OSError, aiosmtplib.SMTPException, TimeoutError) as exc:
            raise IntegrationProviderError("smtp_delivery_failed") from exc
        return ProviderResult(provider_id=message["Message-ID"])

    async def test_connection(self) -> None:
        host = str(self.config.get("host", ""))
        from_email = str(self.config.get("from_email", ""))
        if not host or not from_email:
            raise IntegrationProviderError("email_configuration_missing", retryable=False)
        try:
            client = aiosmtplib.SMTP(
                hostname=host,
                port=int(self.config.get("port", 587)),
                timeout=20,
                start_tls=_config_bool(
                    self.config.get("start_tls"),
                    not _config_bool(self.config.get("use_tls"), False),
                ),
                use_tls=_config_bool(self.config.get("use_tls"), False),
            )
            await client.connect()
            if self.config.get("username"):
                await client.login(
                    str(self.config["username"]), str(self.config.get("password") or "")
                )
            await client.noop()
            await client.quit()
        except (OSError, aiosmtplib.SMTPException, TimeoutError) as exc:
            raise IntegrationProviderError("smtp_connection_failed") from exc


class IMAPMailProvider:
    """IMAP polling adapter; messages are acknowledged only after durable queueing."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _connect(self) -> imaplib.IMAP4:
        host = str(self.config.get("host", ""))
        if not host:
            raise IntegrationProviderError("imap_configuration_missing", retryable=False)
        use_ssl = _config_bool(self.config.get("use_ssl"), True)
        port = int(self.config.get("port", 993 if use_ssl else 143))
        try:
            if use_ssl:
                client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    host, port, ssl_context=ssl.create_default_context(), timeout=20
                )
            else:
                client = imaplib.IMAP4(host, port, timeout=20)
                if _config_bool(self.config.get("start_tls"), True):
                    client.starttls(ssl_context=ssl.create_default_context())
            client.login(str(self.config.get("username", "")), str(self.config.get("password", "")))
            return client
        except (OSError, imaplib.IMAP4.error, ssl.SSLError) as exc:
            raise IntegrationProviderError("imap_connection_failed") from exc

    @staticmethod
    def _text_body(message: Any) -> str:
        chunks: list[str] = []
        for part in message.walk() if message.is_multipart() else (message,):
            if (
                part.get_content_type() != "text/plain"
                or part.get_content_disposition() == "attachment"
            ):
                continue
            try:
                content = part.get_content()
            except (LookupError, UnicodeDecodeError, AttributeError):
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
            if isinstance(content, str):
                chunks.append(content)
            if sum(len(chunk) for chunk in chunks) >= 5000:
                break
        return "\n".join(chunks).strip()[:5000]

    async def fetch_unseen(self, *, limit: int = 20) -> list[IncomingEmail]:
        return await asyncio.to_thread(self._fetch_unseen, max(1, min(limit, 100)))

    def _fetch_unseen(self, limit: int) -> list[IncomingEmail]:
        client = self._connect()
        mailbox = str(self.config.get("mailbox", "INBOX"))
        try:
            status, _ = client.select(mailbox, readonly=False)
            if status != "OK":
                raise IntegrationProviderError("imap_mailbox_unavailable")
            status, data = client.search(None, "UNSEEN")
            if status != "OK" or not data:
                raise IntegrationProviderError("imap_search_failed")
            messages: list[IncomingEmail] = []
            for sequence in data[0].split()[-limit:]:
                status, fetched = client.fetch(sequence, "(RFC822)")
                if status != "OK":
                    continue
                raw = next(
                    (
                        item[1]
                        for item in fetched
                        if isinstance(item, tuple) and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if raw is None:
                    continue
                message = message_from_bytes(raw, policy=email_policy)
                external_id = str(message.get("Message-ID", "")).strip().strip("<>")
                if not external_id:
                    import hashlib

                    external_id = "sha256:" + hashlib.sha256(raw).hexdigest()
                sender = parseaddr(str(message.get("From", "")))[1][:254]
                messages.append(
                    IncomingEmail(
                        sequence.decode("ascii"),
                        external_id[:256],
                        sender,
                        str(message.get("Subject", ""))[:500],
                        self._text_body(message),
                    )
                )
            return messages
        except (OSError, imaplib.IMAP4.error) as exc:
            raise IntegrationProviderError("imap_fetch_failed") from exc
        finally:
            with suppress(OSError, imaplib.IMAP4.error):
                client.logout()

    async def mark_seen(self, sequence_id: str) -> None:
        if not re.fullmatch(r"[0-9]{1,20}", sequence_id):
            raise IntegrationProviderError("imap_sequence_invalid", retryable=False)
        await asyncio.to_thread(self._mark_seen, sequence_id)

    def _mark_seen(self, sequence_id: str) -> None:
        client = self._connect()
        try:
            client.select(str(self.config.get("mailbox", "INBOX")), readonly=False)
            status, _ = client.store(sequence_id, "+FLAGS", "\\Seen")
            if status != "OK":
                raise IntegrationProviderError("imap_ack_failed")
        except (OSError, imaplib.IMAP4.error) as exc:
            raise IntegrationProviderError("imap_ack_failed") from exc
        finally:
            with suppress(OSError, imaplib.IMAP4.error):
                client.logout()

    async def test_connection(self) -> None:
        def _probe() -> None:
            client = self._connect()
            try:
                status, _ = client.select(str(self.config.get("mailbox", "INBOX")), readonly=True)
                if status != "OK":
                    raise IntegrationProviderError("imap_mailbox_unavailable")
            finally:
                with suppress(OSError, imaplib.IMAP4.error):
                    client.logout()

        try:
            await asyncio.to_thread(_probe)
        except IntegrationProviderError:
            raise
        except (OSError, imaplib.IMAP4.error) as exc:
            raise IntegrationProviderError("imap_connection_failed") from exc


class SendGridEmailProvider:
    """SendGrid REST adapter; API credentials are supplied from encrypted settings."""

    endpoint = "https://api.sendgrid.com/v3/mail/send"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult:
        api_key = str(self.config.get("api_key", ""))
        sender = str(self.config.get("from_email", ""))
        if not api_key or not sender:
            raise IntegrationProviderError("sendgrid_configuration_missing", retryable=False)
        attachments = [
            {
                "content": base64.b64encode(a.content).decode("ascii"),
                "type": a.mime_type,
                "filename": a.filename,
                "disposition": "attachment",
            }
            for a in request.attachments
        ]
        body: dict[str, Any] = {
            "from": {"email": sender},
            "personalizations": [{"to": [{"email": email} for email in request.recipients]}],
            "subject": request.subject,
            "content": [
                {"type": "text/plain", "value": request.text_body},
                {
                    "type": "text/html",
                    "value": request.html_body
                    or branded_email(request.subject, request.text_body, locale=request.locale)[1],
                },
            ],
            "custom_args": {"idempotency_key": idempotency_key[:128]},
        }
        if request.logo is not None:
            attachments.append(
                {
                    "content": base64.b64encode(request.logo.content).decode("ascii"),
                    "type": request.logo.mime_type,
                    "filename": request.logo.filename,
                    "disposition": "inline",
                    "content_id": LOGO_CID,
                }
            )
        if attachments:
            body["attachments"] = attachments
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("sendgrid_unreachable") from exc
        if response.status_code == 202:
            return ProviderResult(provider_id=response.headers.get("X-Message-Id"))
        raise IntegrationProviderError(
            "sendgrid_rejected",
            retryable=response.status_code >= 500 or response.status_code == 429,
        )

    async def test_connection(self) -> None:
        api_key = str(self.config.get("api_key", ""))
        sender = str(self.config.get("from_email", ""))
        if not api_key or not sender:
            raise IntegrationProviderError("sendgrid_configuration_missing", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    "https://api.sendgrid.com/v3/scopes",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("sendgrid_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("sendgrid_auth_failed", retryable=False)


class SESEmailProvider:
    """Amazon SES through its SMTP interface; no AWS SDK is required.

    ``region`` selects the regional SES SMTP endpoint. ``smtp_username`` /
    ``smtp_password`` must be the SES SMTP credentials; ``access_key_id`` /
    ``secret_access_key`` are accepted as the same pair for existing
    configurations. Messages keep the same bounded, attachment-aware shape as the
    SMTP provider.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _credentials(self) -> tuple[str | None, str | None]:
        username = self.config.get("smtp_username") or self.config.get("access_key_id")
        password = self.config.get("smtp_password") or self.config.get("secret_access_key")
        return (
            str(username) if username else None,
            str(password) if password else None,
        )

    async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult:
        sender = str(self.config.get("from_email", ""))
        region = str(self.config.get("region", ""))
        if not sender or not region:
            raise IntegrationProviderError("ses_configuration_missing", retryable=False)
        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(request.recipients)
        message["Subject"] = request.subject
        message["X-Idempotency-Key"] = idempotency_key[:128]
        message.set_content(request.text_body)
        message.add_alternative(
            request.html_body
            or branded_email(
                request.subject,
                request.text_body,
                locale=request.locale,
                has_logo=request.logo is not None,
            )[1],
            subtype="html",
        )
        attach_inline_logo(message, request.logo)
        for item in request.attachments:
            maintype, _, subtype = item.mime_type.partition("/")
            message.add_attachment(
                item.content,
                maintype=maintype,
                subtype=subtype,
                filename=item.filename,
            )
        username, password = self._credentials()
        try:
            await aiosmtplib.send(
                message,
                hostname=str(self.config.get("host") or f"email-smtp.{region}.amazonaws.com"),
                port=int(self.config.get("port") or 587),
                username=username,
                password=password,
                start_tls=True,
            )
        except Exception as exc:
            raise IntegrationProviderError("ses_delivery_failed") from exc
        # SES' SMTP interface does not return a message id; the queue keeps its own.
        return ProviderResult(provider_id=None)

    async def test_connection(self) -> None:
        sender = str(self.config.get("from_email", ""))
        region = str(self.config.get("region", ""))
        if not sender or not region:
            raise IntegrationProviderError("ses_configuration_missing", retryable=False)
        username, password = self._credentials()
        try:
            client = aiosmtplib.SMTP(
                hostname=str(self.config.get("host") or f"email-smtp.{region}.amazonaws.com"),
                port=int(self.config.get("port") or 587),
                timeout=20,
                start_tls=True,
            )
            await client.connect()
            if username:
                await client.login(username, password or "")
            await client.noop()
            await client.quit()
        except (OSError, aiosmtplib.SMTPException, TimeoutError) as exc:
            raise IntegrationProviderError("ses_connection_failed") from exc


class WhatsAppCloudProvider:
    """Meta WhatsApp Business Cloud API adapter (template or 24-hour text message)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _messages_url(self, phone_id: str, version: str) -> str:
        return f"https://graph.facebook.com/{version}/{phone_id}/messages"

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def send(self, request: WhatsAppSendRequest, *, idempotency_key: str) -> ProviderResult:
        token = str(self.config.get("access_token", ""))
        phone_id = str(self.config.get("phone_number_id", ""))
        version = str(self.config.get("api_version", "v21.0"))
        if not token or not phone_id or not re.fullmatch(r"v\d+\.\d+", version):
            raise IntegrationProviderError("whatsapp_configuration_missing", retryable=False)
        if request.template_name:
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "to": request.recipient,
                "type": "template",
                "template": {
                    "name": request.template_name,
                    "language": {"code": request.language},
                    "components": (
                        [
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": value} for value in request.parameters
                                ],
                            }
                        ]
                        if request.parameters
                        else []
                    ),
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": request.recipient,
                "type": "text",
                "text": {"preview_url": False, "body": request.body},
            }
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    self._messages_url(phone_id, version),
                    headers=self._auth_headers(token),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("whatsapp_unreachable") from exc
        if response.status_code not in (200, 201):
            raise IntegrationProviderError(
                "whatsapp_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            value = response.json()
            provider_id = value["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            provider_id = None
        return ProviderResult(provider_id=str(provider_id) if provider_id else None)

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> tuple[bytes, str, str]:
        """Download an inbound attachment: the bulk of real WhatsApp traffic.

        Meta returns a short-lived, host-pinned URL from the media id, so this
        is a two-step call. The download is capped and streamed so a hostile
        or mis-sized media item cannot exhaust worker memory.

        Returns ``(content, mime_type, filename)``.
        """
        token = str(self.config.get("access_token", ""))
        version = str(self.config.get("api_version", "v21.0"))
        if not token or not re.fullmatch(r"v\d+\.\d+", version):
            raise IntegrationProviderError("whatsapp_configuration_missing", retryable=False)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", media_id):
            raise IntegrationProviderError("whatsapp_media_invalid", retryable=False)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                lookup = await client.get(
                    f"https://graph.facebook.com/{version}/{media_id}", headers=headers
                )
                if lookup.status_code != 200:
                    raise IntegrationProviderError(
                        "whatsapp_media_unavailable",
                        retryable=lookup.status_code >= 500 or lookup.status_code == 429,
                    )
                document = lookup.json()
                url = str(document.get("url") or "")
                mime_type = str(document.get("mime_type") or "application/octet-stream")
                declared = int(document.get("file_size") or 0)
                # Meta hands back a short-lived URL on one of its own hosts.
                # Pin to those: the operator never supplies this URL, so an
                # unexpected host means the response cannot be trusted.
                host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
                trusted = host.endswith((".fbcdn.net", ".facebook.com")) or host in {
                    "graph.facebook.com",
                    "lookaside.fbsbx.com",
                }
                if not url.startswith("https://") or not trusted:
                    raise IntegrationProviderError("whatsapp_media_untrusted_host", retryable=False)
                if declared and declared > max_bytes:
                    raise IntegrationProviderError("whatsapp_media_too_large", retryable=False)
                buffer = bytearray()
                async with client.stream("GET", url, headers=headers) as stream:
                    if stream.status_code != 200:
                        raise IntegrationProviderError(
                            "whatsapp_media_unavailable",
                            retryable=stream.status_code >= 500,
                        )
                    async for chunk in stream.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > max_bytes:
                            raise IntegrationProviderError(
                                "whatsapp_media_too_large", retryable=False
                            )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("whatsapp_unreachable") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise IntegrationProviderError("whatsapp_media_invalid", retryable=False) from exc
        if not buffer:
            raise IntegrationProviderError("whatsapp_media_empty", retryable=False)
        suffix = mimetypes.guess_extension(mime_type.split(";")[0].strip()) or ".bin"
        return bytes(buffer), mime_type, f"whatsapp-{media_id[:24]}{suffix}"

    async def test_connection(self) -> None:
        token = str(self.config.get("access_token", ""))
        phone_id = str(self.config.get("phone_number_id", ""))
        version = str(self.config.get("api_version", "v21.0"))
        if not token or not phone_id or not re.fullmatch(r"v\d+\.\d+", version):
            raise IntegrationProviderError("whatsapp_configuration_missing", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    f"https://graph.facebook.com/{version}/{phone_id}",
                    params={"fields": "id,display_phone_number,verified_name"},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("whatsapp_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("whatsapp_auth_failed", retryable=False)


class WhatsApp360DialogProvider(WhatsAppCloudProvider):
    """360dialog BSP for the official WhatsApp Business Platform.

    Same Cloud-API payloads as Meta direct; the BSP differs only in host and
    credential header (``D360-API-KEY``). The API number must be provisioned
    for the Platform — it cannot be the regular WhatsApp app number.
    """

    def _messages_url(self, phone_id: str, version: str) -> str:
        return "https://waba-v2.360dialog.io/messages"

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"D360-API-KEY": token, "Content-Type": "application/json"}


def build_whatsapp_provider(config: dict[str, Any]) -> WhatsAppCloudProvider:
    """Pick the WhatsApp transport: Meta direct, or the 360dialog BSP."""
    if str(config.get("bsp", "")).lower() in ("360dialog", "whatsapp_360dialog"):
        return WhatsApp360DialogProvider(config)
    return WhatsAppCloudProvider(config)


class MockLLMProvider:
    """Deterministic local provider; output is always stored as a suggestion."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def analyze(self, text: str, *, locale: str) -> AIAnalysisResult:
        normalized = text.lower()
        priority = (
            "high"
            if any(word in normalized for word in ("urgent", "emergency", "عاجل"))
            else "normal"
        )
        sentiment = (
            "negative"
            if any(word in normalized for word in ("angry", "unacceptable", "upset", "غاضب", "سيئ"))
            else "neutral"
        )
        reply = (
            "شكرًا لتواصلكم معنا. نراجع الشكوى وسنوافيكم بالتحديثات."
            if locale == "ar"
            else "Thank you for contacting us. An agent will review your complaint and update you."
        )
        summary = text.strip()[:240] or "No complaint details supplied."
        items = (
            AIInsightDraft("classification", {"category": "claims", "priority": priority}, 0.72),
            AIInsightDraft("sentiment", {"sentiment": sentiment}, 0.7),
            AIInsightDraft("summary", {"summary": summary}, 0.68),
            AIInsightDraft("reply", {"reply": reply, "locale": locale}, 0.65),
        )
        return AIAnalysisResult(items, "mock-llm-v1")

    async def test_connection(self) -> None:
        # Deterministic in-process adapter; nothing remote to probe. The worker
        # refuses mock providers whenever ENVIRONMENT=production.
        return None


class OpenAICompatibleProvider:
    """OpenAI-compatible chat completions adapter with strict JSON result parsing.

    Supports model selection plus bounded ``timeout_seconds`` (5-120),
    ``max_retries`` (0-5, exponential backoff on 429/5xx/network) and
    ``max_tokens`` (64-4096) cost limits from the encrypted provider config.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _budget(self) -> tuple[float, int, int]:
        timeout = str(self.config.get("timeout_seconds", "")).strip()
        retries = str(self.config.get("max_retries", "")).strip()
        max_tokens = str(self.config.get("max_tokens", "")).strip()
        timeout_value = float(timeout) if timeout.isdigit() else 30.0
        retry_value = int(retries) if retries.isdigit() else 2
        token_value = int(max_tokens) if max_tokens.isdigit() else 1024
        return (
            min(max(timeout_value, 5.0), 120.0),
            min(max(retry_value, 0), 5),
            min(max(token_value, 64), 4096),
        )

    async def analyze(self, text: str, *, locale: str) -> AIAnalysisResult:
        api_key = str(self.config.get("api_key", ""))
        model = str(self.config.get("model", "gpt-4o-mini"))
        base_url = str(self.config.get("base_url", "https://api.openai.com/v1"))
        if not api_key or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model):
            raise IntegrationProviderError("llm_configuration_missing", retryable=False)
        if not base_url.startswith("https://"):
            raise IntegrationProviderError("llm_endpoint_must_use_https", retryable=False)
        try:
            await pin_public_url(base_url)
        except UnsafeDestination as exc:
            raise IntegrationProviderError("llm_endpoint_blocked", retryable=False) from exc
        timeout, retries, max_tokens = self._budget()
        prompt = (
            "Return one JSON object with classification {category,priority}, "
            "sentiment {sentiment}, summary {summary}, and reply {reply,locale}. "
            "Priority must be low, normal, high, or urgent; sentiment positive, neutral, "
            "or negative. "
            "Do not infer identity. A human will review the reply.\nLocale: "
            + locale
            + "\nComplaint text:\n"
            + text[:6000]
        )
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                    response = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "temperature": 0,
                            "max_tokens": max_tokens,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": "You classify insurance complaints."},
                                {"role": "user", "content": prompt},
                            ],
                        },
                    )
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise IntegrationProviderError("llm_unreachable") from exc
            if response.status_code < 400:
                break
            if (response.status_code >= 500 or response.status_code == 429) and attempt < retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            raise IntegrationProviderError(
                "llm_request_failed",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        if response is None or response.status_code != 200:
            raise IntegrationProviderError("llm_request_failed")
        try:
            result = response.json()["choices"][0]["message"]["content"]
            value = json.loads(result)
            drafts = tuple(
                AIInsightDraft(kind, payload, float(value.get("confidence", 0.75)))
                for kind, payload in value.items()
                if kind in {"classification", "sentiment", "summary", "reply"}
                and isinstance(payload, dict)
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise IntegrationProviderError("llm_response_invalid", retryable=False) from exc
        if not drafts:
            raise IntegrationProviderError("llm_response_invalid", retryable=False)
        return AIAnalysisResult(drafts, model)

    async def test_connection(self) -> None:
        api_key = str(self.config.get("api_key", ""))
        base_url = str(self.config.get("base_url", "https://api.openai.com/v1"))
        if not api_key:
            raise IntegrationProviderError("llm_configuration_missing", retryable=False)
        if not base_url.startswith("https://"):
            raise IntegrationProviderError("llm_endpoint_must_use_https", retryable=False)
        try:
            await pin_public_url(base_url)
        except UnsafeDestination as exc:
            raise IntegrationProviderError("llm_endpoint_blocked", retryable=False) from exc
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    f"{base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("llm_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("llm_auth_failed", retryable=False)


def normalize_inbound_voice_event(event: dict[str, Any]) -> VoiceCallResult:
    """Map a signed inbound voice event (e.g. Twilio SpeechResult) to a result.

    Pure local transformation of webhook data; the transcript already comes from
    the provider's real speech recognition, so no mock is involved in production.
    """
    call_id = str(event.get("provider_call_id") or event.get("call_id") or "inbound")[:100]
    if not bool(event.get("consent_confirmed")):
        return VoiceCallResult(call_id, None, "human_fallback_no_consent")
    transcript = str(event.get("text") or "")[:8000] or None
    return VoiceCallResult(
        call_id,
        transcript,
        "human_handoff" if event.get("request_human") else "completed",
    )


class MockVoiceProvider:
    """Working mock that models scheduled calls, consent and human fallback."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def follow_up(
        self, request: VoiceCallRequest, *, idempotency_key: str
    ) -> VoiceCallResult:
        if not request.consent_confirmed:
            return VoiceCallResult(None, None, "human_fallback_no_consent")
        return VoiceCallResult(
            f"mock-call-{idempotency_key[:20]}",
            (
                "AI assistant called the customer for a complaint follow-up. "
                "Customer requested a status update."
            ),
            "completed",
            recording=b"mock-audio-recording",
        )

    async def answer_inbound(self, event: dict[str, Any]) -> VoiceCallResult:
        return normalize_inbound_voice_event(event)

    async def test_connection(self) -> None:
        # Deterministic in-process adapter; nothing remote to probe. The worker
        # refuses mock providers whenever ENVIRONMENT=production.
        return None


# Twilio hands over a URL, not the audio, so the recording has to be
# collected with the account credentials attached. That makes the host an
# access-control decision rather than a detail: sending Basic auth to a URL
# that merely arrived in a payload would hand the account token to whoever
# supplied it. Only Twilio's own API hosts are ever dialled.
_TWILIO_RECORDING_HOST = re.compile(r"\Aapi(?:\.[a-z0-9-]+)?\.twilio\.com\Z")
MAX_RECORDING_BYTES = 25 * 1024 * 1024


async def fetch_twilio_recording(
    recording_url: str, config: dict[str, Any], *, max_bytes: int = MAX_RECORDING_BYTES
) -> bytes:
    """Download a finished Twilio recording as MP3 bytes."""
    account_sid = str(config.get("account_sid", ""))
    auth_token = str(config.get("auth_token", ""))
    if not account_sid or not auth_token:
        raise IntegrationProviderError("twilio_voice_configuration_missing", retryable=False)
    parsed = urlsplit(recording_url)
    if parsed.scheme != "https" or not _TWILIO_RECORDING_HOST.fullmatch(parsed.hostname or ""):
        raise IntegrationProviderError("voice_recording_url_rejected", retryable=False)
    # The bare resource URL answers with the recording's metadata; the audio
    # itself is under an explicit media extension.
    path = parsed.path if parsed.path.endswith((".mp3", ".wav")) else f"{parsed.path}.mp3"
    url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    collected = bytearray()
    try:
        # httpx drops the Authorization header when a redirect leaves the
        # origin, so the hop to Twilio's media CDN carries no credentials.
        async with (
            httpx.AsyncClient(timeout=30, follow_redirects=True, max_redirects=3) as client,
            client.stream("GET", url, auth=(account_sid, auth_token)) as response,
        ):
            if response.status_code != 200:
                raise IntegrationProviderError(
                    "voice_recording_unavailable",
                    retryable=response.status_code >= 500 or response.status_code == 429,
                )
            async for chunk in response.aiter_bytes():
                collected.extend(chunk)
                if len(collected) > max_bytes:
                    raise IntegrationProviderError("voice_recording_too_large", retryable=False)
    except httpx.HTTPError as exc:
        raise IntegrationProviderError("voice_recording_unreachable") from exc
    if not collected:
        raise IntegrationProviderError("voice_recording_empty", retryable=True)
    return bytes(collected)


class TwilioVoiceProvider:
    """Twilio Programmable Voice outbound adapter using generated, consent-aware TwiML."""

    endpoint = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    async def follow_up(
        self, request: VoiceCallRequest, *, idempotency_key: str
    ) -> VoiceCallResult:
        sid = str(self.config.get("account_sid", ""))
        token = str(self.config.get("auth_token", ""))
        from_number = str(self.config.get("from_number", ""))
        callback = str(self.config.get("status_callback_url", ""))
        if not request.consent_confirmed:
            return VoiceCallResult(None, None, "human_fallback_no_consent")
        if not sid or not token or not from_number or not callback:
            raise IntegrationProviderError("twilio_voice_configuration_missing", retryable=False)
        language = "ar-XA" if request.language == "ar" else "en-US"
        script = escape(request.script[:2000])
        twiml = (
            '<Response><Say language="'
            + language
            + '">'
            + script
            + '</Say><Gather numDigits="1" timeout="6"/>'
            '<Say language="'
            + language
            + '">Please wait for a human agent to follow up.</Say></Response>'
        )
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    self.endpoint.format(account_sid=sid),
                    auth=(sid, token),
                    data={
                        "To": request.mobile,
                        "From": from_number,
                        "Twiml": twiml,
                        "StatusCallback": callback,
                        "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                        "StatusCallbackMethod": "POST",
                    },
                    headers={"Idempotency-Key": idempotency_key[:128]},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("twilio_voice_unreachable") from exc
        if response.status_code not in (200, 201):
            raise IntegrationProviderError(
                "twilio_voice_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            call_id = str(response.json()["sid"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationProviderError(
                "twilio_voice_response_invalid", retryable=False
            ) from exc
        return VoiceCallResult(call_id, None, "initiated")

    async def answer_inbound(self, event: dict[str, Any]) -> VoiceCallResult:
        return normalize_inbound_voice_event(event)

    async def test_connection(self) -> None:
        sid = str(self.config.get("account_sid", ""))
        token = str(self.config.get("auth_token", ""))
        from_number = str(self.config.get("from_number", ""))
        callback = str(self.config.get("status_callback_url", ""))
        if not sid or not token or not from_number or not callback:
            raise IntegrationProviderError("twilio_voice_configuration_missing", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", auth=(sid, token)
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("twilio_voice_unreachable") from exc
        if response.status_code == 401:
            raise IntegrationProviderError("twilio_auth_failed", retryable=False)
        if response.status_code != 200:
            raise IntegrationProviderError("twilio_account_unavailable")
        try:
            status = str(response.json().get("status", ""))
        except (ValueError, TypeError):
            raise IntegrationProviderError("twilio_response_invalid") from None
        if status != "active":
            raise IntegrationProviderError("twilio_account_inactive", retryable=False)


class VonageVoiceProvider:
    """Vonage Voice API adapter. Public event callbacks are separately HMAC-verified."""

    endpoint = "https://api.nexmo.com/v1/calls"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _jwt(self) -> str:
        application_id = str(self.config.get("application_id", ""))
        private_key = str(self.config.get("private_key", ""))
        if not application_id or not private_key:
            raise IntegrationProviderError("vonage_voice_configuration_missing", retryable=False)
        now = int(datetime.now().timestamp())
        try:
            return str(
                jwt.encode(
                    {
                        "iss": application_id,
                        "iat": now,
                        "exp": now + 300,
                        "jti": str(uuid.uuid4()),
                    },
                    private_key,
                    algorithm="RS256",
                    headers={"kid": application_id},
                )
            )
        except Exception as exc:
            raise IntegrationProviderError(
                "vonage_voice_credentials_invalid", retryable=False
            ) from exc

    async def follow_up(
        self, request: VoiceCallRequest, *, idempotency_key: str
    ) -> VoiceCallResult:
        from_number = str(self.config.get("from_number", ""))
        callback = str(self.config.get("status_callback_url", ""))
        if not request.consent_confirmed:
            return VoiceCallResult(None, None, "human_fallback_no_consent")
        if not from_number or not callback:
            raise IntegrationProviderError("vonage_voice_configuration_missing", retryable=False)
        voice = "Lea" if request.language == "ar" else "Amy"
        payload = {
            "to": [{"type": "phone", "number": request.mobile.lstrip("+")}],
            "from": {"type": "phone", "number": from_number.lstrip("+")},
            "ncco": [{"action": "talk", "text": request.script[:2000], "voiceName": voice}],
            "event_url": [callback],
            "event_method": "POST",
        }
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self._jwt()}",
                        "Idempotency-Key": idempotency_key[:128],
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("vonage_voice_unreachable") from exc
        if response.status_code not in (200, 201, 202):
            raise IntegrationProviderError(
                "vonage_voice_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            call_id = str(response.json()["uuid"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationProviderError(
                "vonage_voice_response_invalid", retryable=False
            ) from exc
        return VoiceCallResult(call_id, None, "initiated")

    async def answer_inbound(self, event: dict[str, Any]) -> VoiceCallResult:
        return normalize_inbound_voice_event(event)

    async def test_connection(self) -> None:
        try:
            token = self._jwt()
        except IntegrationProviderError:
            raise
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    "https://api.nexmo.com/v1/account",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("vonage_voice_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("vonage_auth_failed", retryable=False)


class MockSpeechProvider:
    """Lightweight STT/TTS/LLM seam for voice-flow tests; it never records audio."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def transcribe(self, audio: bytes, *, language: str) -> str:
        return "Mock transcription omitted audio content."

    async def synthesize(self, text: str, *, language: str) -> bytes:
        return f"mock-audio:{language}:{text}".encode()

    async def respond(self, prompt: str, *, language: str) -> str:
        return (
            "يرجى الانتظار حتى يتواصل معك أحد الموظفين."
            if language == "ar"
            else "A human agent will follow up."
        )

    async def test_connection(self) -> None:
        # Deterministic in-process adapter; nothing remote to probe. The worker
        # refuses mock providers whenever ENVIRONMENT=production.
        return None


class SignedWebhookProvider:
    """HMAC-SHA256 delivery with no redirects and no credential-bearing URLs."""

    async def send(self, url: str, secret: str, body: bytes, *, timestamp: int) -> None:
        import hashlib
        import hmac

        signature = hmac.new(
            secret.encode("utf-8"), str(timestamp).encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        try:
            target = await pin_public_url(url)
        except UnsafeDestination as exc:
            raise IntegrationProviderError("webhook_destination_blocked", retryable=False) from exc
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(
                    target.url,
                    content=body,
                    headers={
                        "Host": target.host_header,
                        "Content-Type": "application/json",
                        "X-Insurance-Timestamp": str(timestamp),
                        "X-Insurance-Signature": f"sha256={signature}",
                    },
                    extensions=target.extensions,
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("webhook_unreachable") from exc
        if not 200 <= response.status_code < 300:
            raise IntegrationProviderError(
                "webhook_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )


# ---------------------------------------------------------------------------
# OAuth 2.0 (Gmail / Microsoft 365) -------------------------------------------
# ---------------------------------------------------------------------------


class OAuthTokenBroker:
    """Exchange and refresh OAuth tokens without ever logging them.

    Access tokens are cached in memory until shortly before expiry. Rotated
    refresh tokens are used for the current process only; the stored refresh
    token stays valid on both Google and Microsoft unless the operator revokes
    it, so no credential write-back path is required from the worker.
    """

    _EXPIRY_SLACK_SECONDS = 60

    def __init__(self, config: dict[str, Any], *, provider: str) -> None:
        self.config = config
        self.provider = provider
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def _token_url(self) -> str:
        if self.provider == "gmail_oauth":
            return "https://oauth2.googleapis.com/token"
        tenant = str(self.config.get("tenant") or "common")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", tenant):
            raise IntegrationProviderError("oauth_tenant_invalid", retryable=False)
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def _client(self) -> tuple[str, str]:
        client_id = str(self.config.get("client_id", ""))
        client_secret = str(self.config.get("client_secret", ""))
        if not client_id or not client_secret:
            raise IntegrationProviderError("oauth_client_configuration_missing", retryable=False)
        return client_id, client_secret

    @staticmethod
    def authorize_url(provider: str, config: dict[str, Any], redirect_uri: str, state: str) -> str:
        if provider == "gmail_oauth":
            scopes = (
                "https://www.googleapis.com/auth/gmail.send "
                "https://www.googleapis.com/auth/gmail.modify "
                "openid email"
            )
            query = urlencode(
                {
                    "client_id": str(config.get("client_id", "")),
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": scopes,
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                }
            )
            return "https://accounts.google.com/o/oauth2/v2/auth?" + query
        tenant = str(config.get("tenant") or "common")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", tenant):
            raise IntegrationProviderError("oauth_tenant_invalid", retryable=False)
        scopes = (
            "https://graph.microsoft.com/Mail.Send "
            "https://graph.microsoft.com/Mail.Read "
            "offline_access openid email"
        )
        query = urlencode(
            {
                "client_id": str(config.get("client_id", "")),
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scopes,
                "state": state,
            }
        )
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?" + query

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        client_id, client_secret = self._client()
        payload = {**data, "client_id": client_id, "client_secret": client_secret}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(self._token_url(), data=payload)
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("oauth_token_endpoint_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError(
                "oauth_token_request_rejected", retryable=response.status_code >= 500
            )
        try:
            value = response.json()
            token = str(value["access_token"])
            expires_in = int(value.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationProviderError("oauth_token_response_invalid") from exc
        if not token or expires_in <= 0:
            raise IntegrationProviderError("oauth_token_response_invalid", retryable=False)
        self._access_token = token
        self._expires_at = time.monotonic() + max(30, expires_in - self._EXPIRY_SLACK_SECONDS)
        if value.get("refresh_token"):
            self.config["refresh_token"] = str(value["refresh_token"])
        return dict(value)

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if not code or len(code) > 2048:
            raise IntegrationProviderError("oauth_code_invalid", retryable=False)
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )

    async def access_token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        refresh_token = str(self.config.get("refresh_token", ""))
        if not refresh_token:
            raise IntegrationProviderError("oauth_refresh_token_missing", retryable=False)
        await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        assert self._access_token is not None
        return self._access_token


class GmailEmailProvider:
    """Google Gmail REST API (users.messages.send) with OAuth 2.0 refresh tokens."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tokens = OAuthTokenBroker(config, provider="gmail_oauth")

    async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult:
        if not self.config.get("from_email"):
            raise IntegrationProviderError("gmail_configuration_missing", retryable=False)
        message = EmailMessage()
        message["From"] = str(self.config["from_email"])
        message["To"] = ", ".join(request.recipients)
        message["Subject"] = request.subject
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = make_msgid(idstring=idempotency_key[:24])
        message["X-Idempotency-Key"] = idempotency_key[:128]
        message.set_content(request.text_body)
        message.add_alternative(
            request.html_body
            or branded_email(
                request.subject,
                request.text_body,
                locale=request.locale,
                has_logo=request.logo is not None,
            )[1],
            subtype="html",
        )
        attach_inline_logo(message, request.logo)
        for item in request.attachments:
            maintype, _, subtype = item.mime_type.partition("/")
            if not maintype or not subtype:
                raise IntegrationProviderError("attachment_mime_invalid", retryable=False)
            message.add_attachment(
                item.content, maintype=maintype, subtype=subtype, filename=item.filename
            )
        raw = base64.urlsafe_b64encode(message.as_bytes(policy=email_policy)).decode("ascii")
        token = await self.tokens.access_token()
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"raw": raw},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("gmail_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError(
                "gmail_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            provider_id = str(response.json()["id"])
        except (ValueError, KeyError, TypeError):
            provider_id = None
        return ProviderResult(provider_id=provider_id)

    async def test_connection(self) -> None:
        token = await self.tokens.access_token()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("gmail_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("gmail_auth_failed", retryable=False)


class GmailInboxProvider:
    """Inbound Gmail polling over the Gmail API (UNSEEN -> message list -> modify)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tokens = OAuthTokenBroker(config, provider="gmail_oauth")

    @staticmethod
    def _text_body(payload: dict[str, Any]) -> str:
        chunks: list[str] = []

        def walk(node: dict[str, Any]) -> None:
            body = node.get("body", {})
            if node.get("mimeType") == "text/plain" and body.get("data"):
                with suppress(ValueError, UnicodeDecodeError):
                    chunks.append(
                        base64.urlsafe_b64decode(str(body["data"])).decode("utf-8", "replace")
                    )
            for part in node.get("parts", [])[:40]:
                if isinstance(part, dict):
                    walk(part)

        walk(payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {})
        return "\n".join(chunks).strip()[:5000]

    async def fetch_unseen(self, *, limit: int = 20) -> list[IncomingEmail]:
        token = await self.tokens.access_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                listing = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                    headers=headers,
                    params={"q": "is:unread", "maxResults": max(1, min(limit, 50))},
                )
                if listing.status_code != 200:
                    raise IntegrationProviderError(
                        "gmail_inbox_unavailable", retryable=listing.status_code >= 500
                    )
                messages: list[IncomingEmail] = []
                for item in listing.json().get("messages", [])[:50]:
                    message_id = str(item.get("id") or "")[:256]
                    if not message_id:
                        continue
                    detail = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                        headers=headers,
                        params={"format": "full"},
                    )
                    if detail.status_code != 200:
                        continue
                    value = detail.json()
                    headers_list = {
                        str(h.get("name", "")).lower(): str(h.get("value", ""))
                        for h in value.get("payload", {}).get("headers", [])[:60]
                        if isinstance(h, dict)
                    }
                    messages.append(
                        IncomingEmail(
                            message_id,
                            str(value.get("id") or message_id)[:256],
                            parseaddr(headers_list.get("from", ""))[1][:254],
                            headers_list.get("subject", "")[:500],
                            self._text_body(value),
                        )
                    )
                return messages
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("gmail_inbox_unreachable") from exc

    async def mark_seen(self, sequence_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sequence_id):
            raise IntegrationProviderError("gmail_sequence_invalid", retryable=False)
        token = await self.tokens.access_token()
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{sequence_id}/modify",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"removeLabelIds": ["UNSEEN"]},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("gmail_ack_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("gmail_ack_failed")


class OutlookEmailProvider:
    """Microsoft 365 / Outlook through Microsoft Graph (sendMail) with OAuth 2.0."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tokens = OAuthTokenBroker(config, provider="outlook_oauth")

    async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult:
        sender = str(self.config.get("from_email", ""))
        if not sender:
            raise IntegrationProviderError("outlook_configuration_missing", retryable=False)
        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(request.recipients)
        message["Subject"] = request.subject[:300]
        message.set_content(request.text_body)
        message.add_alternative(
            request.html_body
            or branded_email(
                request.subject,
                request.text_body,
                locale=request.locale,
                has_logo=request.logo is not None,
            )[1],
            subtype="html",
        )
        attach_inline_logo(message, request.logo)
        for item in request.attachments:
            maintype, _, subtype = item.mime_type.partition("/")
            message.add_attachment(
                item.content,
                maintype=maintype,
                subtype=subtype or "octet-stream",
                filename=item.filename,
            )
        token = await self.tokens.access_token()
        payload = base64.b64encode(message.as_bytes()).decode("ascii")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    "https://graph.microsoft.com/v1.0/me/sendMail",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
                    content=payload,
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("outlook_unreachable") from exc
        if response.status_code not in (202, 200):
            raise IntegrationProviderError(
                "outlook_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        return ProviderResult(provider_id=None)

    async def test_connection(self) -> None:
        token = await self.tokens.access_token()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    "https://graph.microsoft.com/v1.0/me",
                    params={"$select": "mail,userPrincipalName"},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("outlook_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("outlook_auth_failed", retryable=False)


class OutlookInboxProvider:
    """Inbound Microsoft 365 mail polling over Microsoft Graph."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tokens = OAuthTokenBroker(config, provider="outlook_oauth")

    async def fetch_unseen(self, *, limit: int = 20) -> list[IncomingEmail]:
        token = await self.tokens.access_token()
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                listing = await client.get(
                    "https://graph.microsoft.com/v1.0/me/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "$filter": "isRead eq false",
                        "$top": max(1, min(limit, 50)),
                        "$select": "id,subject,from,body,internetMessageId",
                    },
                )
                if listing.status_code != 200:
                    raise IntegrationProviderError(
                        "outlook_inbox_unavailable", retryable=listing.status_code >= 500
                    )
                messages: list[IncomingEmail] = []
                for item in listing.json().get("value", [])[:50]:
                    message_id = str(item.get("id") or "")[:256]
                    if not message_id:
                        continue
                    body = item.get("body", {})
                    content = re.sub(r"<[^>]+>", " ", str(body.get("content", "")))[:5000]
                    messages.append(
                        IncomingEmail(
                            message_id,
                            str(item.get("internetMessageId") or message_id).strip("<>")[:256],
                            str(item.get("from", {}).get("emailAddress", {}).get("address", ""))[
                                :254
                            ],
                            str(item.get("subject", ""))[:500],
                            content,
                        )
                    )
                return messages
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("outlook_inbox_unreachable") from exc

    async def mark_seen(self, sequence_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9=._+-]{1,512}", sequence_id):
            raise IntegrationProviderError("outlook_sequence_invalid", retryable=False)
        token = await self.tokens.access_token()
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.patch(
                    f"https://graph.microsoft.com/v1.0/me/messages/{sequence_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"isRead": True},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("outlook_ack_unreachable") from exc
        if response.status_code not in (200, 204):
            raise IntegrationProviderError("outlook_ack_failed")


class TwilioSMSProvider:
    """Twilio Programmable SMS with a country allowlist and delivery callbacks.

    Sending is fail-closed: the destination country code must appear in the
    operator-verified ``allowed_country_codes`` list (for example ``+20`` for
    Egypt) before any message is queued to Twilio.
    """

    endpoint = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _allowed(self, recipient: str) -> None:
        raw = str(self.config.get("allowed_country_codes", "")).strip()
        codes = [part.strip() for part in raw.split(",") if part.strip()]
        if not codes:
            raise IntegrationProviderError("sms_country_allowlist_missing", retryable=False)
        for code in codes:
            if not re.fullmatch(r"\+\d{1,4}", code):
                raise IntegrationProviderError("sms_country_allowlist_invalid", retryable=False)
        if not recipient.startswith(tuple(codes)):
            raise IntegrationProviderError("sms_destination_country_not_allowed", retryable=False)

    def _credentials(self) -> tuple[str, str, str]:
        sid = str(self.config.get("account_sid", ""))
        token = str(self.config.get("auth_token", ""))
        from_number = str(self.config.get("from_number", ""))
        if not sid or not token or not from_number:
            raise IntegrationProviderError("twilio_sms_configuration_missing", retryable=False)
        return sid, token, from_number

    async def send(self, request: SMSProviderRequest, *, idempotency_key: str) -> ProviderResult:
        sid, token, from_number = self._credentials()
        self._allowed(request.recipient)
        fields = {
            "To": request.recipient,
            "From": from_number,
            "Body": request.body[:1600],
        }
        callback = str(self.config.get("status_callback_url", ""))
        if callback:
            fields["StatusCallback"] = callback
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    self.endpoint.format(account_sid=sid),
                    auth=(sid, token),
                    data=fields,
                    headers={"Idempotency-Key": idempotency_key[:128]},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("twilio_sms_unreachable") from exc
        if response.status_code not in (200, 201):
            raise IntegrationProviderError(
                "twilio_sms_rejected",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            message_sid = str(response.json()["sid"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationProviderError("twilio_sms_response_invalid") from exc
        return ProviderResult(provider_id=message_sid)

    async def test_connection(self) -> None:
        sid, token, _ = self._credentials()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", auth=(sid, token)
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("twilio_unreachable") from exc
        if response.status_code == 401:
            raise IntegrationProviderError("twilio_auth_failed", retryable=False)
        if response.status_code != 200:
            raise IntegrationProviderError("twilio_account_unavailable")
        try:
            status = str(response.json().get("status", ""))
        except (ValueError, TypeError):
            raise IntegrationProviderError("twilio_response_invalid") from None
        if status != "active":
            raise IntegrationProviderError("twilio_account_inactive", retryable=False)


class OpenAISpeechProvider:
    """Real STT (Whisper transcription) and TTS (OpenAI audio speech)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _headers(self) -> dict[str, str]:
        api_key = str(self.config.get("api_key", ""))
        if not api_key:
            raise IntegrationProviderError("speech_configuration_missing", retryable=False)
        return {"Authorization": f"Bearer {api_key}"}

    def _base_url(self) -> str:
        base_url = str(self.config.get("base_url", "https://api.openai.com/v1"))
        if not base_url.startswith("https://"):
            raise IntegrationProviderError("speech_endpoint_must_use_https", retryable=False)
        return base_url.rstrip("/")

    async def transcribe(self, audio: bytes, *, language: str) -> str:
        if not audio:
            raise IntegrationProviderError("speech_audio_empty", retryable=False)
        stt_model = str(self.config.get("stt_model") or "whisper-1")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", stt_model):
            raise IntegrationProviderError("speech_model_invalid", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.post(
                    f"{self._base_url()}/audio/transcriptions",
                    headers=self._headers(),
                    files={"file": ("audio.mp3", audio, "application/octet-stream")},
                    data={"model": stt_model, "language": language[:8]},
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("speech_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError(
                "speech_transcription_failed",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            return str(response.json()["text"])[:8000]
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationProviderError("speech_response_invalid") from exc

    async def synthesize(self, text: str, *, language: str) -> bytes:
        tts_model = str(self.config.get("tts_model") or "gpt-4o-mini-tts")
        voice = str(self.config.get("voice") or "alloy")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", tts_model) or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,64}", voice
        ):
            raise IntegrationProviderError("speech_model_invalid", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.post(
                    f"{self._base_url()}/audio/speech",
                    headers=self._headers(),
                    json={
                        "model": tts_model,
                        "voice": voice,
                        "input": text[:4096],
                        "response_format": "mp3",
                    },
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("speech_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError(
                "speech_synthesis_failed",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        return response.content[: 5 * 1024 * 1024]

    async def test_connection(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(f"{self._base_url()}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("speech_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError("speech_auth_failed", retryable=False)


# ---------------------------------------------------------------------------
# Registries used by the worker, the connection-test service and the admin UI ---
# ---------------------------------------------------------------------------

EMAIL_PROVIDER_CLASSES: dict[str, type] = {
    "smtp": SMTPEmailProvider,
    "sendgrid": SendGridEmailProvider,
    "ses": SESEmailProvider,
    "gmail_oauth": GmailEmailProvider,
    "outlook_oauth": OutlookEmailProvider,
}

INBOUND_EMAIL_PROVIDER_CLASSES: dict[str, type] = {
    "imap": IMAPMailProvider,
    "gmail_oauth": GmailInboxProvider,
    "outlook_oauth": OutlookInboxProvider,
}

SMS_PROVIDER_CLASSES: dict[str, type] = {"twilio_sms": TwilioSMSProvider}

VOICE_PROVIDER_CLASSES: dict[str, type] = {
    "mock_voice": MockVoiceProvider,
    "twilio": TwilioVoiceProvider,
    "vonage": VonageVoiceProvider,
}

LLM_PROVIDER_CLASSES: dict[str, type] = {
    "mock_llm": MockLLMProvider,
    "openai": OpenAICompatibleProvider,
}

# Providers that can be probed without sending any message or placing any call.
CONNECTION_TESTERS: dict[str, type] = {
    **EMAIL_PROVIDER_CLASSES,
    "imap": IMAPMailProvider,
    "whatsapp_cloud": WhatsAppCloudProvider,
    "whatsapp_360dialog": WhatsApp360DialogProvider,
    **SMS_PROVIDER_CLASSES,
    **LLM_PROVIDER_CLASSES,
    **VOICE_PROVIDER_CLASSES,
    "openai_speech": OpenAISpeechProvider,
}

# Fields that must exist in the merged (stored + submitted) credential blob
# before a provider may be enabled. Empty sets are valid (e.g. mock adapters).
PROVIDER_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "smtp": frozenset({"host", "from_email"}),
    "sendgrid": frozenset({"api_key", "from_email"}),
    "ses": frozenset({"region", "from_email"}),
    "gmail_oauth": frozenset({"client_id", "client_secret", "refresh_token", "from_email"}),
    "outlook_oauth": frozenset({"client_id", "client_secret", "refresh_token", "from_email"}),
    "whatsapp_cloud": frozenset({"access_token", "phone_number_id", "app_secret"}),
    "whatsapp_360dialog": frozenset({"access_token", "phone_number_id"}),
    "email_webhook": frozenset({"webhook_secret"}),
    "imap": frozenset({"host", "username", "password"}),
    "twilio_sms": frozenset({"account_sid", "auth_token", "from_number"}),
    "mock_llm": frozenset(),
    "openai": frozenset({"api_key"}),
    "mock_voice": frozenset(),
    "twilio": frozenset({"account_sid", "auth_token", "from_number", "status_callback_url"}),
    "vonage": frozenset({"application_id", "private_key", "from_number", "status_callback_url"}),
    "openai_speech": frozenset({"api_key"}),
    "clamav": frozenset({"host"}),
}

# Providers that must never run in production (development/tests only).
MOCK_PROVIDERS = frozenset({"mock_llm", "mock_voice"})


def build_provider(provider: str, config: dict[str, Any]) -> Any:
    """Instantiate the adapter for ``provider``; unknown names fail closed."""
    if provider in EMAIL_PROVIDER_CLASSES:
        return EMAIL_PROVIDER_CLASSES[provider](config)
    if provider in SMS_PROVIDER_CLASSES:
        return SMS_PROVIDER_CLASSES[provider](config)
    if provider in LLM_PROVIDER_CLASSES:
        return LLM_PROVIDER_CLASSES[provider](config)
    if provider in VOICE_PROVIDER_CLASSES:
        return VOICE_PROVIDER_CLASSES[provider](config)
    if provider == "whatsapp_cloud":
        return WhatsAppCloudProvider(config)
    if provider == "whatsapp_360dialog":
        return WhatsApp360DialogProvider(config)
    if provider == "openai_speech":
        return OpenAISpeechProvider(config)
    raise IntegrationProviderError("provider_unsupported", retryable=False)


async def probe_provider(provider: str, config: dict[str, Any]) -> None:
    """Run a non-mutating credential/connection probe.

    Never sends a message or places a call; raises
    :class:`IntegrationProviderError` with a log-safe code on any failure.
    """
    if provider == "clamav":
        from app.services.providers import ClamAVScanner, ProviderUnavailable

        try:
            await ClamAVScanner(config).ping()
        except ProviderUnavailable as exc:
            raise IntegrationProviderError("clamav_unreachable") from exc
        return
    tester_class = CONNECTION_TESTERS.get(provider)
    if tester_class is None:
        raise IntegrationProviderError("provider_connection_test_unsupported", retryable=False)
    tester = tester_class(config)
    probe = getattr(tester, "test_connection", None)
    if probe is None:
        raise IntegrationProviderError("provider_connection_test_unsupported", retryable=False)
    await probe()
