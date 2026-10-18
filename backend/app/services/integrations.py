"""Integration orchestration: policy, scope, encryption, idempotency and unified inbox."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import CommunicationChannel, MessageDirection, MessageVisibility
from app.domain.schemas import (
    COMPANY_ONLY_PROVIDERS,
    GLOBAL_ONLY_PROVIDERS,
    AIAnalysisRequest,
    OAuthStartRequest,
    SandboxSendRequest,
    SendComplaintEmailRequest,
    VoiceFollowupRequest,
)
from app.domain.schemas import (
    WhatsAppSendRequest as WhatsAppSendSchema,
)
from app.models.identity import Customer
from app.models.tickets import (
    AiInsight,
    Attachment,
    CallLog,
    Complaint,
    ComplaintEvent,
    ComplaintMessage,
)
from app.repositories.attachments import AttachmentRepository
from app.repositories.complaints import ComplaintRepository
from app.repositories.integrations import IntegrationRepository
from app.security.integration_crypto import SecretCipher
from app.services.email_templates import render_customer_reply, render_insurer_forward
from app.services.integration_providers import (
    PROVIDER_REQUIRED_FIELDS as _PROVIDER_REQUIRED_FIELDS,
)
from app.services.integration_providers import (
    EmailSendRequest,
    IntegrationProviderError,
    OAuthTokenBroker,
    SMSProviderRequest,
    build_provider,
    probe_provider,
)
from app.services.providers import ProviderUnavailable

_TICKET_RE = re.compile(r"\bCMP-\d{4}-\d{6,}\b", re.IGNORECASE)
_NATIONAL_ID_RE = re.compile(r"(?<!\d)\d{14}(?!\d)")
_EGYPT_MOBILE_RE = re.compile(r"(?<!\w)(?:\+?20|0)?1[0125]\d{8}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_POLICY_RE = re.compile(
    r"(?i)(policy|policy number|رقم الوثيقة|رقم البوليصة)\s*[:#]?\s*[A-Z0-9/-]{4,}"
)


def config_aad(company_id: UUID | None, provider: str) -> str:
    return f"integration-config:v1:{company_id or 'global'}:{provider}"


def job_aad(idempotency_digest: bytes) -> str:
    return f"integration-job:v1:{idempotency_digest.hex()}"


def ai_insight_aad(job_id: UUID, kind: str) -> str:
    return f"ai-insight:v1:{job_id}:{kind}"


def call_transcript_aad(call_id: UUID) -> str:
    return f"call-transcript:v1:{call_id}"


def webhook_secret_aad(webhook_id: UUID) -> str:
    return f"webhook-secret:v1:{webhook_id}"


_CONNECTION_MESSAGES: dict[str, str] = {
    "smtp_connection_failed": "SMTP server rejected the connection or the credentials.",
    "sendgrid_auth_failed": "SendGrid rejected the API key.",
    "ses_connection_failed": "Amazon SES rejected the SMTP credentials.",
    "gmail_auth_failed": "Google rejected the OAuth tokens or their scopes.",
    "outlook_auth_failed": "Microsoft rejected the OAuth tokens or their scopes.",
    "imap_connection_failed": "IMAP server rejected the connection or the credentials.",
    "imap_mailbox_unavailable": "The IMAP mailbox does not exist or is not selectable.",
    "whatsapp_auth_failed": "Meta rejected the WhatsApp access token or phone number id.",
    "twilio_auth_failed": "Twilio rejected the account SID or auth token.",
    "twilio_account_inactive": "The Twilio account is not active.",
    "twilio_unreachable": "Twilio could not be reached; try again.",
    "llm_auth_failed": "The AI provider rejected the API key.",
    "llm_unreachable": "The AI provider could not be reached; try again.",
    "vonage_auth_failed": "Vonage rejected the application credentials.",
    "speech_auth_failed": "The speech provider rejected the API key.",
    "oauth_token_request_rejected": "The OAuth token endpoint rejected the request.",
    "oauth_refresh_token_missing": "Reconnect the account; the refresh token is missing.",
}


def _mask_recipient(value: str) -> str:
    """Mask a recipient for display: never echo the full address/number back."""
    if "@" in value:
        local, _, domain = value.partition("@")
        return (local[:1] or "*") + "***@" + domain
    digits = re.sub(r"\D", "", value)
    return ("*" * max(0, len(digits) - 4)) + digits[-4:]


def minimize_pii(text: str, *, max_chars: int = 6000, known_names: tuple[str, ...] = ()) -> str:
    """Remove identifiers, contact data and a known customer's name before LLM calls."""
    value = text[: max_chars * 2]
    value = _NATIONAL_ID_RE.sub("[NATIONAL_ID]", value)
    value = _EGYPT_MOBILE_RE.sub("[PHONE]", value)
    value = _EMAIL_RE.sub("[EMAIL]", value)
    value = _POLICY_RE.sub("[POLICY_REFERENCE]", value)
    for name in sorted(
        (item.strip() for item in known_names if item.strip()), key=len, reverse=True
    ):
        value = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[CUSTOMER]", value, flags=re.IGNORECASE)
    return value[:max_chars]


def extract_ticket_no(*values: str) -> str | None:
    for value in values:
        match = _TICKET_RE.search(value)
        if match:
            return match.group(0).upper()
    return None


def verify_timestamped_hmac(
    raw_body: bytes, timestamp: str | None, signature: str | None, secret: str, *, now: int
) -> bool:
    if timestamp is None or signature is None or not timestamp.isdigit():
        return False
    timestamp_value = int(timestamp)
    if abs(now - timestamp_value) > 300:
        return False
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def verify_raw_hmac(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def verify_twilio_signature(
    callback_url: str,
    form_fields: dict[str, list[str]],
    signature: str | None,
    auth_token: str,
) -> bool:
    """Validate Twilio's documented HMAC-SHA1 form signature against its configured URL."""
    if not callback_url.startswith("https://") or not signature or not auth_token:
        return False
    signed = callback_url + "".join(
        key + value for key in sorted(form_fields) for value in sorted(form_fields[key])
    )
    expected = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature)


class IntegrationService:
    def __init__(
        self,
        cipher: SecretCipher,
        idempotency_secret: bytes,
        *,
        repository: IntegrationRepository | None = None,
        complaints: ComplaintRepository | None = None,
        attachments: AttachmentRepository | None = None,
        oauth_redirect_base: str | None = None,
    ) -> None:
        if len(idempotency_secret) < 32:
            raise ValueError("Integration idempotency secret must be at least 32 bytes")
        self.cipher = cipher
        self.idempotency_secret = idempotency_secret
        self.repository = repository or IntegrationRepository()
        self.complaints = complaints or ComplaintRepository()
        self.attachments = attachments or AttachmentRepository()
        self.oauth_redirect_base = oauth_redirect_base.rstrip("/") if oauth_redirect_base else None

    async def send_staff_otp(self, db: AsyncSession, mobile: str, code: str) -> None:
        """Send the password-verified staff challenge through the global SMS provider."""
        loaded = await self._load_credentials(db, None, "twilio_sms", fallback=False)
        if loaded is None:
            raise ProviderUnavailable("A global Twilio SMS integration is required")
        _config_id, _company_id, config = loaded
        platform_name = await db.scalar(
            text("SELECT platform_name FROM app_security.public_platform_brand()")
        )
        brand = str(platform_name or "Insurance").strip()[:80] or "Insurance"
        try:
            await build_provider("twilio_sms", config).send(
                SMSProviderRequest(
                    recipient=mobile,
                    body=f"{brand} staff verification code: {code}. Do not share this code.",
                ),
                idempotency_key="staff-otp-" + secrets.token_urlsafe(24),
            )
        except IntegrationProviderError as exc:
            raise ProviderUnavailable("Staff SMS OTP delivery failed") from exc

    @staticmethod
    def _require_staff(scope: AccessScope) -> None:
        if scope.actor.scope != "staff":
            raise HTTPException(status_code=403, detail="Staff access required")

    def _digest(self, kind: str, scope: AccessScope, complaint_id: UUID, key: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise HTTPException(status_code=422, detail="A valid Idempotency-Key is required")
        material = f"{kind}:{scope.actor.id}:{complaint_id}:{key}".encode()
        return hmac.new(self.idempotency_secret, material, hashlib.sha256).digest()

    def _inbound_digest(self, company_id: UUID, provider: str, event_id: str) -> bytes:
        material = f"inbound:{company_id}:{provider}:{event_id}".encode()
        return hmac.new(self.idempotency_secret, material, hashlib.sha256).digest()

    def _payload_digest(self, payload: dict[str, Any]) -> bytes:
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hmac.new(self.idempotency_secret, raw, hashlib.sha256).digest()

    @staticmethod
    def _message_id(kind: str, digest: bytes) -> UUID:
        return uuid5(NAMESPACE_URL, f"insurance-platform:{kind}:{digest.hex()}")

    @staticmethod
    def _job_error(exc: Exception) -> HTTPException:
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail="Integration action is not authorized")
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", "")
        if sqlstate == "55000":
            return HTTPException(status_code=503, detail="Integration is not connected")
        if sqlstate == "23505":
            return HTTPException(
                status_code=409, detail="Idempotency key was reused with another request"
            )
        if sqlstate == "42501":
            return HTTPException(status_code=403, detail="Integration action is not authorized")
        return HTTPException(status_code=503, detail="Integration queue is unavailable")

    async def _queue_message(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint: Complaint,
        *,
        channel: CommunicationChannel,
        body: str,
        job_kind: Literal["send_email", "send_whatsapp", "send_sms"],
        key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        digest = self._digest(job_kind, scope, complaint.id, key)
        payload_digest = self._payload_digest(payload)
        message_id = self._message_id(job_kind, digest)
        message = {
            "id": message_id,
            "complaint_id": complaint.id,
            "complaint_created_at": complaint.created_at,
            "body": body,
            "visibility": MessageVisibility.PUBLIC,
            "channel": channel,
            "direction": MessageDirection.OUTBOUND,
            "sender_staff_id": scope.actor.id,
            "delivery_status": "queued",
        }
        await db.execute(
            pg_insert(ComplaintMessage)
            .values(**message)
            .on_conflict_do_nothing(index_elements=[ComplaintMessage.id])
        )
        encrypted = self.cipher.encrypt_json(payload, associated_data=job_aad(digest))
        try:
            queued = await self.repository.enqueue_job(
                db,
                job_kind=job_kind,
                company_id=complaint.company_id,
                complaint_id=complaint.id,
                complaint_created_at=complaint.created_at,
                sender_staff_id=scope.actor.id,
                message_id=message_id,
                call_log_id=None,
                payload_ciphertext=encrypted,
                idempotency_digest=digest,
                payload_digest=payload_digest,
            )
        except Exception as exc:
            raise self._job_error(exc) from exc
        await self.publish_event(db, scope, complaint, "complaint.message_received")
        return {
            "job_id": queued["job_id"],
            "message_id": message_id,
            "state": queued["state"],
            "created": queued["created"],
        }

    async def send_complaint_by_email(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        request: SendComplaintEmailRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        recipients = await self.repository.company_contacts(db, complaint.company_id)
        if not recipients:
            raise HTTPException(status_code=409, detail="Insurance company has no contact email")
        selected: list[Attachment] = []
        for attachment_id in request.attachment_ids:
            record = await self.attachments.clean_attachment(db, scope, attachment_id)
            if (
                record is None
                or record.complaint_id != complaint.id
                or record.complaint_created_at != complaint.created_at
            ):
                raise HTTPException(
                    status_code=422, detail="Only clean attachments from this ticket can be sent"
                )
            selected.append(record)
        subject, body = render_insurer_forward(
            ticket_no=complaint.ticket_no,
            subject=minimize_pii(complaint.subject, max_chars=160),
            details=minimize_pii(complaint.description, max_chars=12_000),
            policy_number=complaint.policy_number,
            locale=request.locale,
            agent_note=(
                minimize_pii(request.agent_note, max_chars=2000) if request.agent_note else None
            ),
        )
        payload = {
            "recipients": recipients,
            "subject": subject,
            "body": body,
            "locale": request.locale,
            "attachment_ids": [str(item.id) for item in selected],
        }
        return await self._queue_message(
            db,
            scope,
            complaint,
            channel=CommunicationChannel.EMAIL,
            body=body,
            job_kind="send_email",
            key=idempotency_key,
            payload=payload,
        )

    async def send_customer_email(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        body: str,
        locale: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        email = await db.scalar(select(Customer.email).where(Customer.id == complaint.customer_id))
        if not email:
            raise HTTPException(status_code=409, detail="Customer email address is unavailable")
        subject, localized_body = render_customer_reply(complaint.ticket_no, body, locale)
        payload = {
            "recipients": [email],
            "subject": subject,
            "body": localized_body,
            "locale": locale,
            "attachment_ids": [],
        }
        return await self._queue_message(
            db,
            scope,
            complaint,
            channel=CommunicationChannel.EMAIL,
            body=body,
            job_kind="send_email",
            key=idempotency_key,
            payload=payload,
        )

    async def send_whatsapp(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        request: WhatsAppSendSchema,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        customer = await db.execute(
            select(Customer.mobile, Customer.whatsapp_available).where(
                Customer.id == complaint.customer_id
            )
        )
        contact = customer.one_or_none()
        if contact is None or not contact.whatsapp_available:
            raise HTTPException(status_code=409, detail="Customer has not opted in to WhatsApp")
        if request.template_name is None:
            session_open = await db.scalar(
                select(func.count())
                .select_from(ComplaintMessage)
                .where(
                    ComplaintMessage.complaint_id == complaint.id,
                    ComplaintMessage.complaint_created_at == complaint.created_at,
                    ComplaintMessage.channel == CommunicationChannel.WHATSAPP,
                    ComplaintMessage.direction == MessageDirection.INBOUND,
                    ComplaintMessage.created_at >= datetime.now(UTC) - timedelta(hours=24),
                )
            )
            if not session_open:
                raise HTTPException(
                    status_code=409,
                    detail="Use an approved WhatsApp template outside the customer service window",
                )
        payload = {
            "recipient": contact.mobile,
            "body": request.body,
            "template_name": request.template_name,
            "language": request.language,
            "parameters": request.parameters,
        }
        return await self._queue_message(
            db,
            scope,
            complaint,
            channel=CommunicationChannel.WHATSAPP,
            body=request.body,
            job_kind="send_whatsapp",
            key=idempotency_key,
            payload=payload,
        )

    async def _enforce_ai_budget(self, db: AsyncSession, company_id: UUID) -> None:
        """Bound per-day AI usage with the limit stored in the provider config."""
        limit = 500
        for provider in ("openai", "mock_llm"):
            loaded = await self._load_credentials(db, company_id, provider, fallback=True)
            if loaded is None:
                continue
            raw = loaded[2].get("daily_request_limit")
            if str(raw).isdigit():
                limit = max(1, min(int(str(raw)), 100_000))
            break
        used = await self.repository.integration_job_counts(
            db, company_id, "ai_analyze", window_seconds=86_400
        )
        if used >= limit:
            raise HTTPException(
                status_code=429,
                detail="The AI daily request limit for this company has been reached",
            )

    async def request_ai_analysis(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        request: AIAnalysisRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        await self._enforce_ai_budget(db, complaint.company_id)
        customer_name = await db.scalar(
            select(Customer.full_name).where(Customer.id == complaint.customer_id)
        )
        minimized = minimize_pii(
            f"{complaint.subject}\n{complaint.description}",
            known_names=(customer_name,) if customer_name else (),
        )
        payload = {"text": minimized, "locale": request.locale, "ticket_no": complaint.ticket_no}
        digest = self._digest("ai_analyze", scope, complaint.id, idempotency_key)
        encrypted = self.cipher.encrypt_json(payload, associated_data=job_aad(digest))
        try:
            queued = await self.repository.enqueue_job(
                db,
                job_kind="ai_analyze",
                company_id=complaint.company_id,
                complaint_id=complaint.id,
                complaint_created_at=complaint.created_at,
                sender_staff_id=scope.actor.id,
                message_id=None,
                call_log_id=None,
                payload_ciphertext=encrypted,
                idempotency_digest=digest,
                payload_digest=self._payload_digest(payload),
            )
        except Exception as exc:
            raise self._job_error(exc) from exc
        return {"job_id": queued["job_id"], "state": queued["state"], "created": queued["created"]}

    async def list_ai_insights(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> list[dict[str, Any]]:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        result = await db.scalars(
            select(AiInsight)
            .where(
                AiInsight.complaint_id == complaint.id,
                AiInsight.complaint_created_at == complaint.created_at,
            )
            .order_by(AiInsight.created_at.desc(), AiInsight.id.desc())
            .limit(50)
        )
        response: list[dict[str, Any]] = []
        for insight in result:
            if insight.integration_job_id is None:
                continue
            try:
                payload = self.cipher.decrypt_json(
                    insight.payload_ciphertext,
                    associated_data=ai_insight_aad(insight.integration_job_id, insight.kind),
                )
            except ValueError:
                continue
            response.append(
                {
                    "id": insight.id,
                    "kind": insight.kind,
                    "payload": payload,
                    "confidence": float(insight.confidence)
                    if insight.confidence is not None
                    else None,
                    "provider_model": insight.provider_model,
                    "review_status": insight.review_status,
                    "created_at": insight.created_at,
                }
            )
        return response

    async def review_ai_insight(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        insight_id: UUID,
        accept: bool,
    ) -> dict[str, Any]:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        changed = await self.repository.review_ai_insight(
            db, complaint.id, complaint.created_at, insight_id, accept
        )
        if not changed:
            raise HTTPException(
                status_code=409, detail="AI suggestion was already reviewed or is unavailable"
            )
        return {"id": insight_id, "review_status": "accepted" if accept else "rejected"}

    async def send_approved_reply(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        insight_id: UUID,
        channel: Literal["email", "whatsapp"],
        idempotency_key: str,
    ) -> dict[str, Any]:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        insight = await db.scalar(
            select(AiInsight).where(
                AiInsight.id == insight_id,
                AiInsight.complaint_id == complaint.id,
                AiInsight.complaint_created_at == complaint.created_at,
                AiInsight.kind == "reply",
                AiInsight.review_status == "accepted",
            )
        )
        if insight is None or insight.integration_job_id is None:
            raise HTTPException(status_code=409, detail="Only an approved AI reply can be sent")
        payload = self.cipher.decrypt_json(
            insight.payload_ciphertext,
            associated_data=ai_insight_aad(insight.integration_job_id, insight.kind),
        )
        body = str(payload.get("reply", ""))[:5000]
        if not body:
            raise HTTPException(status_code=409, detail="Approved suggestion has no reply text")
        if channel == "email":
            return await self.send_customer_email(
                db, scope, complaint.id, body, str(payload.get("locale", "en")), idempotency_key
            )
        return await self.send_whatsapp(
            db,
            scope,
            complaint.id,
            WhatsAppSendSchema(body=body, language=payload.get("locale", "en")),
            idempotency_key,
        )

    async def similar_tickets(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID, limit: int = 10
    ) -> list[dict[str, Any]]:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        query_text = complaint.subject.strip()[:250]
        if not query_text:
            return []
        rank = func.ts_rank(
            Complaint.search_vector, func.plainto_tsquery("simple", query_text)
        ).label("similarity")
        stmt = (
            select(
                Complaint.id,
                Complaint.ticket_no,
                Complaint.subject,
                Complaint.status,
                Complaint.created_at,
                rank,
            )
            .where(
                scope.predicate(),
                Complaint.company_id == complaint.company_id,
                Complaint.insurance_type == complaint.insurance_type,
                Complaint.id != complaint.id,
                Complaint.search_vector.op("@@")(func.plainto_tsquery("simple", query_text)),
            )
            .order_by(rank.desc(), Complaint.created_at.desc())
            .limit(max(1, min(limit, 20)))
        )
        rows = await db.execute(stmt)
        return [dict(row._mapping) for row in rows]

    async def record_voice_consent(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID, enabled: bool
    ) -> dict[str, Any]:
        if scope.actor.scope != "customer":
            raise HTTPException(status_code=403, detail="Customer account required")
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        consent_at = await self.repository.set_voice_consent(
            db, complaint.id, complaint.created_at, enabled
        )
        return {"enabled": enabled, "consent_at": consent_at}

    async def schedule_voice_followup(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        request: VoiceFollowupRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        customer = (
            await db.execute(
                select(
                    Customer.mobile,
                    Customer.voice_followup_opt_in,
                    Customer.voice_consent_at,
                ).where(Customer.id == complaint.customer_id)
            )
        ).one_or_none()
        if (
            customer is None
            or not customer.voice_followup_opt_in
            or customer.voice_consent_at is None
        ):
            raise HTTPException(status_code=409, detail="Customer voice consent is required")
        digest = self._digest("voice_followup", scope, complaint.id, idempotency_key)
        call_id = uuid5(NAMESPACE_URL, f"insurance-platform:call:{digest.hex()}")
        scheduled_at = request.scheduled_at.astimezone(UTC)
        await self.repository.queue_call_log(
            db,
            call_id=call_id,
            complaint_id=complaint.id,
            complaint_created_at=complaint.created_at,
            sender_staff_id=scope.actor.id,
            scheduled_at=scheduled_at,
        )
        script = (
            f"نحن نتصل لمتابعة الشكوى {complaint.ticket_no}. إذا كنت بحاجة إلى مساعدة بشرية، "
            "يرجى طلب التحدث إلى أحد الموظفين."
            if request.language == "ar"
            else (
                f"We are following up on complaint {complaint.ticket_no}. "
                "Ask for a human agent at any time."
            )
        )
        payload = {
            "mobile": customer.mobile,
            "ticket_no": complaint.ticket_no,
            "script": script,
            "language": request.language,
            "consent_confirmed": True,
            "scheduled_at": scheduled_at.isoformat(),
        }
        encrypted = self.cipher.encrypt_json(payload, associated_data=job_aad(digest))
        try:
            queued = await self.repository.enqueue_job(
                db,
                job_kind="voice_followup",
                company_id=complaint.company_id,
                complaint_id=complaint.id,
                complaint_created_at=complaint.created_at,
                sender_staff_id=scope.actor.id,
                message_id=None,
                call_log_id=call_id,
                payload_ciphertext=encrypted,
                idempotency_digest=digest,
                payload_digest=self._payload_digest(payload),
                scheduled_at=scheduled_at,
            )
        except Exception as exc:
            raise self._job_error(exc) from exc
        return {
            "job_id": queued["job_id"],
            "call_log_id": call_id,
            "state": queued["state"],
            "created": queued["created"],
        }

    async def unified_inbox(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        *,
        include_calls: bool,
    ) -> dict[str, Any]:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        items: list[dict[str, Any]] = []
        messages = await db.scalars(
            select(ComplaintMessage)
            .where(
                ComplaintMessage.complaint_id == complaint.id,
                ComplaintMessage.complaint_created_at == complaint.created_at,
            )
            .order_by(ComplaintMessage.created_at, ComplaintMessage.id)
            .limit(500)
        )
        for item in messages:
            items.append(
                {
                    "id": item.id,
                    "kind": "message",
                    "channel": item.channel.value,
                    "direction": item.direction.value,
                    "body": item.body,
                    "visibility": item.visibility.value,
                    "sender_staff_id": item.sender_staff_id,
                    "sender_customer_id": item.sender_customer_id,
                    "sender_external": item.sender_external,
                    "delivery_status": item.delivery_status,
                    "delivered_at": item.delivered_at,
                    "read_at": item.read_at,
                    "created_at": item.created_at,
                }
            )
        event_query = (
            select(ComplaintEvent)
            .join(
                Complaint,
                (Complaint.id == ComplaintEvent.complaint_id)
                & (Complaint.created_at == ComplaintEvent.complaint_created_at),
            )
            .where(
                scope.predicate(),
                Complaint.id == complaint.id,
                Complaint.created_at == complaint.created_at,
            )
            .order_by(ComplaintEvent.occurred_at, ComplaintEvent.id)
            .limit(500)
        )
        events = await db.scalars(event_query)
        for event in events:
            items.append(
                {
                    "id": event.id,
                    "kind": "event",
                    "channel": "system",
                    "event_type": event.event_type,
                    "from_status": event.from_status.value if event.from_status else None,
                    "to_status": event.to_status.value if event.to_status else None,
                    "metadata": event.event_metadata,
                    "occurred_at": event.occurred_at,
                }
            )
        if include_calls:
            calls = await db.scalars(
                select(CallLog)
                .where(
                    CallLog.complaint_id == complaint.id,
                    CallLog.complaint_created_at == complaint.created_at,
                )
                .order_by(CallLog.started_at, CallLog.scheduled_at, CallLog.id)
                .limit(100)
            )
            for call in calls:
                transcript = None
                if call.transcript_ciphertext:
                    try:
                        transcript = self.cipher.decrypt(
                            call.transcript_ciphertext,
                            associated_data=call_transcript_aad(call.id),
                        ).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        transcript = None
                items.append(
                    {
                        "id": call.id,
                        "kind": "call",
                        "channel": "ai_voice",
                        "direction": call.direction.value,
                        "status": call.status,
                        "outcome": call.outcome,
                        "transcript": transcript,
                        "consent_at": call.consent_at,
                        "scheduled_at": call.scheduled_at,
                        "started_at": call.started_at,
                        "ended_at": call.ended_at,
                        "sender_staff_id": call.sender_staff_id,
                    }
                )
        items.sort(
            key=lambda item: (
                item.get("created_at")
                or item.get("occurred_at")
                or item.get("started_at")
                or item.get("scheduled_at")
                or datetime.min.replace(tzinfo=UTC)
            )
        )
        return {"ticket_no": complaint.ticket_no, "items": items}

    async def publish_event(
        self, db: AsyncSession, scope: AccessScope, complaint: Complaint, event: str
    ) -> None:
        if scope.actor.scope != "staff":
            return
        if event == "complaint.message_received":
            event_id = await db.scalar(
                select(ComplaintMessage.id)
                .where(
                    ComplaintMessage.complaint_id == complaint.id,
                    ComplaintMessage.complaint_created_at == complaint.created_at,
                    ComplaintMessage.sender_staff_id == scope.actor.id,
                )
                .order_by(ComplaintMessage.created_at.desc(), ComplaintMessage.id.desc())
                .limit(1)
            )
        else:
            event_id = await db.scalar(
                select(ComplaintEvent.id)
                .where(
                    ComplaintEvent.complaint_id == complaint.id,
                    ComplaintEvent.complaint_created_at == complaint.created_at,
                    ComplaintEvent.event_type == "status_changed",
                )
                .order_by(ComplaintEvent.occurred_at.desc(), ComplaintEvent.id.desc())
                .limit(1)
            )
        if event_id is None:
            return
        hook_ids = await self.repository.webhooks_for_event(
            db, complaint.id, complaint.created_at, event
        )
        if not hook_ids:
            return
        payload = {
            "event_id": str(event_id),
            "event_type": event,
            "ticket_no": complaint.ticket_no,
            "status": complaint.status.value,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        payload_digest = self._payload_digest(payload)
        for hook_id in hook_ids:
            idem = hmac.new(
                self.idempotency_secret,
                f"webhook:{hook_id}:{event_id}".encode(),
                hashlib.sha256,
            ).digest()
            encrypted = self.cipher.encrypt_json(payload, associated_data=job_aad(idem))
            await self.repository.enqueue_webhook(
                db,
                webhook_id=hook_id,
                complaint_id=complaint.id,
                created_at=complaint.created_at,
                company_id=complaint.company_id,
                event=event,
                ciphertext=encrypted,
                idempotency_digest=idem,
                payload_digest=payload_digest,
            )

    async def create_webhook(
        self,
        db: AsyncSession,
        *,
        company_id: UUID | None,
        url: str,
        event_types: set[str],
    ) -> dict[str, Any]:
        webhook_id = uuid4()
        secret = secrets.token_urlsafe(32)
        ciphertext = self.cipher.encrypt(
            secret.encode(), associated_data=webhook_secret_aad(webhook_id)
        )
        result = await self.repository.save_webhook(
            db,
            webhook_id=webhook_id,
            company_id=company_id,
            url=url,
            events=sorted(event_types),
            ciphertext=ciphertext,
            active=True,
        )
        if result != webhook_id:
            raise HTTPException(status_code=403, detail="Webhook configuration was not authorized")
        return {
            "id": result,
            "url": url,
            "event_types": sorted(event_types),
            "signing_secret": secret,
        }

    async def list_webhooks(self, db: AsyncSession) -> list[dict[str, Any]]:
        return await self.repository.list_webhooks(db)

    async def deactivate_webhook(self, db: AsyncSession, webhook_id: UUID) -> None:
        existing = next(
            (item for item in await self.repository.list_webhooks(db) if item["id"] == webhook_id),
            None,
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="Webhook unavailable")
        updated = await self.repository.save_webhook(
            db,
            webhook_id=webhook_id,
            company_id=existing["company_id"],
            url=existing["url"],
            events=list(existing["event_types"]),
            ciphertext=None,
            active=False,
        )
        if updated is None:
            raise HTTPException(status_code=403, detail="Webhook change was not authorized")

    async def verify_whatsapp_subscription(
        self,
        db: AsyncSession,
        company_id: UUID,
        mode: str | None,
        verify_token: str | None,
        challenge: str | None,
    ) -> str:
        row = await self.repository.webhook_config(db, company_id, "whatsapp_cloud")
        if row is None:
            raise HTTPException(status_code=404, detail="WhatsApp integration is not connected")
        config_company_id, ciphertext = row
        try:
            config = self.cipher.decrypt_json(
                ciphertext, associated_data=config_aad(config_company_id, "whatsapp_cloud")
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503, detail="WhatsApp configuration is invalid"
            ) from exc
        expected = str(config.get("verify_token", ""))
        if (
            mode != "subscribe"
            or not expected
            or not verify_token
            or not hmac.compare_digest(expected, verify_token)
            or not challenge
            or len(challenge) > 512
        ):
            raise HTTPException(status_code=403, detail="WhatsApp subscription verification failed")
        return challenge

    async def list_configs(self, db: AsyncSession) -> list[dict[str, Any]]:
        return await self.repository.list_configs(db)

    async def _load_credentials(
        self, db: AsyncSession, company_id: UUID | None, provider: str, *, fallback: bool
    ) -> tuple[UUID, UUID | None, dict[str, Any]] | None:
        """Return (config_id, config_company_id, credentials) or None. Values never leave here."""
        row = await self.repository.integration_credentials(
            db, company_id, provider, fallback=fallback
        )
        if row is None:
            return None
        try:
            config = self.cipher.decrypt_json(
                bytes(row["config_ciphertext"]),
                associated_data=config_aad(row["config_company_id"], provider),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=503, detail="Integration configuration is invalid"
            ) from exc
        return row["config_id"], row["config_company_id"], config

    @staticmethod
    def _require_complete(provider: str, merged: dict[str, Any]) -> None:
        required = _PROVIDER_REQUIRED_FIELDS.get(provider, frozenset())
        if not required <= merged.keys():
            raise HTTPException(
                status_code=422, detail="Required credentials are missing for this provider"
            )

    async def save_config(
        self,
        db: AsyncSession,
        *,
        company_id: UUID | None,
        provider: str,
        credentials: dict[str, Any] | None,
        enabled: bool,
        clear_credentials: bool = False,
    ) -> dict[str, Any]:
        if clear_credentials and (enabled or credentials is not None):
            raise HTTPException(
                status_code=422, detail="Credential removal must disconnect the provider"
            )
        if provider in COMPANY_ONLY_PROVIDERS and company_id is None:
            raise HTTPException(
                status_code=422, detail="This integration must be configured for one company"
            )
        if provider in GLOBAL_ONLY_PROVIDERS and company_id is not None:
            raise HTTPException(status_code=422, detail="File scanning is a platform-wide setting")
        merged: dict[str, Any] | None = None
        if credentials is not None and not clear_credentials:
            existing = await self._load_credentials(db, company_id, provider, fallback=False)
            merged = {**(existing[2] if existing else {}), **credentials}
            if enabled:
                self._require_complete(provider, merged)
        elif enabled and not clear_credentials:
            existing = await self._load_credentials(db, company_id, provider, fallback=False)
            if existing is not None:
                self._require_complete(provider, existing[2])
        ciphertext = (
            self.cipher.encrypt_json(merged, associated_data=config_aad(company_id, provider))
            if merged is not None and not clear_credentials
            else None
        )
        try:
            config_id = await self.repository.save_config(
                db, company_id, provider, ciphertext, enabled, clear_credentials
            )
        except Exception as exc:
            raise self._job_error(exc) from exc
        return {
            "id": config_id,
            "company_id": company_id,
            "provider": provider,
            "enabled": enabled,
            "credentials_saved": merged is not None and not clear_credentials,
            "connection_status": "unconfigured" if clear_credentials else "configured",
        }

    async def test_connection(
        self,
        db: AsyncSession,
        *,
        scope: AccessScope,
        company_id: UUID | None,
        provider: str,
    ) -> dict[str, Any]:
        """Probe credentials without sending mail, messages or placing calls."""
        self._require_staff(scope)
        if provider not in _PROVIDER_REQUIRED_FIELDS:
            raise HTTPException(status_code=422, detail="Unknown integration provider")
        loaded = await self._load_credentials(db, company_id, provider, fallback=True)
        if loaded is None:
            return {
                "provider": provider,
                "company_id": company_id,
                "connection_status": "unconfigured",
                "tested_at": None,
                "error_code": None,
                "message": "No credentials are stored for this provider yet.",
            }
        config_id, config_company_id, config = loaded
        status: str
        error_code: str | None
        try:
            await probe_provider(provider, config)
            status, error_code = "connected", None
        except IntegrationProviderError as exc:
            status, error_code = "failed", exc.code
        except Exception:
            status, error_code = "failed", "provider_probe_failed"
        try:
            await self.repository.record_connection_test(db, config_id, status, error_code)
        except Exception as exc:
            raise self._job_error(exc) from exc
        return {
            "provider": provider,
            "company_id": config_company_id,
            "connection_status": status,
            "tested_at": datetime.now(UTC),
            "error_code": error_code,
            "message": _CONNECTION_MESSAGES.get(
                error_code or "", "Connection verified; the provider accepted the credentials."
            )
            if status == "connected"
            else _CONNECTION_MESSAGES.get(
                error_code or "provider_probe_failed",
                "The connection test failed. Check the stored credentials.",
            ),
        }

    async def sandbox_send(
        self,
        db: AsyncSession,
        *,
        scope: AccessScope,
        request: SandboxSendRequest,
    ) -> dict[str, Any]:
        """Send a fixed template to the stored sandbox address after explicit consent."""
        self._require_staff(scope)
        if not request.confirm:
            raise HTTPException(
                status_code=422,
                detail="Sandbox sends require an explicit confirmation",
            )
        loaded = await self._load_credentials(
            db, request.company_id, request.provider, fallback=True
        )
        if loaded is None:
            raise HTTPException(status_code=409, detail="Integration is not configured")
        config_id, config_company_id, config = loaded
        sandbox = str(config.get("sandbox_recipient", "")).strip()
        if not sandbox:
            raise HTTPException(
                status_code=422,
                detail="Configure sandbox_recipient for this provider before testing sends",
            )
        subject = "Insurance Platform sandbox integration test"
        body = (
            "This is an automated sandbox test of the configured provider. "
            "It contains no customer data. Reply STOP to opt out of further tests."
        )
        key = "sandbox-" + secrets.token_urlsafe(12)
        try:
            if request.provider == "twilio_sms":
                result = await build_provider(request.provider, config).send(
                    SMSProviderRequest(recipient=sandbox, body=body), idempotency_key=key
                )
            else:
                result = await build_provider(request.provider, config).send(
                    EmailSendRequest(
                        recipients=(sandbox,), subject=subject, text_body=body, locale="en"
                    ),
                    idempotency_key=key,
                )
        except IntegrationProviderError as exc:
            status = "failed"
            await self.repository.record_connection_test(db, config_id, status, exc.code)
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Sandbox send failed",
                    "error_code": exc.code,
                },
            ) from exc
        await self.repository.record_connection_test(db, config_id, "connected", None)
        return {
            "provider": request.provider,
            "queued": True,
            "recipient_masked": _mask_recipient(sandbox),
            "message": "Sandbox message accepted by the provider.",
            "provider_id": result.provider_id,
            "company_id": config_company_id,
        }

    def oauth_redirect_uri(self, provider: str) -> str:
        if not self.oauth_redirect_base:
            raise HTTPException(
                status_code=503,
                detail="OAuth redirect base URL is not configured (PUBLIC_APP_URL)",
            )
        return (
            f"{self.oauth_redirect_base}/api/staff/admin/integration-connections"
            f"/oauth/{provider}/callback"
        )

    def _sign_oauth_state(
        self, actor_id: UUID, company_id: UUID | None, provider: str, redirect_path: str
    ) -> str:
        expires = int(datetime.now(UTC).timestamp()) + 600
        nonce = secrets.token_urlsafe(18)
        payload = json.dumps(
            {
                "actor": str(actor_id),
                "company": str(company_id) if company_id else None,
                "provider": provider,
                "nonce": nonce,
                "redirect": redirect_path,
                "exp": expires,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        signature = hmac.new(
            self.idempotency_secret, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii") + "." + signature

    def _verify_oauth_state(
        self, state: str, *, actor_id: UUID, provider: str
    ) -> tuple[UUID | None, str]:
        try:
            encoded, signature = state.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="OAuth state is invalid") from exc
        expected = hmac.new(
            self.idempotency_secret, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="OAuth state is invalid")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="OAuth state is invalid") from exc
        if (
            value.get("actor") != str(actor_id)
            or value.get("provider") != provider
            or int(value.get("exp", 0)) < int(datetime.now(UTC).timestamp())
        ):
            raise HTTPException(status_code=400, detail="OAuth state expired or mismatched")
        redirect = str(value.get("redirect") or "/staff/operations")
        if not redirect.startswith("/") or redirect.startswith("//"):
            redirect = "/staff/operations"
        company = value.get("company")
        try:
            company_id = UUID(str(company)) if company else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="OAuth state is invalid") from exc
        return company_id, redirect

    async def oauth_start(
        self,
        db: AsyncSession,
        *,
        scope: AccessScope,
        request: OAuthStartRequest,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        if request.provider not in ("gmail_oauth", "outlook_oauth"):
            raise HTTPException(status_code=422, detail="Unsupported OAuth provider")
        loaded = await self._load_credentials(
            db, request.company_id, request.provider, fallback=False
        )
        config = loaded[2] if loaded else {}
        if not config.get("client_id") or not config.get("client_secret"):
            raise HTTPException(
                status_code=422,
                detail="Save the OAuth client_id and client_secret before connecting",
            )
        redirect_uri = self.oauth_redirect_uri(request.provider)
        state = self._sign_oauth_state(
            scope.actor.id, request.company_id, request.provider, request.redirect_path
        )
        url = OAuthTokenBroker.authorize_url(request.provider, config, redirect_uri, state)
        return {
            "authorization_url": url,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }

    async def oauth_callback(
        self,
        db: AsyncSession,
        *,
        scope: AccessScope,
        provider: str,
        code: str,
        state: str,
    ) -> tuple[str, dict[str, Any]]:
        """Exchange the code, store the refresh token and redirect back to the console.

        Returns the browser redirect target with sanitized query parameters; the
        access token is used in-process and never persisted.
        """
        self._require_staff(scope)
        if provider not in ("gmail_oauth", "outlook_oauth"):
            raise HTTPException(status_code=422, detail="Unsupported OAuth provider")
        company_id, redirect = self._verify_oauth_state(
            state, actor_id=scope.actor.id, provider=provider
        )
        loaded = await self._load_credentials(db, company_id, provider, fallback=False)
        if loaded is None:
            raise HTTPException(
                status_code=422,
                detail="Save the OAuth client credentials before connecting",
            )
        config_id, _config_company, config = loaded
        broker = OAuthTokenBroker(dict(config), provider=provider)
        try:
            value = await broker.exchange_code(code, self.oauth_redirect_uri(provider))
        except IntegrationProviderError as exc:
            await self.repository.record_connection_test(db, config_id, "failed", exc.code)
            query = urlencode({"integration_oauth": "failed", "provider": provider})
            return f"{redirect}?{query}", {"provider": provider, "status": "failed"}
        merged = {**config}
        if value.get("refresh_token"):
            merged["refresh_token"] = str(value["refresh_token"])
        self._require_complete(provider, merged)
        ciphertext = self.cipher.encrypt_json(
            merged, associated_data=config_aad(company_id, provider)
        )
        await self.repository.save_config(db, company_id, provider, ciphertext, True, False)
        await self.repository.record_connection_test(db, config_id, "configured", None)
        query = urlencode({"integration_oauth": "connected", "provider": provider})
        return f"{redirect}?{query}", {"provider": provider, "status": "configured"}

    async def send_customer_sms(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_staff(scope)
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        customer = await db.execute(
            select(Customer.mobile).where(Customer.id == complaint.customer_id)
        )
        mobile = customer.one_or_none()
        contact = mobile[0] if mobile else None
        if not contact:
            raise HTTPException(status_code=409, detail="Customer mobile number is unavailable")
        payload = {"recipient": contact, "body": body[:480]}
        return await self._queue_message(
            db,
            scope,
            complaint,
            channel=CommunicationChannel.SMS,
            body=body[:480],
            job_kind="send_sms",
            key=idempotency_key,
            payload=payload,
        )

    async def create_api_key(
        self,
        db: AsyncSession,
        *,
        permissions: set[str],
        all_companies: bool,
        company_ids: list[UUID],
        expires_at: datetime | None,
    ) -> dict[str, Any]:
        prefix = "ipk_" + secrets.token_urlsafe(12)
        secret = secrets.token_urlsafe(32)
        token = f"{prefix}.{secret}"
        key_hash = hashlib.sha256(secret.encode("ascii")).digest()
        try:
            key_id = await self.repository.create_api_key(
                db,
                prefix=prefix,
                key_hash=key_hash,
                permissions=sorted(permissions),
                all_companies=all_companies,
                company_ids=company_ids,
                expires_at=expires_at,
            )
        except Exception as exc:
            raise self._job_error(exc) from exc
        return {"id": key_id, "prefix": prefix, "token": token, "expires_at": expires_at}

    async def list_api_keys(self, db: AsyncSession) -> list[dict[str, Any]]:
        return await self.repository.list_api_keys(db)

    async def revoke_api_key(self, db: AsyncSession, key_id: UUID) -> bool:
        return await self.repository.revoke_api_key(db, key_id)

    async def status(self, db: AsyncSession, job_id: UUID) -> dict[str, Any]:
        result = await self.repository.job_status(db, job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Integration job unavailable")
        return result

    async def dead_letters(self, db: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
        return await self.repository.dead_letters(db, limit)

    async def requeue_dead_letter(self, db: AsyncSession, job_id: UUID) -> None:
        if not await self.repository.requeue_dead_letter(db, job_id):
            raise HTTPException(status_code=404, detail="Dead-letter job unavailable")

    async def receive_webhook(
        self,
        db: AsyncSession,
        *,
        company_id: UUID,
        provider: str,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        config_row = await self.repository.webhook_config(db, company_id, provider)
        if config_row is None:
            raise HTTPException(status_code=404, detail="Webhook integration not connected")
        config_company_id, ciphertext = config_row
        try:
            config = self.cipher.decrypt_json(
                ciphertext, associated_data=config_aad(config_company_id, provider)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503, detail="Webhook integration configuration is invalid"
            ) from exc
        twilio_signature = (
            headers.get("x-twilio-signature") if provider in {"twilio", "twilio_sms"} else None
        )
        twilio_fields: dict[str, list[str]] | None = None
        if twilio_signature:
            try:
                twilio_fields = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail="Twilio body must be form encoded"
                ) from exc
            callback_urls = [
                str(config.get(key, ""))
                for key in ("status_callback_url", "sms_status_callback_url", "incoming_url")
                if config.get(key)
            ]
            valid = any(
                verify_twilio_signature(
                    candidate, twilio_fields, twilio_signature, str(config.get("auth_token", ""))
                )
                for candidate in callback_urls
            )
        else:
            secret_field = "app_secret" if provider == "whatsapp_cloud" else "webhook_secret"
            secret = str(config.get(secret_field, ""))
            if not secret:
                raise HTTPException(
                    status_code=404, detail="Webhook signature key is not configured"
                )
            timestamp = headers.get("x-webhook-timestamp") or headers.get("x-insurance-timestamp")
            signature = headers.get("x-webhook-signature") or headers.get("x-insurance-signature")
            if provider == "whatsapp_cloud":
                signature = headers.get("x-hub-signature-256")
                valid = verify_raw_hmac(raw_body, signature, secret)
            else:
                valid = verify_timestamped_hmac(
                    raw_body,
                    timestamp,
                    signature,
                    secret,
                    now=int(datetime.now(UTC).timestamp()),
                )
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        if twilio_fields is not None:
            document: dict[str, Any] | list[Any] = {
                key: values[-1] for key, values in twilio_fields.items() if values
            }
        else:
            try:
                document = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(
                    status_code=422, detail="Webhook body must be valid JSON"
                ) from exc
        if not isinstance(document, (dict, list)) or (
            isinstance(document, list) and provider != "email_webhook"
        ):
            raise HTTPException(status_code=422, detail="Webhook body must be an object")
        events = self._normalize_webhook(provider, document)
        queued_ids: list[UUID] = []
        for job_kind, event_id, payload in events:
            idem = self._inbound_digest(company_id, provider, event_id)
            digest = self._payload_digest(payload)
            encrypted = self.cipher.encrypt_json(payload, associated_data=job_aad(idem))
            try:
                result = await self.repository.enqueue_inbound(
                    db, company_id, provider, job_kind, encrypted, idem, digest
                )
            except Exception as exc:
                raise self._job_error(exc) from exc
            queued_ids.append(result["job_id"])
        return {"accepted": len(queued_ids), "job_ids": queued_ids}

    def _normalize_webhook(
        self, provider: str, document: dict[str, Any] | list[Any]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        if provider == "email_webhook":
            candidates = document if isinstance(document, list) else [document]
            email_events: list[tuple[str, str, dict[str, Any]]] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                event_name = str(candidate.get("event") or candidate.get("status") or "").lower()
                provider_message_id = str(
                    candidate.get("provider_message_id") or candidate.get("sg_message_id") or ""
                )
                delivery_status = (
                    "delivered"
                    if event_name in {"delivered", "processed", "open", "click"}
                    else "failed"
                    if event_name in {"bounce", "bounced", "dropped", "blocked", "failed"}
                    else None
                )
                if delivery_status and provider_message_id:
                    if ".filter" in provider_message_id:
                        provider_message_id = provider_message_id.split(".filter", 1)[0]
                    event_id = str(
                        candidate.get("event_id")
                        or candidate.get("sg_event_id")
                        or f"{provider_message_id}:{event_name}:{candidate.get('timestamp', '')}"
                    )[:256]
                    email_events.append(
                        (
                            "email_status",
                            event_id,
                            {
                                "provider_message_id": provider_message_id[:256],
                                "status": delivery_status,
                            },
                        )
                    )
                    continue
                event_id = str(candidate.get("event_id") or candidate.get("message_id") or "")[:256]
                body = str(candidate.get("text") or candidate.get("body") or "")[:5000]
                subject = str(candidate.get("subject") or "")[:500]
                ticket_no = str(
                    candidate.get("ticket_no") or extract_ticket_no(subject, body) or ""
                ).upper()
                if event_id and ticket_no and body:
                    email_events.append(
                        (
                            "inbound_email",
                            event_id,
                            {
                                "ticket_no": ticket_no,
                                "external_message_id": event_id,
                                "sender": str(candidate.get("from") or "")[:254],
                                "body": body,
                            },
                        )
                    )
            if not email_events:
                raise HTTPException(
                    status_code=422,
                    detail="Email webhook requires an inbound message or delivery event",
                )
            return email_events
        if provider == "whatsapp_cloud":
            if not isinstance(document, dict):
                raise HTTPException(status_code=422, detail="WhatsApp event must be an object")
            whatsapp_events: list[tuple[str, str, dict[str, Any]]] = []
            try:
                for entry in document.get("entry", []):
                    for change in entry.get("changes", []):
                        value = change.get("value", {})
                        for message in value.get("messages", []):
                            event_id = str(message.get("id") or "")[:256]
                            body = str(message.get("text", {}).get("body", ""))[:5000]
                            sender = str(message.get("from") or "")
                            ticket_no = (
                                extract_ticket_no(body)
                                or str(message.get("context", {}).get("ticket_no", "")).upper()
                            )
                            if not event_id or not ticket_no or not body or not sender.isdigit():
                                continue
                            whatsapp_events.append(
                                (
                                    "inbound_whatsapp",
                                    event_id,
                                    {
                                        "ticket_no": ticket_no,
                                        "external_message_id": event_id,
                                        "sender": f"+{sender}"[:32],
                                        "body": body,
                                    },
                                )
                            )
                        for status in value.get("statuses", []):
                            event_id = str(status.get("id") or "")[:256]
                            status_name = str(status.get("status") or "")
                            if event_id and status_name in {"delivered", "read"}:
                                status_event = (
                                    f"{event_id}:{status_name}:{status.get('timestamp', '')}"
                                )
                                whatsapp_events.append(
                                    (
                                        "whatsapp_status",
                                        status_event[:256],
                                        {"provider_message_id": event_id, "status": status_name},
                                    )
                                )
            except (AttributeError, TypeError):
                raise HTTPException(
                    status_code=422, detail="WhatsApp event structure is invalid"
                ) from None
            if not whatsapp_events:
                raise HTTPException(
                    status_code=422, detail="No supported WhatsApp messages or statuses found"
                )
            return whatsapp_events
        if provider == "twilio_sms":
            if not isinstance(document, dict):
                raise HTTPException(status_code=422, detail="SMS event must be an object")
            provider_message_id = str(document.get("MessageSid") or document.get("sid") or "")[:256]
            status_name = str(
                document.get("MessageStatus") or document.get("SmsStatus") or ""
            ).lower()
            known = {"queued", "sending", "sent", "delivered", "undelivered", "failed", "read"}
            if status_name in known:
                if not provider_message_id:
                    raise HTTPException(status_code=422, detail="SMS status event id is missing")
                stamp = str(document.get("timestamp") or "")
                status_event = f"{provider_message_id}:{status_name}:{stamp}"
                return [
                    (
                        "sms_status",
                        status_event[:256],
                        {"provider_message_id": provider_message_id, "status": status_name},
                    )
                ]
            body = str(document.get("Body") or "")[:5000]
            sender = str(document.get("From") or "")
            ticket_no = extract_ticket_no(body) or ""
            event_id = provider_message_id or str(document.get("event_id") or "")[:256]
            if not event_id or not ticket_no or not body or not sender:
                raise HTTPException(
                    status_code=422,
                    detail="SMS webhook requires message id, ticket number and body",
                )
            return [
                (
                    "inbound_sms",
                    event_id,
                    {
                        "ticket_no": ticket_no.upper(),
                        "external_message_id": event_id,
                        "sender": sender[:32],
                        "body": body,
                    },
                )
            ]
        if provider in {"mock_voice", "twilio", "vonage"}:
            if not isinstance(document, dict):
                raise HTTPException(status_code=422, detail="Voice event must be an object")
            provider_call_id = str(
                document.get("provider_call_id")
                or document.get("CallSid")
                or document.get("uuid")
                or ""
            )[:256]
            status = str(document.get("status") or document.get("CallStatus") or "").lower()
            timestamp = document.get("timestamp") or document.get("Timestamp") or ""
            if (
                status
                in {
                    "ringing",
                    "answered",
                    "in-progress",
                    "in_progress",
                    "completed",
                    "failed",
                    "busy",
                    "no-answer",
                    "canceled",
                }
                and provider_call_id
            ):
                event_id = str(
                    document.get("event_id")
                    or document.get("SequenceNumber")
                    or f"{provider_call_id}:{status}:{timestamp}"
                )[:256]
                return [
                    (
                        "voice_status",
                        event_id,
                        {
                            "provider_call_id": provider_call_id,
                            "status": status,
                            "outcome": str(document.get("outcome") or status)[:80],
                        },
                    )
                ]
            event_id = str(document.get("event_id") or provider_call_id)[:256]
            ticket_no = str(
                document.get("ticket_no")
                or extract_ticket_no(
                    str(document.get("text") or document.get("SpeechResult") or "")
                )
                or ""
            ).upper()
            caller_value = (
                document.get("caller") or document.get("from") or document.get("From") or ""
            )
            if isinstance(caller_value, dict):
                caller_value = caller_value.get("number", "")
            if not event_id or not ticket_no:
                raise HTTPException(
                    status_code=422, detail="Voice webhook requires event ID and ticket number"
                )
            voice_payload: dict[str, Any] = {
                "provider_call_id": provider_call_id or event_id,
                "ticket_no": ticket_no,
                "consent_confirmed": document.get("consent_confirmed") is True,
                "request_human": document.get("request_human") is True,
                "caller": str(caller_value)[:32],
                "text": str(document.get("transcript") or "")[:8000],
            }
            return [("inbound_voice", event_id, voice_payload)]
        raise HTTPException(status_code=422, detail="Unsupported inbound provider")
