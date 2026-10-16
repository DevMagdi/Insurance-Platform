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
from io import BytesIO
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid5

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from pydantic import Field
from pydantic_settings import BaseSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import make_worker_engine
from app.observability import metrics
from app.observability.logging import configure_logging
from app.observability.sentry import init_sentry
from app.security.crypto import build_keyring, idempotency_root
from app.security.integration_crypto import SecretCipher, derive_idempotency_key
from app.services.backpressure import DEPTH_KEY
from app.services.exports import render_export
from app.services.integration_providers import (
    EmailProvider,
    EmailSendRequest,
    IMAPMailProvider,
    IntegrationProviderError,
    MockLLMProvider,
    MockVoiceProvider,
    OpenAICompatibleProvider,
    OutboundAttachment,
    SendGridEmailProvider,
    SESEmailProvider,
    SignedWebhookProvider,
    SMTPEmailProvider,
    TwilioVoiceProvider,
    VoiceCallRequest,
    VoiceProvider,
    VonageVoiceProvider,
    WhatsAppCloudProvider,
    WhatsAppSendRequest,
)
from app.services.integrations import (
    ai_insight_aad,
    call_recording_aad,
    call_transcript_aad,
    config_aad,
    job_aad,
    minimize_pii,
    webhook_secret_aad,
)
from app.services.providers import Delivery, SMTPDelivery, UnconfiguredVirusScanner, VirusScanner
from app.services.render import EXTENSIONS, MEDIA_TYPES
from app.services.reports import ReportService, render_report, report_message
from app.services.storage import ObjectStorage, S3ObjectStorage

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
    redis_url: str = "redis://redis:6379/0"
    s3_bucket: str = "complaint-attachments"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "us-east-1"
    smtp_host: str | None = None
    smtp_port: int = 1025
    smtp_from: str = "no-reply@insurance.local"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_start_tls: bool = False
    dev_sms_mailbox: bool = False
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
    ctx["delivery"] = SMTPDelivery(config)
    ctx["scanner"] = UnconfiguredVirusScanner()
    ctx["storage"] = (
        S3ObjectStorage(config)
        if config.s3_endpoint_url or (config.s3_access_key_id and config.s3_secret_access_key)
        else None
    )
    keyring = build_keyring(
        config.app_encryption_key,
        config.app_encryption_key_id,
        config.app_encryption_previous_keys,
    )
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


def _email_provider(provider: str, config: dict[str, Any]) -> EmailProvider:
    if provider == "smtp":
        return SMTPEmailProvider(config)
    if provider == "sendgrid":
        return SendGridEmailProvider(config)
    if provider == "ses":
        return SESEmailProvider(config)
    raise IntegrationProviderError("email_provider_unsupported", retryable=False)


def _voice_provider(provider: str, config: dict[str, Any]) -> VoiceProvider:
    if provider == "mock_voice":
        return MockVoiceProvider()
    if provider == "twilio":
        return TwilioVoiceProvider(config)
    if provider == "vonage":
        return VonageVoiceProvider(config)
    raise IntegrationProviderError("voice_provider_unsupported", retryable=False)


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


async def _store_call_recording(
    ctx: dict[str, Any], call_id: UUID, recording: bytes | None
) -> str | None:
    if recording is None:
        return None
    if len(recording) > 25 * 1024 * 1024:
        raise IntegrationProviderError("voice_recording_too_large", retryable=False)
    storage: ObjectStorage | None = ctx.get("storage")
    if storage is None:
        raise IntegrationProviderError("voice_recording_storage_unavailable")
    encrypted = ctx["integration_cipher"].encrypt(
        recording, associated_data=call_recording_aad(call_id)
    )
    key = f"call-recordings/{call_id}/recording.gcm"
    await storage.put(key, BytesIO(encrypted), "application/octet-stream")
    return key


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
            storage: ObjectStorage | None = ctx.get("storage")
            for attachment in attachment_rows:
                if storage is None:
                    raise IntegrationProviderError("attachment_storage_unavailable")
                content = await storage.get(attachment.storage_key, int(attachment.size_bytes))
                if len(content) != attachment.size_bytes or not hmac.compare_digest(
                    hashlib.sha256(content).hexdigest(), attachment.checksum_sha256
                ):
                    raise IntegrationProviderError(
                        "outbound_attachment_integrity_failed", retryable=False
                    )
                filename = str(attachment.filename).replace("\r", "").replace("\n", "")[:200]
                outbound_attachments.append(
                    OutboundAttachment(filename, attachment.mime_type, content)
                )
            email_request = EmailSendRequest(
                recipients=tuple(str(value) for value in payload.get("recipients", [])),
                subject=str(payload.get("subject", ""))[:300],
                text_body=str(payload.get("body", ""))[:20_000],
                locale=str(payload.get("locale", "en")),
                attachments=tuple(outbound_attachments),
            )
            email_result = await _email_provider(row.provider, config).send(
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
        if kind == "ai_analyze":
            if row.provider == "mock_llm":
                llm: Any = MockLLMProvider()
            elif row.provider == "openai":
                llm = OpenAICompatibleProvider(config)
            else:
                raise IntegrationProviderError("llm_provider_unsupported", retryable=False)
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
            voice = _voice_provider(row.provider, config)
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
                recording_key = await _store_call_recording(
                    ctx, row.call_log_id, call_result.recording
                )
            except IntegrationProviderError as exc:
                logger.warning(
                    "Call recording was not stored call_id=%s error=%s",
                    row.call_log_id,
                    exc.code,
                )
                recording_key = None
            await _worker_query(
                ctx["engine"],
                "SELECT app_security.create_call_job("
                ":id,:attempt,:provider_id,:outcome,:transcript,:recording_key)",
                {
                    "id": job_id,
                    "attempt": row.attempt,
                    "provider_id": call_result.provider_call_id,
                    "outcome": call_result.outcome,
                    "transcript": transcript,
                    "recording_key": recording_key,
                },
            )
            return
        if kind in {
            "inbound_email",
            "inbound_whatsapp",
            "email_status",
            "whatsapp_status",
            "voice_status",
        }:
            await _worker_query(
                ctx["engine"],
                "SELECT app_security.complete_inbound_event(:id,:attempt,CAST(:data AS jsonb))",
                {"id": job_id, "attempt": row.attempt, "data": json.dumps(payload)},
            )
            return
        if kind == "inbound_voice":
            inbound_result = await MockVoiceProvider().answer_inbound(payload)
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
            try:
                recording_key = await _store_call_recording(ctx, call_id, inbound_result.recording)
            except IntegrationProviderError as exc:
                logger.warning(
                    "Inbound call recording was not stored call_id=%s error=%s",
                    call_id,
                    exc.code,
                )
                recording_key = None
            normalized = {
                **payload,
                "call_log_id": str(call_id),
                "provider_call_id": provider_call_id,
                "outcome": inbound_result.outcome,
                "transcript_ciphertext": base64.b64encode(transcript).decode("ascii")
                if transcript
                else None,
                "recording_storage_key": recording_key,
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
        try:
            config = cipher.decrypt_json(
                row.config_ciphertext, associated_data=config_aad(company_id, "imap")
            )
            mailbox = IMAPMailProvider(config)
            messages = await mailbox.fetch_unseen(limit=20)
        except IntegrationProviderError as exc:
            logger.warning("IMAP poll failed company_id=%s error=%s", company_id, exc.code)
            continue
        except Exception:
            logger.warning("IMAP configuration invalid company_id=%s", company_id)
            continue
        for message in messages:
            ticket_no_match = re.search(
                r"\bCMP-\d{4}-\d{6,}\b", f"{message.subject}\n{message.body}", re.IGNORECASE
            )
            if not ticket_no_match or not message.body.strip():
                with suppress(IntegrationProviderError):
                    await mailbox.mark_seen(message.sequence_id)
                continue
            ticket_no = ticket_no_match.group(0).upper()
            payload = {
                "ticket_no": ticket_no,
                "external_message_id": message.external_message_id,
                "sender": message.sender,
                "body": message.body[:5000],
            }
            raw_payload = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            idem = hmac.new(
                idempotency_key,
                f"inbound:{company_id}:imap:{message.external_message_id}".encode(),
                hashlib.sha256,
            ).digest()
            payload_digest = hmac.new(idempotency_key, raw_payload, hashlib.sha256).digest()
            encrypted = cipher.encrypt_json(payload, associated_data=job_aad(idem))
            try:
                await _worker_query(
                    ctx["engine"],
                    "SELECT * FROM app_security.enqueue_worker_inbound("
                    ":company,:kind,:cipher,:idem,:digest)",
                    {
                        "company": company_id,
                        "kind": "inbound_email",
                        "cipher": encrypted,
                        "idem": idem,
                        "digest": payload_digest,
                    },
                )
                await mailbox.mark_seen(message.sequence_id)
                queued += 1
            except Exception:
                logger.warning("IMAP message queue failed company_id=%s", company_id)
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


async def scan_pending(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    scanner: VirusScanner = ctx["scanner"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.pending_scans(:limit)", {"limit": 20}
    )
    for row in rows:
        verdict = await scanner.scan(row.storage_key)
        if verdict in ("clean", "infected", "error"):
            await _worker_query(
                engine,
                "SELECT app_security.record_scan_verdict(:id, :verdict)",
                {"id": row.upload_id, "verdict": verdict},
            )
    return f"checked {len(rows)} pending scan(s)"


async def export_pending(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    storage: ObjectStorage | None = ctx.get("storage")
    if storage is None:
        return "export storage unavailable; jobs remain queued"
    jobs = await _worker_query(
        engine, "SELECT * FROM app_security.claim_exports(:limit)", {"limit": 10}
    )
    for job in jobs:
        media_type = MEDIA_TYPES.get(job.format, MEDIA_TYPES["csv"])
        key = f"exports/{job.job_id}/{job.attempt}.{EXTENSIONS.get(job.format, 'csv')}"
        uploaded = False
        try:
            rows = await _worker_query(
                engine,
                "SELECT * FROM app_security.export_rows(:id, :attempt)",
                {"id": job.job_id, "attempt": job.attempt},
            )
            data = render_export(job.kind, rows, job.format)
            manifest = [
                {"id": str(row.complaint_id), "created_at": row.complaint_created_at.isoformat()}
                for row in rows
            ]
            await storage.put(key, BytesIO(data), media_type)
            uploaded = True
            finished = await _worker_query(
                engine,
                "SELECT app_security.finish_export(:id,:attempt,true,:key,"
                "CAST(:manifest AS jsonb)) AS finished",
                {
                    "id": job.job_id,
                    "attempt": job.attempt,
                    "key": key,
                    "manifest": json.dumps(manifest),
                },
            )
            if not finished[0].finished:
                await storage.delete(key)
        except Exception:
            if uploaded:
                # Private artifact stays unreferenced, even when cleanup fails.
                with suppress(Exception):
                    await storage.delete(key)
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
    storage: ObjectStorage | None = ctx.get("storage")
    if storage is None:
        return "export storage unavailable; expired objects remain private"
    engine: AsyncEngine = ctx["engine"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_export_cleanup(:limit)", {"limit": 20}
    )
    for row in rows:
        try:
            await storage.delete(row.storage_key)
        except Exception:
            logger.warning("Expired private export object cleanup failed; retrying later")
            continue  # Remains private and marked expired; retry next maintenance run.
        await _worker_query(
            engine,
            "SELECT app_security.finish_export_cleanup(:id,:key)",
            {"id": row.job_id, "key": row.storage_key},
        )
    return f"cleaned {len(rows)} expired export object(s)"


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Awaitable[str]]]] = [health_job]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(deliver_notices, second=0, run_at_startup=True),
        cron(assign_tickets, second=5, run_at_startup=True),
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
        cron(apply_retention, hour={3}, minute={17}, second=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    max_jobs = 20
    job_timeout = 120
