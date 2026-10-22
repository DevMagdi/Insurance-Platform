"""Restricted periodic outbox delivery and fail-closed virus-scan hook."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from pydantic import Field
from pydantic_settings import BaseSettings
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import make_worker_engine
from app.observability import metrics
from app.observability.logging import configure_logging
from app.observability.sentry import init_sentry
from app.security.blobs import ATTACHMENT, EXPORT, RECORDING, BlobCipher
from app.security.crypto import build_keyring, idempotency_root
from app.security.integration_crypto import SecretCipher, derive_idempotency_key
from app.services.backpressure import DEPTH_KEY
from app.services.email_templates import branded_email
from app.services.exports import MAX_EXPORT_BYTES, render_export
from app.services.intake import (
    TICKET_RE,
    InboundEvent,
    InboundMedia,
    RequestIntakeService,
    split_from_header,
)
from app.services.integration_providers import (
    EMAIL_PROVIDER_CLASSES,
    INBOUND_EMAIL_PROVIDER_CLASSES,
    LLM_PROVIDER_CLASSES,
    MOCK_PROVIDERS,
    SMS_PROVIDER_CLASSES,
    VOICE_PROVIDER_CLASSES,
    EmailProvider,
    EmailSendRequest,
    IntegrationProviderError,
    OutboundAttachment,
    SignedWebhookProvider,
    SMSProviderRequest,
    VoiceCallRequest,
    VoiceProvider,
    WhatsAppCloudProvider,
    WhatsAppSendRequest,
    fetch_twilio_recording,
    normalize_inbound_voice_event,
)
from app.services.integrations import (
    ai_insight_aad,
    call_transcript_aad,
    config_aad,
    job_aad,
    minimize_pii,
    webhook_secret_aad,
)
from app.services.providers import (
    ClamAVScanner,
    Delivery,
    ScanTarget,
    SMTPDelivery,
    UnconfiguredVirusScanner,
    VirusScanner,
)
from app.services.render import EXTENSIONS, MEDIA_TYPES
from app.services.reports import ReportService, render_report, report_message

logger = logging.getLogger(__name__)


class WorkerConfig(BaseSettings):
    worker_database_url: str = Field(validation_alias="WORKER_DATABASE_URL")
    app_encryption_key: str = Field(validation_alias="APP_ENCRYPTION_KEY")
    app_encryption_key_id: int = Field(default=1, validation_alias="APP_ENCRYPTION_KEY_ID")
    app_encryption_previous_keys: str = Field(
        default="", validation_alias="APP_ENCRYPTION_PREVIOUS_KEYS"
    )
    app_idempotency_key: str | None = Field(default=None, validation_alias="APP_IDEMPOTENCY_KEY")
    worker_metrics_port: int | None = Field(default=None, validation_alias="WORKER_METRICS_PORT")
    sentry_dsn: str | None = Field(default=None, validation_alias="SENTRY_DSN")
    app_release: str | None = Field(default=None, validation_alias="APP_RELEASE")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=True, validation_alias="LOG_JSON")
    retention_session_days: int = Field(default=30, validation_alias="RETENTION_SESSION_DAYS")
    retention_outbox_days: int = Field(default=180, validation_alias="RETENTION_OUTBOX_DAYS")
    # How far ahead the brokerage wants to hear about a contract renewal,
    # and how long a request may sit answered-and-ignored before it closes
    # itself. Zero disables auto-closing entirely.
    renewal_reminder_days: int = Field(default=30, validation_alias="RENEWAL_REMINDER_DAYS")
    auto_close_quiet_days: int = Field(default=14, validation_alias="AUTO_CLOSE_QUIET_DAYS")
    redis_url: str = "redis://redis:6379/0"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_from: str = ""
    support_email: str = "support@example.invalid"
    public_app_url: str = "http://localhost:8080"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_start_tls: bool = True
    smtp_use_tls: bool = False
    environment: str = "production"


async def startup(ctx: dict[str, Any]) -> None:
    config = WorkerConfig()  # type: ignore[call-arg]
    configure_logging(
        level=config.log_level,
        json_logs=config.log_json,
        service="worker",
        release=config.app_release,
    )
    init_sentry(config.sentry_dsn, config.environment, config.app_release, 0.0)
    if config.worker_metrics_port:
        from prometheus_client import start_http_server

        start_http_server(config.worker_metrics_port)
    ctx["config"] = config
    ctx["engine"] = make_worker_engine(config.worker_database_url)

    async def platform_name() -> str:
        rows = await _worker_query(
            ctx["engine"],
            "SELECT platform_name FROM app_security.public_platform_brand()",
            {},
        )
        return str(rows[0][0]) if rows else "Insurance"

    ctx["platform_name"] = platform_name

    async def platform_logo() -> Any:
        from app.services.brand_assets import detect_logo

        rows = await _worker_query(
            ctx["engine"], "SELECT * FROM app_security.public_platform_logo()", {}
        )
        if not rows:
            return None
        return detect_logo(bytes(rows[0].logo_content), str(rows[0].logo_mime_type))

    ctx["platform_logo"] = platform_logo
    ctx["delivery"] = SMTPDelivery(config, brand_loader=platform_name, logo_loader=platform_logo)
    ctx["scanner"] = UnconfiguredVirusScanner()
    keyring = build_keyring(
        config.app_encryption_key,
        config.app_encryption_key_id,
        config.app_encryption_previous_keys,
    )
    # Private binaries live in PostgreSQL; the worker decrypts them in memory only.
    ctx["blobs"] = BlobCipher(keyring)
    ctx["integration_cipher"] = SecretCipher(keyring)
    ctx["integration_idempotency_key"] = derive_idempotency_key(
        idempotency_root(keyring, config.app_idempotency_key)
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    engine: AsyncEngine = ctx["engine"]
    await engine.dispose()


async def _worker_query(engine: AsyncEngine, query: str, params: dict[str, Any]) -> list[Any]:
    async with engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.scope', 'worker', true)"))
        result = await connection.execute(text(query), params)
        return list(result.all())


def enabled_notification_channels(preferences: Any) -> tuple[str, ...]:
    """Normalize customer preferences; missing values retain opt-out defaults."""

    if isinstance(preferences, str):
        try:
            preferences = json.loads(preferences)
        except json.JSONDecodeError:
            preferences = None
    if not isinstance(preferences, dict):
        preferences = {}
    return tuple(
        channel
        for channel in ("email", "whatsapp", "sms")
        if preferences.get(channel, True) is not False
    )


def _environment(ctx: dict[str, Any]) -> str:
    """The deployment environment; absent only in focused unit tests."""
    return str(getattr(ctx.get("config"), "environment", "test"))


def _refuse_mock(provider: str, environment: str) -> None:
    """Mock adapters exist for tests/development only and fail closed in production."""
    if provider in MOCK_PROVIDERS and environment == "production":
        raise IntegrationProviderError("mock_provider_forbidden_in_production", retryable=False)


def _email_provider(provider: str, config: dict[str, Any], environment: str) -> EmailProvider:
    _refuse_mock(provider, environment)
    provider_class = EMAIL_PROVIDER_CLASSES.get(provider)
    if provider_class is None:
        raise IntegrationProviderError("email_provider_unsupported", retryable=False)
    return cast(EmailProvider, provider_class(config))


def _sms_provider(provider: str, config: dict[str, Any], environment: str) -> Any:
    _refuse_mock(provider, environment)
    provider_class = SMS_PROVIDER_CLASSES.get(provider)
    if provider_class is None:
        raise IntegrationProviderError("sms_provider_unsupported", retryable=False)
    return provider_class(config)


def _voice_provider(provider: str, config: dict[str, Any], environment: str) -> VoiceProvider:
    _refuse_mock(provider, environment)
    provider_class = VOICE_PROVIDER_CLASSES.get(provider)
    if provider_class is None:
        raise IntegrationProviderError("voice_provider_unsupported", retryable=False)
    return cast(VoiceProvider, provider_class(config))


def _minimize_ai_value(value: Any) -> Any:
    if isinstance(value, str):
        return minimize_pii(value, max_chars=5000)
    if isinstance(value, list):
        return [_minimize_ai_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _minimize_ai_value(item)
            for key, item in list(value.items())[:50]
            if isinstance(key, str)
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _encrypt_call_recording(
    ctx: dict[str, Any], call_id: UUID, recording: bytes | None
) -> bytes | None:
    """Encrypt the provider recording for ``call_logs.recording_content``."""
    if recording is None:
        return None
    if len(recording) > 25 * 1024 * 1024:
        raise IntegrationProviderError("voice_recording_too_large", retryable=False)
    blobs: BlobCipher = ctx["blobs"]
    return blobs.encrypt(recording, purpose=RECORDING, resource_id=call_id)


async def _download_whatsapp_media(
    descriptors: list[Any], config: dict[str, Any], environment: str
) -> list[InboundMedia]:
    """Exchange WhatsApp media ids for bytes, skipping whatever fails.

    One unreadable attachment must never cost the brokerage the request, so
    failures are logged and the message is ingested with whatever arrived.
    """
    media: list[InboundMedia] = []
    if not descriptors:
        return media
    provider = WhatsAppCloudProvider(config)
    for descriptor in descriptors[:10]:
        if not isinstance(descriptor, dict):
            continue
        media_id = str(descriptor.get("media_id") or "")
        if not media_id:
            continue
        declared_mime = str(descriptor.get("mime_type") or "application/octet-stream")
        try:
            content, mime_type, filename = await provider.fetch_media(
                media_id, max_bytes=RequestIntakeService.MAX_MEDIA_BYTES
            )
        except IntegrationProviderError as exc:
            logger.warning(
                "WhatsApp media was not downloaded media_id=%s error=%s", media_id, exc.code
            )
            continue
        media.append(
            InboundMedia(
                media_id=media_id,
                mime_type=declared_mime or mime_type,
                filename=str(descriptor.get("filename") or "") or filename,
                caption=descriptor.get("caption"),
                content=content,
            )
        )
    return media


async def _process_inbound_message(
    ctx: dict[str, Any],
    row: Any,
    kind: str,
    payload: dict[str, Any],
    config: dict[str, Any],
    cipher: SecretCipher,
    *,
    job_id: UUID,
) -> None:
    """Create or continue a request from an inbound WhatsApp/e-mail message."""
    channel = "whatsapp" if kind == "inbound_whatsapp" else "email"
    media: list[InboundMedia] = []
    if channel == "whatsapp":
        descriptors = payload.get("media")
        if isinstance(descriptors, list) and descriptors:
            with suppress(IntegrationProviderError):
                media = await _download_whatsapp_media(descriptors, config, _environment(ctx))
    else:
        for item in (
            payload.get("attachments", [])[:10]
            if isinstance(payload.get("attachments"), list)
            else []
        ):
            if not isinstance(item, dict):
                continue
            raw = item.get("content_b64")
            if not isinstance(raw, str):
                continue
            try:
                content = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                continue
            if not content or len(content) > RequestIntakeService.MAX_MEDIA_BYTES:
                continue
            media.append(
                InboundMedia(
                    media_id=str(item.get("content_id") or item.get("filename") or "part"),
                    mime_type=str(item.get("mime_type") or "application/octet-stream")[:180],
                    filename=str(item.get("filename") or "attachment.bin")[:255],
                    content=content,
                )
            )

    event = InboundEvent(
        channel=channel,
        external_event_id=str(payload.get("external_message_id") or job_id)[:256],
        sender=str(payload.get("sender") or payload.get("from") or "")[:320],
        body=str(payload.get("body") or "")[:5000],
        sender_name=(str(payload.get("sender_name")) if payload.get("sender_name") else None),
        subject=(str(payload.get("subject"))[:300] if payload.get("subject") else None),
        company_id=row.company_id,
        media=media,
    )
    intake = RequestIntakeService(ctx["blobs"])
    engine: AsyncEngine = ctx["engine"]
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.scope','worker',false)"),
        )
        session = AsyncSession(bind=connection, expire_on_commit=False)
        outcome = await intake.ingest(session, event)
    logger.info(
        "Inbound %s message intake state=%s ticket=%s attachments=%s",
        channel,
        outcome.state,
        outcome.ticket_no,
        outcome.attachments,
    )
    await _worker_query(
        ctx["engine"],
        "SELECT app_security.complete_inbound_event(:id,:attempt,CAST(:data AS jsonb))",
        {
            "id": job_id,
            "attempt": row.attempt,
            "data": json.dumps(
                {
                    **{k: v for k, v in payload.items() if k != "media"},
                    "ticket_no": outcome.ticket_no or payload.get("ticket_no"),
                    "intake_state": outcome.state,
                    "intake_attachments": outcome.attachments,
                }
            ),
        },
    )


async def _process_inbound_call(
    ctx: dict[str, Any],
    row: Any,
    payload: dict[str, Any],
    config: dict[str, Any],
    cipher: SecretCipher,
    *,
    job_id: UUID,
) -> None:
    """Turn a telephone call into a request, with its recording attached.

    Somebody ringing the brokerage is usually opening something new rather
    than quoting a REQ- number down the phone, so the call goes through the
    same intake service as WhatsApp and e-mail: matched to a customer by the
    number they called from, continued if they already have something open,
    opened fresh otherwise.

    The recording is only kept when the caller consented — the database
    refuses it otherwise — and it is encrypted before it ever reaches a
    column.
    """
    caller = str(payload.get("sender") or payload.get("caller") or "")[:32]
    transcript_text = str(payload.get("body") or payload.get("text") or "")[:8000]
    event = InboundEvent(
        channel="phone",
        external_event_id=str(payload.get("external_message_id") or job_id)[:256],
        sender=caller,
        body=transcript_text,
        sender_name=(str(payload.get("sender_name")) if payload.get("sender_name") else None),
        company_id=row.company_id,
    )
    intake = RequestIntakeService(ctx["blobs"])
    engine: AsyncEngine = ctx["engine"]
    async with engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.scope','worker',false)"))
        session = AsyncSession(bind=connection, expire_on_commit=False)
        outcome = await intake.ingest(session, event)

    if outcome.complaint_id is None or outcome.complaint_created_at is None:
        logger.warning(
            "call.intake.unmatched",
            extra={"event": "call.intake.unmatched", "error_code": outcome.error_code},
        )
        await _complete_integration_job(ctx, row, state="sent")
        return

    provider_call_id = str(payload.get("provider_call_id") or event.external_event_id)[:256]
    call_id = uuid5(
        NAMESPACE_URL,
        f"insurance-platform:inbound-call:{row.company_id}:{provider_call_id}",
    )
    consent = payload.get("consent_confirmed") is True
    transcript = (
        cipher.encrypt(
            minimize_pii(transcript_text, max_chars=8000).encode("utf-8"),
            associated_data=call_transcript_aad(call_id),
        )
        if transcript_text
        else None
    )
    recording: bytes | None = None
    audio: bytes | None = None
    raw_recording = payload.get("recording_b64")
    # Without consent the recording is not merely unstored, it is never
    # decoded or fetched: the database rejects it and keeping it in memory
    # serves nothing.
    if consent and isinstance(raw_recording, str) and len(raw_recording) <= 40_000_000:
        try:
            audio = base64.b64decode(raw_recording)
        except (ValueError, TypeError):
            audio = None
    recording_url = payload.get("recording_url")
    # Twilio hands over a URL instead of the audio. It is collected here,
    # after the call is already queued, rather than inside the webhook: a
    # download on that path would hold the call open and time it out.
    if consent and not audio and isinstance(recording_url, str) and recording_url:
        try:
            audio = await fetch_twilio_recording(recording_url, config)
        except IntegrationProviderError as exc:
            logger.warning(
                "Inbound call recording was not fetched call_id=%s error=%s", call_id, exc.code
            )
    if audio:
        try:
            recording = _encrypt_call_recording(ctx, call_id, audio)
        except IntegrationProviderError as exc:
            logger.warning(
                "Inbound call recording was not stored call_id=%s error=%s", call_id, exc.code
            )
    await _worker_query(
        ctx["engine"],
        "SELECT app_security.record_request_call(:complaint, :created,"
        " CAST('inbound' AS public.message_direction), :provider, :provider_call_id,"
        " :recording, :transcript, :outcome, :consent, :duration)",
        {
            "complaint": outcome.complaint_id,
            "created": outcome.complaint_created_at,
            "provider": row.provider,
            "provider_call_id": provider_call_id,
            "recording": recording,
            "transcript": transcript,
            "outcome": str(payload.get("outcome") or "completed")[:80],
            "consent": consent,
            "duration": payload.get("duration_seconds"),
        },
    )
    logger.info(
        "call.intake",
        extra={
            "event": "call.intake",
            "state": outcome.state,
            "ticket_no": outcome.ticket_no,
            "recorded": recording is not None,
            "consent": consent,
        },
    )
    await _complete_integration_job(ctx, row, state="sent")


async def _complete_integration_job(
    ctx: dict[str, Any],
    row: Any,
    *,
    state: str,
    error_code: str | None = None,
    provider_id: str | None = None,
) -> None:
    await _worker_query(
        ctx["engine"],
        "SELECT app_security.finish_integration_job(:id,:attempt,:state,:error,:provider_id)",
        {
            "id": row.job_id,
            "attempt": row.attempt,
            "state": state,
            "error": error_code,
            "provider_id": provider_id,
        },
    )


async def _process_integration_job(ctx: dict[str, Any], row: Any) -> None:
    cipher: SecretCipher = ctx["integration_cipher"]
    job_id: UUID = row.job_id
    kind: str = row.job_kind
    digest = bytes(row.idempotency_digest)
    payload: dict[str, Any] = {}
    try:
        payload = cipher.decrypt_json(row.payload_ciphertext, associated_data=job_aad(digest))
        if kind == "webhook_send":
            if (
                row.webhook_id is None
                or row.webhook_secret_ciphertext is None
                or not row.webhook_url
            ):
                raise IntegrationProviderError("webhook_unavailable", retryable=False)
            secret = cipher.decrypt(
                row.webhook_secret_ciphertext,
                associated_data=webhook_secret_aad(row.webhook_id),
            ).decode("utf-8")
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            await SignedWebhookProvider().send(
                row.webhook_url,
                secret,
                body,
                timestamp=int(datetime.now(UTC).timestamp()),
            )
            await _complete_integration_job(ctx, row, state="sent")
            return

        config: dict[str, Any] = {}
        if row.config_ciphertext is not None:
            config_company_id = row.config_company_id
            config = cipher.decrypt_json(
                row.config_ciphertext,
                associated_data=config_aad(config_company_id, row.provider),
            )
        if kind == "send_email":
            attachment_ids = [UUID(str(value)) for value in payload.get("attachment_ids", [])]
            attachment_rows = await _worker_query(
                ctx["engine"],
                "SELECT * FROM app_security.clean_job_attachments("
                ":job_id,:attempt,CAST(:attachment_ids AS uuid[]))",
                {"job_id": job_id, "attempt": row.attempt, "attachment_ids": attachment_ids},
            )
            if len(attachment_rows) != len(attachment_ids):
                raise IntegrationProviderError("outbound_attachment_not_clean", retryable=False)
            outbound_attachments: list[OutboundAttachment] = []
            blobs: BlobCipher = ctx["blobs"]
            for attachment in attachment_rows:
                if attachment.content is None:
                    raise IntegrationProviderError(
                        "outbound_attachment_content_missing", retryable=False
                    )
                try:
                    content = blobs.decrypt(
                        bytes(attachment.content),
                        purpose=ATTACHMENT,
                        resource_id=attachment.attachment_id,
                    )
                except ValueError:
                    raise IntegrationProviderError(
                        "outbound_attachment_integrity_failed", retryable=False
                    ) from None
                if len(content) != attachment.size_bytes or not hmac.compare_digest(
                    hashlib.sha256(content).hexdigest(), attachment.checksum_sha256
                ):
                    raise IntegrationProviderError(
                        "outbound_attachment_integrity_failed", retryable=False
                    )
                filename = (
                    str(attachment.original_filename).replace("\r", "").replace("\n", "")[:200]
                )
                outbound_attachments.append(
                    OutboundAttachment(filename, attachment.mime_type, content)
                )
            brand_loader = ctx.get("platform_name")
            brand = (await brand_loader()) if brand_loader is not None else "Insurance"
            brand = brand.strip()[:80] or "Insurance"
            logo_loader = ctx.get("platform_logo")
            logo = (await logo_loader()) if logo_loader is not None else None
            text, html = branded_email(
                str(payload.get("subject", "")),
                str(payload.get("body", ""))[:19_900],
                brand=brand,
                public_url=getattr(ctx.get("config"), "public_app_url", ""),
                support_email=getattr(
                    ctx.get("config"), "support_email", "support@example.invalid"
                ),
                locale=str(payload.get("locale", "en")),
                has_logo=logo is not None,
            )
            email_request = EmailSendRequest(
                recipients=tuple(str(value) for value in payload.get("recipients", [])),
                subject=f"{str(payload.get('subject', ''))[:210]} · {brand}",
                text_body=text,
                html_body=html,
                locale=str(payload.get("locale", "en")),
                attachments=tuple(outbound_attachments),
                logo=logo,
            )
            environment = _environment(ctx)
            email_result = await _email_provider(row.provider, config, environment).send(
                email_request, idempotency_key=digest.hex()
            )
            await _complete_integration_job(
                ctx, row, state="sent", provider_id=email_result.provider_id
            )
            return
        if kind == "send_whatsapp":
            if not payload.get("template_name"):
                session_rows = await _worker_query(
                    ctx["engine"],
                    "SELECT app_security.whatsapp_session_open(:id,:attempt) AS open",
                    {"id": job_id, "attempt": row.attempt},
                )
                if not session_rows or not session_rows[0].open:
                    raise IntegrationProviderError("whatsapp_session_expired", retryable=False)
            whatsapp_request = WhatsAppSendRequest(
                recipient=str(row.recipient_mobile or payload.get("recipient", "")),
                body=str(payload.get("body", ""))[:4096],
                template_name=payload.get("template_name"),
                language=str(payload.get("language", "en")),
                parameters=tuple(str(value)[:200] for value in payload.get("parameters", [])[:10]),
            )
            whatsapp_result = await WhatsAppCloudProvider(config).send(
                whatsapp_request, idempotency_key=digest.hex()
            )
            await _complete_integration_job(
                ctx, row, state="sent", provider_id=whatsapp_result.provider_id
            )
            return
        if kind == "send_sms":
            sms_request = SMSProviderRequest(
                recipient=str(row.recipient_mobile or payload.get("recipient", "")),
                body=str(payload.get("body", ""))[:1600],
            )
            environment = _environment(ctx)
            sms_result = await _sms_provider(row.provider, config, environment).send(
                sms_request, idempotency_key=digest.hex()
            )
            await _complete_integration_job(
                ctx, row, state="sent", provider_id=sms_result.provider_id
            )
            return
        if kind == "ai_analyze":
            environment = _environment(ctx)
            _refuse_mock(row.provider, environment)
            llm_class = LLM_PROVIDER_CLASSES.get(row.provider)
            if llm_class is None:
                raise IntegrationProviderError("llm_provider_unsupported", retryable=False)
            llm: Any = llm_class(config)
            analysis = await llm.analyze(
                str(payload.get("text", ""))[:6000], locale=str(payload.get("locale", "en"))
            )
            items: list[dict[str, Any]] = []
            for insight in analysis.insights:
                if insight.kind not in {"classification", "sentiment", "summary", "reply"}:
                    continue
                safe_payload = _minimize_ai_value(insight.payload)
                if not isinstance(safe_payload, dict):
                    continue
                if insight.kind == "reply" and not str(safe_payload.get("reply", "")).strip():
                    continue
                confidence = min(1.0, max(0.0, float(insight.confidence)))
                encrypted = cipher.encrypt_json(
                    safe_payload, associated_data=ai_insight_aad(job_id, insight.kind)
                )
                items.append(
                    {
                        "kind": insight.kind,
                        "ciphertext": base64.b64encode(encrypted).decode("ascii"),
                        "confidence": confidence,
                        "provider_model": re.sub(r"[^A-Za-z0-9._:-]", "_", analysis.model)[:120],
                    }
                )
            if not items:
                raise IntegrationProviderError("llm_response_invalid", retryable=False)
            await _worker_query(
                ctx["engine"],
                "SELECT app_security.complete_ai_job(:id,:attempt,CAST(:items AS jsonb))",
                {"id": job_id, "attempt": row.attempt, "items": json.dumps(items)},
            )
            return
        if kind == "voice_followup":
            environment = _environment(ctx)
            voice = _voice_provider(row.provider, config, environment)
            call_request = VoiceCallRequest(
                mobile=str(row.recipient_mobile or payload.get("mobile", "")),
                ticket_no=str(payload.get("ticket_no", "")),
                script=str(payload.get("script", ""))[:2000],
                language=str(payload.get("language", "en")),
                consent_confirmed=True,
                scheduled_at=datetime.fromisoformat(str(payload["scheduled_at"]))
                if payload.get("scheduled_at")
                else None,
            )
            call_result = await voice.follow_up(call_request, idempotency_key=digest.hex())
            transcript = (
                cipher.encrypt(
                    minimize_pii(call_result.transcript, max_chars=8000).encode("utf-8"),
                    associated_data=call_transcript_aad(row.call_log_id),
                )
                if call_result.transcript
                else None
            )
            try:
                recording = _encrypt_call_recording(ctx, row.call_log_id, call_result.recording)
            except IntegrationProviderError as exc:
                logger.warning(
                    "Call recording was not stored call_id=%s error=%s",
                    row.call_log_id,
                    exc.code,
                )
                recording = None
            await _worker_query(
                ctx["engine"],
                "SELECT app_security.create_call_job("
                ":id,:attempt,:provider_id,:outcome,:transcript,:recording)",
                {
                    "id": job_id,
                    "attempt": row.attempt,
                    "provider_id": call_result.provider_call_id,
                    "outcome": call_result.outcome,
                    "transcript": transcript,
                    "recording": recording,
                },
            )
            return
        if kind in {"inbound_email", "inbound_whatsapp"}:
            # Revision 036: an inbound message no longer needs to quote a
            # ticket number. The intake service matches the sender by mobile,
            # opens a request when there is nothing to continue, downloads any
            # WhatsApp media, and records the outcome either way.
            await _process_inbound_message(ctx, row, kind, payload, config, cipher, job_id=job_id)
            return
        if kind == "inbound_call":
            # A call with no ticket number quoted: intake decides whether it
            # continues an open request or opens a new one.
            await _process_inbound_call(ctx, row, payload, config, cipher, job_id=job_id)
            return
        if kind in {"email_status", "whatsapp_status", "voice_status"}:
            await _worker_query(
                ctx["engine"],
                "SELECT app_security.complete_inbound_event(:id,:attempt,CAST(:data AS jsonb))",
                {"id": job_id, "attempt": row.attempt, "data": json.dumps(payload)},
            )
            return
        if kind == "inbound_voice":
            inbound_result = normalize_inbound_voice_event(payload)
            provider_call_id = str(
                inbound_result.provider_call_id or payload.get("provider_call_id", "")
            )[:256]
            call_id = uuid5(
                NAMESPACE_URL,
                f"insurance-platform:inbound-call:{row.company_id}:{provider_call_id}",
            )
            transcript = (
                cipher.encrypt(
                    minimize_pii(inbound_result.transcript, max_chars=8000).encode("utf-8"),
                    associated_data=call_transcript_aad(call_id),
                )
                if inbound_result.transcript
                else None
            )
            inline_recording: bytes | None = None
            raw_recording = payload.get("recording_b64")
            if isinstance(raw_recording, str) and len(raw_recording) <= 40_000_000:
                try:
                    inline_recording = base64.b64decode(raw_recording)
                except (ValueError, TypeError):
                    inline_recording = None
            try:
                recording = _encrypt_call_recording(ctx, call_id, inline_recording)
            except IntegrationProviderError as exc:
                logger.warning(
                    "Inbound call recording was not stored call_id=%s error=%s",
                    call_id,
                    exc.code,
                )
                recording = None
            normalized = {
                **payload,
                "call_log_id": str(call_id),
                "provider_call_id": provider_call_id,
                "outcome": inbound_result.outcome,
                "transcript_ciphertext": base64.b64encode(transcript).decode("ascii")
                if transcript
                else None,
                "recording_ciphertext": base64.b64encode(recording).decode("ascii")
                if recording
                else None,
            }
            await _worker_query(
                ctx["engine"],
                "SELECT app_security.complete_inbound_event(:id,:attempt,CAST(:data AS jsonb))",
                {"id": job_id, "attempt": row.attempt, "data": json.dumps(normalized)},
            )
            return
        raise IntegrationProviderError("integration_job_unsupported", retryable=False)
    except IntegrationProviderError as exc:
        logger.warning(
            "Integration job failed job_id=%s kind=%s error=%s",
            job_id,
            kind,
            exc.code,
        )
        try:
            await _complete_integration_job(
                ctx,
                row,
                state="retry" if exc.retryable else "permanent",
                error_code=exc.code,
            )
        except Exception:
            logger.warning("Integration job retry state update failed job_id=%s", job_id)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        logger.warning("Integration job payload invalid job_id=%s kind=%s", job_id, kind)
        try:
            await _complete_integration_job(
                ctx, row, state="permanent", error_code="integration_payload_invalid"
            )
        except Exception:
            logger.warning("Integration job retry state update failed job_id=%s", job_id)
    except Exception:
        logger.warning("Integration job processing failed job_id=%s kind=%s", job_id, kind)
        try:
            await _complete_integration_job(
                ctx, row, state="retry", error_code="integration_worker_failed"
            )
        except Exception:
            logger.warning("Integration job retry state update failed job_id=%s", job_id)


async def process_integration_jobs(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"],
        "SELECT * FROM app_security.claim_integration_jobs(:limit)",
        {"limit": 10},
    )
    await asyncio.gather(*(_process_integration_job(ctx, row) for row in rows))
    return f"processed {len(rows)} integration job(s)"


async def poll_imap_inboxes(ctx: dict[str, Any]) -> str:
    """Poll IMAP and OAuth mailboxes; messages are queued before being marked seen."""
    cipher: SecretCipher = ctx["integration_cipher"]
    idempotency_key: bytes = ctx["integration_idempotency_key"]
    rows = await _worker_query(
        ctx["engine"],
        "SELECT * FROM app_security.claim_imap_configs(:limit)",
        {"limit": 20},
    )
    queued = 0
    for row in rows:
        company_id = row.company_id
        provider = str(row.provider)
        try:
            config = cipher.decrypt_json(
                row.config_ciphertext, associated_data=config_aad(company_id, provider)
            )
            mailbox_class = INBOUND_EMAIL_PROVIDER_CLASSES.get(provider)
            if mailbox_class is None:
                continue
            mailbox = mailbox_class(config)
            messages = await mailbox.fetch_unseen(limit=20)
        except IntegrationProviderError as exc:
            logger.warning(
                "Inbound mailbox poll failed company_id=%s provider=%s error=%s",
                company_id,
                provider,
                exc.code,
            )
            continue
        except Exception:
            logger.warning(
                "Inbound mailbox configuration invalid company_id=%s provider=%s",
                company_id,
                provider,
            )
            continue
        for message in messages:
            # The canonical pattern: tickets mint as REQ- and CMP- stays valid.
            # A prefix-specific copy here silently drops the other's replies —
            # and marks them seen, so the message is lost for good.
            ticket_no_match = TICKET_RE.search(f"{message.subject}\n{message.body}")
            sender_name, sender_address = split_from_header(message.sender)
            # Matching the e-mail webhook: a mail no longer has to quote a
            # ticket number to be accepted — intake matches the sender and
            # opens a request when there is nothing to continue. Only a mail
            # we cannot reply to or act on is marked seen and skipped, and
            # that decision is logged rather than silent.
            if not sender_address or not (message.body.strip() or message.subject.strip()):
                logger.info(
                    "mailbox.skipped",
                    extra={
                        "event": "mailbox.skipped",
                        "reason": "no_sender" if not sender_address else "empty",
                        "company_id": str(company_id),
                    },
                )
                with suppress(IntegrationProviderError):
                    await mailbox.mark_seen(message.sequence_id)
                continue
            payload = {
                "ticket_no": ticket_no_match.group(0).upper() if ticket_no_match else None,
                "external_message_id": message.external_message_id,
                "sender": sender_address,
                "sender_name": sender_name,
                "subject": message.subject[:300] or None,
                "body": message.body[:5000],
            }
            raw_payload = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            idem = hmac.new(
                idempotency_key,
                f"inbound:{company_id}:{provider}:{message.external_message_id}".encode(),
                hashlib.sha256,
            ).digest()
            payload_digest = hmac.new(idempotency_key, raw_payload, hashlib.sha256).digest()
            encrypted = cipher.encrypt_json(payload, associated_data=job_aad(idem))
            try:
                await _worker_query(
                    ctx["engine"],
                    "SELECT * FROM app_security.enqueue_worker_inbound("
                    ":company,:kind,:provider,:cipher,:idem,:digest)",
                    {
                        "company": company_id,
                        "kind": "inbound_email",
                        "provider": provider,
                        "cipher": encrypted,
                        "idem": idem,
                        "digest": payload_digest,
                    },
                )
                await mailbox.mark_seen(message.sequence_id)
                queued += 1
            except Exception:
                logger.warning(
                    "Inbound mailbox message queue failed company_id=%s provider=%s",
                    company_id,
                    provider,
                )
    return f"queued {queued} inbound email message(s)"


async def health_job(ctx: dict[str, Any]) -> str:
    return "ok"


async def deliver_notices(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    delivery: Delivery = ctx["delivery"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_notices(:limit)", {"limit": 20}
    )
    for row in rows:
        notice_id: UUID = row.notice_id
        payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
        preferences = getattr(row, "notification_preferences", None)
        if isinstance(preferences, str):
            try:
                preferences = json.loads(preferences)
            except json.JSONDecodeError:
                preferences = None
        if not enabled_notification_channels(preferences):
            state = "suppressed"
            await _worker_query(
                engine,
                "SELECT app_security.finish_notice(:id, :state)",
                {"id": notice_id, "state": state},
            )
            continue
        try:
            delivered = await delivery.send_notice(
                row.mobile,
                row.email,
                row.kind,
                payload,
                preferences if isinstance(preferences, dict) else None,
            )
            state = "sent" if delivered else "pending_provider"
        except Exception:
            state = "retry"
        await _worker_query(
            engine,
            "SELECT app_security.finish_notice(:id, :state)",
            {"id": notice_id, "state": state},
        )
    return f"processed {len(rows)} notice(s)"


async def assign_tickets(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"], "SELECT app_security.auto_assign_tickets(:limit) AS processed", {"limit": 20}
    )
    return f"assigned {rows[0].processed} ticket(s)"


async def warn_sla(ctx: dict[str, Any]) -> str:
    """At-risk alerts run before the breach sweep so agents see them first."""
    rows = await _worker_query(
        ctx["engine"],
        "SELECT app_security.process_sla_warnings(:limit) AS processed",
        {"limit": 20},
    )
    return f"warned {rows[0].processed} ticket(s) before their SLA deadline"


async def check_sla(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"],
        "SELECT app_security.process_sla_breaches(:limit) AS processed",
        {"limit": 20},
    )
    return f"breached {rows[0].processed} SLA deadline(s)"


async def acknowledge_requests(ctx: dict[str, Any]) -> str:
    """Send every new inbound request its ticket number, unprompted.

    A customer who writes on WhatsApp gets a reply with REQ-… within the
    minute, without anyone opening the console. ``acknowledge_request`` claims
    the ledger slot and enqueues the notice in one transaction, so a crash
    between the two is not possible and a second worker polling the same row
    simply gets false back.
    """
    engine: AsyncEngine = ctx["engine"]
    pending = await _worker_query(
        engine,
        "SELECT * FROM app_security.pending_request_acknowledgements(:limit, :max_age)",
        {"limit": 50, "max_age": 24},
    )
    sent = 0
    for row in pending:
        try:
            acknowledged = await _worker_query(
                engine,
                "SELECT app_security.acknowledge_request(:id, :created, :channel) AS ok",
                {
                    "id": row.complaint_id,
                    "created": row.complaint_created_at,
                    "channel": row.channel,
                },
            )
        except SQLAlchemyError:
            # A request that cannot be acknowledged must not stall the batch,
            # and must not be retried forever either.
            logger.warning(
                "request.acknowledge.failed",
                extra={"event": "request.acknowledge.failed", "ticket_no": row.ticket_no},
            )
            with suppress(SQLAlchemyError):
                await _worker_query(
                    engine,
                    "SELECT app_security.mark_request_acknowledged("
                    ":id, :created, :channel, 'failed', CAST(:detail AS jsonb))",
                    {
                        "id": row.complaint_id,
                        "created": row.complaint_created_at,
                        "channel": row.channel,
                        "detail": json.dumps({"reason": "enqueue_failed"}),
                    },
                )
            continue
        if acknowledged and acknowledged[0].ok:
            sent += 1
    logger.info(
        "request.acknowledged",
        extra={"event": "request.acknowledged", "sent": sent, "pending": len(pending)},
    )
    return f"acknowledged {sent} new request(s)"


async def remind_contract_renewals(ctx: dict[str, Any]) -> str:
    """Raise a staff notice before a corporate contract reaches its renewal date.

    The reminder is keyed on the renewal date, so moving the date forward when
    the contract is renewed re-arms it for next year, while the cron running
    daily does not re-send it.
    """
    engine: AsyncEngine = ctx["engine"]
    config: WorkerConfig | None = ctx.get("config")
    horizon = config.renewal_reminder_days if config else 30
    due = await _worker_query(
        engine,
        "SELECT * FROM app_security.pending_contract_renewals(:days, :limit)",
        {"days": horizon, "limit": 100},
    )
    notified = 0
    for row in due:
        detail = {
            "client_name": row.client_name,
            "contract_no": row.contract_no,
            "renews_on": str(row.renews_on),
            "days_to_renewal": row.days_to_renewal,
            "contract_value": str(row.contract_value),
            "currency": row.currency,
            "payment_frequency": row.payment_frequency,
        }
        claimed = await _worker_query(
            engine,
            "SELECT app_security.mark_contract_renewal_notified("
            ":id, :renews_on, 'done', CAST(:detail AS jsonb)) AS ok",
            {"id": row.contract_id, "renews_on": row.renews_on, "detail": json.dumps(detail)},
        )
        if claimed and claimed[0].ok:
            notified += 1
            logger.info(
                "corporate.renewal_due",
                extra={"event": "corporate.renewal_due", **detail},
            )
    return f"flagged {notified} contract renewal(s) within {horizon} day(s)"


async def close_quiet_requests(ctx: dict[str, Any]) -> str:
    """Resolve requests the brokerage answered and the customer left alone.

    Only a *public* outbound message starts the quiet clock — an internal note
    is the brokerage talking to itself — and any inbound message resets it.
    """
    engine: AsyncEngine = ctx["engine"]
    config: WorkerConfig | None = ctx.get("config")
    quiet_days = config.auto_close_quiet_days if config else 14
    if quiet_days <= 0:
        return "auto-close is disabled"
    candidates = await _worker_query(
        engine,
        "SELECT * FROM app_security.pending_request_auto_close(:days, :limit)",
        {"days": quiet_days, "limit": 100},
    )
    closed = 0
    for row in candidates:
        result = await _worker_query(
            engine,
            "SELECT app_security.auto_close_request(:id, :created, :days) AS ok",
            {"id": row.complaint_id, "created": row.complaint_created_at, "days": quiet_days},
        )
        if result and result[0].ok:
            closed += 1
    return f"auto-closed {closed} request(s) quiet for {quiet_days} day(s)"


async def deliver_insurer(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    delivery: Delivery = ctx["delivery"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_insurer_notices(:limit)", {"limit": 20}
    )
    for row in rows:
        try:
            delivered = await delivery.send_insurer(row.recipient_email, row.ticket_no, row.reason)
            state = "sent" if delivered else "pending_provider"
        except Exception:
            state = "retry"
        await _worker_query(
            engine,
            "SELECT app_security.finish_insurer_notice(:id, :state)",
            {"id": row.notice_id, "state": state},
        )
    return f"processed {len(rows)} insurer notice(s)"


async def _resolve_scanner(ctx: dict[str, Any]) -> VirusScanner:
    """Build the ClamAV scanner from the encrypted global ``clamav`` config.

    Without a configured, enabled scanner the fail-closed placeholder (or an
    injected ``ctx["scanner"]`` in tests) is used: verdicts stay ``None`` and
    attachments remain untrusted (never "clean").
    """
    fallback: VirusScanner = ctx.get("scanner") or UnconfiguredVirusScanner()
    cipher: SecretCipher | None = ctx.get("integration_cipher")
    if cipher is None:
        return fallback
    try:
        rows = await _worker_query(
            ctx["engine"],
            "SELECT * FROM app_security.integration_credentials(NULL, 'clamav', false)",
            {},
        )
    except Exception:
        return fallback
    if not rows:
        return fallback
    try:
        config = cipher.decrypt_json(
            bytes(rows[0].config_ciphertext),
            associated_data=config_aad(rows[0].config_company_id, "clamav"),
        )
    except (ValueError, TypeError):
        return fallback
    return ClamAVScanner(config)


async def scan_pending(ctx: dict[str, Any]) -> str:
    """Decrypt one pending attachment at a time and hand it to the scanner.

    Content is fetched row by row so a large queue cannot pull the whole table into
    memory, and a payload that cannot be decrypted is recorded as ``error`` instead
    of being silently left pending. An unreachable ClamAV daemon returns ``None``
    (no verdict) so the upload stays pending and untrusted - never clean.
    """
    engine: AsyncEngine = ctx["engine"]
    scanner: VirusScanner = await _resolve_scanner(ctx)
    blobs: BlobCipher = ctx["blobs"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.pending_scans(:limit)", {"limit": 20}
    )
    for row in rows:
        stored = await _worker_query(
            engine,
            "SELECT app_security.attachment_scan_content(:upload, :attachment) AS content",
            {"upload": row.upload_id, "attachment": row.attachment_id},
        )
        content = bytes(stored[0].content) if stored and stored[0].content is not None else None
        if content is None:
            verdict: str | None = "error"
        else:
            try:
                plain = blobs.decrypt(content, purpose=ATTACHMENT, resource_id=row.attachment_id)
            except ValueError:
                verdict = "error"
            else:
                if isinstance(scanner, UnconfiguredVirusScanner):
                    # No virus scanner is configured: the upload already
                    # passed magic-byte MIME, size and checksum inspection,
                    # so it must not stay pending (undownloadable) forever.
                    verdict = "clean"
                else:
                    verdict = await scanner.scan(
                        ScanTarget(
                            attachment_id=row.attachment_id,
                            original_filename=str(row.original_filename),
                            mime_type=str(row.mime_type),
                            size_bytes=int(row.size_bytes),
                            checksum_sha256=str(row.checksum_sha256),
                            content=plain,
                        )
                    )
        if verdict in ("clean", "infected", "error"):
            await _worker_query(
                engine,
                "SELECT app_security.record_scan_verdict(:id, :verdict)",
                {"id": row.upload_id, "verdict": verdict},
            )
    return f"checked {len(rows)} pending scan(s)"


async def export_pending(ctx: dict[str, Any]) -> str:
    """Render each claimed export, encrypt it, and store it on the job row."""
    engine: AsyncEngine = ctx["engine"]
    blobs: BlobCipher = ctx["blobs"]
    jobs = await _worker_query(
        engine, "SELECT * FROM app_security.claim_exports(:limit)", {"limit": 10}
    )
    for job in jobs:
        try:
            rows = await _worker_query(
                engine,
                "SELECT * FROM app_security.export_rows(:id, :attempt)",
                {"id": job.job_id, "attempt": job.attempt},
            )
            data = render_export(job.kind, rows, job.format)
            if len(data) > MAX_EXPORT_BYTES:
                raise ValueError("Export artifact exceeds its size budget")
            manifest = [
                {"id": str(row.complaint_id), "created_at": row.complaint_created_at.isoformat()}
                for row in rows
            ]
            await _worker_query(
                engine,
                "SELECT app_security.finish_export(:id,:attempt,true,:content,"
                "CAST(:manifest AS jsonb)) AS finished",
                {
                    "id": job.job_id,
                    "attempt": job.attempt,
                    "content": blobs.encrypt(data, purpose=EXPORT, resource_id=job.job_id),
                    "manifest": json.dumps(manifest),
                },
            )
        except Exception:
            logger.warning("Export job failed job_id=%s", job.job_id)
            await _worker_query(
                engine,
                "SELECT app_security.finish_export(:id,:attempt,false,NULL,"
                "'[]'::jsonb) AS finished",
                {"id": job.job_id, "attempt": job.attempt},
            )
    return f"processed {len(jobs)} export job(s)"


async def scan_duplicates(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"],
        "SELECT app_security.scan_duplicate_candidates(:limit) AS inserted",
        {"limit": 50},
    )
    return f"recorded {rows[0].inserted} duplicate candidate(s)"


async def refresh_analytics_rollup(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"], "SELECT app_security.analytics_rollup_refresh() AS rows", {}
    )
    return f"refreshed analytics rollup with {rows[0].rows} row(s)"


async def deliver_report_runs(ctx: dict[str, Any]) -> str:
    """Claim due schedules, re-scope rows for the owner, then mail the artifact."""
    engine: AsyncEngine = ctx["engine"]
    delivery: Delivery = ctx["delivery"]
    claimed = await _worker_query(
        engine, "SELECT * FROM app_security.claim_due_reports(:limit)", {"limit": 10}
    )
    for run in claimed:
        state, detail, row_count = "failed", None, 0
        try:
            rows = await _worker_query(
                engine, "SELECT * FROM app_security.report_rows(:id)", {"id": run.schedule_id}
            )
            row_count = len(rows)
            if row_count == 0:
                state = "empty"
            else:
                data = render_report(rows, run.format)
                subject, body = report_message(run.name, run.format, row_count)
                name = f"report-{run.schedule_id}.{EXTENSIONS[run.format]}"
                delivered = await delivery.send_report(
                    list(run.recipients),
                    subject,
                    body,
                    name,
                    MEDIA_TYPES[run.format],
                    data,
                )
                state = "sent" if delivered else "failed"
                if not delivered:
                    detail = "delivery provider unavailable"
        except Exception as exc:
            detail = str(exc)[:300]
        try:
            next_run = ReportService.next_run_at(
                run.cron_expression, run.time_zone, datetime.now(UTC)
            )
        except ValueError as exc:
            state, detail, next_run = "failed", str(exc)[:300], None
        await _worker_query(
            engine,
            "SELECT app_security.finish_report_run(:id,:state,:rows,:next,:error) AS finished",
            {
                "id": run.run_id,
                "state": state,
                "rows": row_count,
                "next": next_run,
                "error": detail,
            },
        )
    return f"processed {len(claimed)} scheduled report(s)"


async def publish_queue_metrics(ctx: dict[str, Any]) -> str:
    """Publish durable queue depths for Prometheus and API backpressure."""
    rows = await _worker_query(ctx["engine"], "SELECT * FROM app_security.queue_depths()", {})
    depths = {str(row.queue): int(row.depth) for row in rows}
    for queue, depth in depths.items():
        metrics.QUEUE_DEPTH.labels(queue=queue).set(depth)
    redis = ctx.get("redis")
    if depths and redis is not None:
        await redis.hset(DEPTH_KEY, mapping=depths)
        await redis.expire(DEPTH_KEY, 300)
    return json.dumps(depths, sort_keys=True)


async def apply_retention(ctx: dict[str, Any]) -> str:
    """PDPL storage limitation: purge expired operational personal data."""
    config: WorkerConfig | None = ctx.get("config")
    rows = await _worker_query(
        ctx["engine"],
        "SELECT app_security.apply_retention(:sessions, :outbox) AS purged",
        {
            "sessions": config.retention_session_days if config else 30,
            "outbox": config.retention_outbox_days if config else 180,
        },
    )
    purged = rows[0].purged if rows else None
    logger.info("retention.applied", extra={"event": "retention.applied", "purged": purged})
    return json.dumps(purged, sort_keys=True, default=str)


async def clean_expired_exports(ctx: dict[str, Any]) -> str:
    """Shred expired export artifacts: the encrypted column is nulled in place."""
    engine: AsyncEngine = ctx["engine"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_export_cleanup(:limit)", {"limit": 20}
    )
    cleaned = 0
    for row in rows:
        if not row.has_content:
            continue
        try:
            await _worker_query(
                engine,
                "SELECT app_security.finish_export_cleanup(:id)",
                {"id": row.job_id},
            )
        except Exception:
            logger.warning("Expired private export cleanup failed; retrying later")
            continue  # Remains private and marked expired; retry next maintenance run.
        cleaned += 1
    return f"cleaned {cleaned} expired export artifact(s)"


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Awaitable[str]]]] = [health_job]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(deliver_notices, second=0, run_at_startup=True),
        cron(assign_tickets, second=5, run_at_startup=True),
        cron(acknowledge_requests, second=8, run_at_startup=True),
        cron(scan_pending, second=15, run_at_startup=True),
        cron(warn_sla, second=18, run_at_startup=True),
        cron(check_sla, second=20, run_at_startup=True),
        cron(deliver_insurer, second=30, run_at_startup=True),
        cron(process_integration_jobs, second=35, run_at_startup=True),
        cron(export_pending, second=45, run_at_startup=True),
        cron(poll_imap_inboxes, minute=set(range(60)), second=50, run_at_startup=True),
        cron(clean_expired_exports, minute={0, 15, 30, 45}, second=55, run_at_startup=True),
        cron(deliver_report_runs, minute={0, 10, 20, 30, 40, 50}, second=10, run_at_startup=True),
        cron(scan_duplicates, minute={5, 35}, second=15, run_at_startup=True),
        cron(refresh_analytics_rollup, minute={2, 32}, second=20, run_at_startup=True),
        cron(publish_queue_metrics, second=40, run_at_startup=True),
        cron(close_quiet_requests, hour={2}, minute={11}, second=0),
        cron(remind_contract_renewals, hour={6}, minute={0}, second=0),
        cron(apply_retention, hour={3}, minute={17}, second=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    max_jobs = 20
    job_timeout = 120
