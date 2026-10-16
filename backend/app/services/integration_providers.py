"""Replaceable adapters for outbound email, WhatsApp, LLM and voice providers.

Only the mock LLM/voice adapters are suitable for local exercises. Production
implementations are intentionally fail-closed when credentials or providers are absent.
"""

from __future__ import annotations

import asyncio
import base64
import imaplib
import json
import re
import ssl
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, Protocol
from xml.sax.saxutils import escape

import aiosmtplib
import boto3
import httpx
import jwt

from app.security.ssrf import UnsafeDestination, pin_public_url


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
                start_tls=_config_bool(self.config.get("start_tls"), True),
                timeout=20,
            )
        except (OSError, aiosmtplib.SMTPException, TimeoutError) as exc:
            raise IntegrationProviderError("smtp_delivery_failed") from exc
        return ProviderResult(provider_id=message["Message-ID"])


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
            "content": [{"type": "text/plain", "value": request.text_body}],
            "custom_args": {"idempotency_key": idempotency_key[:128]},
        }
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


class SESEmailProvider:
    """AWS SES RawMessage adapter with attachment support and bounded SMTP-equivalent payloads."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

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
        for item in request.attachments:
            maintype, _, subtype = item.mime_type.partition("/")
            message.add_attachment(
                item.content,
                maintype=maintype,
                subtype=subtype,
                filename=item.filename,
            )
        raw = message.as_bytes()
        try:
            client = boto3.client(
                "ses",
                region_name=region,
                aws_access_key_id=self.config.get("access_key_id"),
                aws_secret_access_key=self.config.get("secret_access_key"),
            )
            result = await asyncio.to_thread(
                client.send_raw_email,
                Source=sender,
                Destinations=list(request.recipients),
                RawMessage={"Data": raw},
            )
        except Exception as exc:
            raise IntegrationProviderError("ses_delivery_failed") from exc
        return ProviderResult(provider_id=str(result.get("MessageId", "")) or None)


class WhatsAppCloudProvider:
    """Meta WhatsApp Business Cloud API adapter (template or 24-hour text message)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

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
                    f"https://graph.facebook.com/{version}/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
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


class MockLLMProvider:
    """Deterministic local provider; output is always stored as a suggestion."""

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


class OpenAICompatibleProvider:
    """OpenAI-compatible chat completions adapter with strict JSON result parsing."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

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
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": "You classify insurance complaints."},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
        except httpx.HTTPError as exc:
            raise IntegrationProviderError("llm_unreachable") from exc
        if response.status_code != 200:
            raise IntegrationProviderError(
                "llm_request_failed",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
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


class MockVoiceProvider:
    """Working mock that models scheduled calls, consent and human fallback."""

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
        call_id = str(event.get("provider_call_id", "mock-inbound"))[:100]
        if not bool(event.get("consent_confirmed")):
            return VoiceCallResult(call_id, None, "human_fallback_no_consent")
        transcript = str(
            event.get("text")
            or (
                "Caller consented to an AI assistant. "
                "The assistant provided a complaint status update."
            )
        )[:8000]
        return VoiceCallResult(
            call_id,
            transcript,
            "human_handoff" if event.get("request_human") else "completed",
            recording=b"mock-audio-recording",
        )


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
        return await MockVoiceProvider().answer_inbound(event)


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
        return await MockVoiceProvider().answer_inbound(event)


class MockSpeechProvider:
    """Lightweight STT/TTS/LLM seam for voice-flow tests; it never records audio."""

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
