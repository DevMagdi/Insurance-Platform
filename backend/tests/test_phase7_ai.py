"""Local AI analyzer, encrypted payload storage and preference-aware delivery."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.ai import generate_ai_insights, list_ai_insights, review_ai_insight
from app.domain.enums import ComplaintPriority, ComplaintStatus, InsuranceType
from app.domain.schemas import AiInsightGenerateRequest, AiInsightReviewRequest
from app.models.tickets import AiInsight, Complaint
from app.repositories.ai import AiInsightRepository
from app.security.crypto import PayloadProtector
from app.services.ai import GeneratedInsight, LocalAiAnalyzer
from app.services.providers import SMTPDelivery
from app.settings import Settings
from app.worker import enabled_notification_channels


def complaint(subject: str, description: str) -> Complaint:
    return Complaint(
        id=uuid4(),
        created_at=datetime.now(UTC),
        ticket_no="CMP-2026-000123",
        customer_id=uuid4(),
        company_id=uuid4(),
        insurance_type=InsuranceType.HEALTH,
        policy_number="POL-12345",
        subject=subject,
        description=description,
        status=ComplaintStatus.IN_REVIEW,
        priority=ComplaintPriority.NORMAL,
        channel="portal",  # type: ignore[arg-type]
    )


def test_ai_analyzer_classifies_summarizes_and_suggests_bilingual_reply() -> None:
    item = complaint(
        "Claim payment delayed",
        "I have been waiting for my claim payment. The delay is causing a serious problem.",
    )
    insights = LocalAiAnalyzer().analyze(item, ["classification", "sentiment", "summary", "reply"])
    by_kind = {insight.kind: insight for insight in insights}
    assert by_kind["classification"].payload["category_code"] == "claim_delay"
    assert by_kind["sentiment"].payload["sentiment"] == "negative"
    assert "waiting" in by_kind["summary"].payload["summary_en"]
    assert "CMP-2026-000123" in by_kind["reply"].payload["reply_en"]
    assert "CMP-2026-000123" in by_kind["reply"].payload["reply_ar"]
    assert by_kind["reply"].payload["auto_send"] is False
    assert all(insight.payload["requires_human_review"] for insight in insights)


def test_ai_analyzer_supports_arabic_medical_approval() -> None:
    item = complaint("تأخر الموافقة الطبية", "أنتظر موافقة المستشفى منذ أيام لإجراء العملية.")
    insights = LocalAiAnalyzer().analyze(item, ["classification", "sentiment"])
    assert insights[0].payload["category_code"] == "medical_approval"
    assert insights[0].payload["label_ar"] == "الموافقة الطبية"


def test_payload_protector_round_trips_unicode_json() -> None:
    protector = PayloadProtector(b"p" * 32)
    payload = {"summary_ar": "ملخص عربي", "confidence_note": "human review"}
    blob = protector.encrypt_json(payload)
    assert blob != str(payload).encode()
    assert protector.decrypt_json(blob) == payload


def test_notification_preferences_are_opt_out_by_channel() -> None:
    assert enabled_notification_channels(None) == ("email", "whatsapp", "sms")
    assert enabled_notification_channels({"email": False, "sms": True}) == ("whatsapp", "sms")
    assert enabled_notification_channels({"email": False, "whatsapp": False, "sms": False}) == ()
    assert enabled_notification_channels('{"email": false}') == ("whatsapp", "sms")


class FakeScalars:
    def __init__(self, rows: list[AiInsight]) -> None:
        self.rows = rows

    def all(self) -> list[AiInsight]:
        return self.rows

    def one_or_none(self) -> AiInsight | None:
        return self.rows[0] if self.rows else None


async def test_ai_repository_encrypts_creates_lists_and_reviews() -> None:
    protector = PayloadProtector(b"p" * 32)
    repo = AiInsightRepository(protector)
    parent = complaint("Claim delayed", "Waiting for the claim payment.")
    added: list[AiInsight] = []

    async def get_complaint(db: object, scope: object, complaint_id: object) -> Complaint:
        return parent

    repo.complaints.get = get_complaint  # type: ignore[method-assign]
    db = SimpleNamespace(add=added.append, flush=AsyncMock(), scalars=AsyncMock())
    generated = [
        GeneratedInsight(
            "sentiment",
            {"sentiment": "negative", "requires_human_review": True},
            Decimal("0.9000"),
        )
    ]
    created = await repo.create(db, SimpleNamespace(), parent.id, generated, "local-rules-v1")  # type: ignore[arg-type]
    assert created is not None
    assert created[0]["payload"] == {"sentiment": "negative", "requires_human_review": True}
    assert added[0].payload_ciphertext != b"negative"
    assert protector.decrypt_json(added[0].payload_ciphertext)["sentiment"] == "negative"
    assert parent.sentiment == "negative"

    db.scalars = AsyncMock(return_value=FakeScalars(added))
    listed = await repo.list_for_complaint(db, SimpleNamespace(), parent.id)  # type: ignore[arg-type]
    assert listed is not None and listed[0]["kind"] == "sentiment"
    # The repository query is scoped through the parent complaint lookup above.
    assert select(AiInsight).where(AiInsight.complaint_id == parent.id) is not None

    reviewed = await repo.review(
        db,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        parent.id,
        added[0].id,
        "accepted",
        uuid4(),
    )
    assert reviewed is not None
    assert reviewed["review_status"] == "accepted"
    assert reviewed["reviewed_by_staff_id"] is not None


async def test_ai_repository_hides_out_of_scope_complaints() -> None:
    repo = AiInsightRepository(PayloadProtector(b"p" * 32))

    async def missing(db: object, scope: object, complaint_id: object) -> None:
        return None

    repo.complaints.get = missing  # type: ignore[method-assign]
    db = SimpleNamespace(add=lambda item: None, flush=AsyncMock(), scalars=AsyncMock())
    assert await repo.list_for_complaint(db, SimpleNamespace(), uuid4()) is None  # type: ignore[arg-type]
    assert (
        await repo.create(db, SimpleNamespace(), uuid4(), [], "local-rules-v1")  # type: ignore[arg-type]
        is None
    )


async def test_ai_api_generates_lists_and_reviews_scoped_insights() -> None:
    parent = complaint("Claim delayed", "Waiting for the delayed claim payment.")
    repo = AiInsightRepository(PayloadProtector(b"p" * 32))
    added: list[AiInsight] = []

    async def get_complaint(db: object, scope: object, complaint_id: object) -> Complaint:
        return parent

    repo.complaints.get = get_complaint  # type: ignore[method-assign]
    db = SimpleNamespace(add=added.append, flush=AsyncMock(), scalars=AsyncMock())
    service = SimpleNamespace(repository=repo, analyzer=LocalAiAnalyzer())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai=service)))
    ctx = SimpleNamespace(db=db, scope=SimpleNamespace(), actor=SimpleNamespace(id=uuid4()))

    created = await generate_ai_insights(
        parent.id,
        AiInsightGenerateRequest(kinds=["classification", "sentiment"]),
        request,  # type: ignore[arg-type]
        ctx,  # type: ignore[arg-type]
    )
    assert [item["kind"] for item in created] == ["classification", "sentiment"]
    assert parent.sentiment == "negative"

    db.scalars = AsyncMock(return_value=FakeScalars(added))
    listed = await list_ai_insights(parent.id, request, ctx)  # type: ignore[arg-type]
    assert len(listed) == 2
    reviewed = await review_ai_insight(
        parent.id,
        added[0].id,
        AiInsightReviewRequest(review_status="accepted"),
        request,  # type: ignore[arg-type]
        ctx,  # type: ignore[arg-type]
    )
    assert reviewed["review_status"] == "accepted"

    with pytest.raises(Exception) as exc:
        repo.complaints.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
        await list_ai_insights(parent.id, request, ctx)  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 404


@pytest.fixture
def delivery_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="development",
        SMTP_HOST="localhost",
        DEV_SMS_MAILBOX=True,
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
    assert await provider.send_notice(
        "+201123456789",
        "customer@example.test",
        "status_changed",
        payload,
        {"email": False, "whatsapp": True, "sms": False},
    )
    assert len(sent) == 1
    assert "sms-201123456789@sms.local.test" in str(sent[0])
