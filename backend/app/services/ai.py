"""Deterministic, human-reviewed AI assistance for staff.

The production provider boundary is intentionally local and replaceable: this
module never calls an external model with complaint text, never auto-sends a
reply, and persists only AES-GCM encrypted JSON through ``AiInsightRepository``.
A future hosted model can implement the same ``analyze`` contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.domain.schemas import AiInsightKind
from app.models.tickets import Complaint

if TYPE_CHECKING:
    from app.repositories.ai import AiInsightRepository

PROVIDER_MODEL = "local-rules-v1"

_CATEGORY_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "claim_delay",
        "Claim delay",
        "تأخر المطالبة",
        (
            "delay",
            "delayed",
            "waiting",
            "claim status",
            "not paid",
            "تأخر",
            "متأخر",
            "انتظار",
            "المطالبة",
            "لم يتم الصرف",
        ),
    ),
    (
        "medical_approval",
        "Medical approval",
        "الموافقة الطبية",
        (
            "approval",
            "pre-authorization",
            "hospital",
            "doctor",
            "surgery",
            "موافقة",
            "مستشفى",
            "طبيب",
            "عملية",
            "علاج",
        ),
    ),
    (
        "coverage_policy",
        "Coverage or policy terms",
        "التغطية أو شروط الوثيقة",
        (
            "coverage",
            "covered",
            "policy terms",
            "excluded",
            "exclusion",
            "تغطية",
            "مشمول",
            "استثناء",
            "شروط الوثيقة",
        ),
    ),
    (
        "documents",
        "Documents requested",
        "مستندات مطلوبة",
        (
            "document",
            "documents",
            "upload",
            "attachment",
            "invoice",
            "مستند",
            "مستندات",
            "رفع",
            "مرفق",
            "فاتورة",
        ),
    ),
    (
        "billing_premium",
        "Billing or premium",
        "الفواتير أو القسط",
        (
            "premium",
            "billing",
            "charged",
            "payment",
            "refund",
            "قسط",
            "فوترة",
            "تم خصم",
            "دفع",
            "استرداد",
        ),
    ),
    (
        "service_quality",
        "Service quality",
        "جودة الخدمة",
        (
            "rude",
            "service",
            "call center",
            "no response",
            "خدمة",
            "مركز الاتصال",
            "لا يوجد رد",
            "تعامل",
        ),
    ),
    (
        "cancellation",
        "Cancellation",
        "الإلغاء",
        ("cancel", "cancellation", "terminate", "إلغاء", "انهاء الوثيقة", "إنهاء الوثيقة"),
    ),
)

_NEGATIVE = (
    "angry",
    "bad",
    "delay",
    "delayed",
    "fail",
    "failed",
    "problem",
    "complaint",
    "wrong",
    "refused",
    "reject",
    "rejected",
    "غاضب",
    "سيئ",
    "تأخر",
    "متأخر",
    "مشكلة",
    "شكوى",
    "خطأ",
    "رفض",
    "مرفوض",
)
_POSITIVE = (
    "thank",
    "thanks",
    "resolved",
    "satisfied",
    "good",
    "شكرا",
    "شكراً",
    "تم الحل",
    "راض",
    "جيد",
)
_URGENT = ("urgent", "emergency", "fraud", "legal", "عاجل", "طارئ", "احتيال", "قانوني")


@dataclass(frozen=True)
class AiInsightService:
    repository: AiInsightRepository
    analyzer: LocalAiAnalyzer


@dataclass(frozen=True)
class GeneratedInsight:
    kind: AiInsightKind
    payload: dict[str, Any]
    confidence: Decimal


def _sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?؟。])\s+|\n+", cleaned)
    return [part.strip() for part in parts if part.strip()]


class LocalAiAnalyzer:
    """Explainable bilingual rules; outputs are suggestions for staff review."""

    model = PROVIDER_MODEL

    def analyze(self, complaint: Complaint, kinds: list[AiInsightKind]) -> list[GeneratedInsight]:
        text = f"{complaint.subject}\n{complaint.description}".strip()
        lowered = text.lower()
        classification, classification_confidence = self._classify(lowered)
        sentiment, sentiment_confidence = self._sentiment(lowered)
        result: list[GeneratedInsight] = []
        for kind in kinds:
            if kind == "classification":
                result.append(
                    GeneratedInsight(
                        kind,
                        {
                            "category_code": classification[0],
                            "label_en": classification[1],
                            "label_ar": classification[2],
                            "priority_hint": "urgent"
                            if any(word in lowered for word in _URGENT)
                            else complaint.priority.value,
                            "requires_human_review": True,
                        },
                        classification_confidence,
                    )
                )
            elif kind == "sentiment":
                result.append(
                    GeneratedInsight(
                        kind,
                        {
                            "sentiment": sentiment,
                            "tone": "distressed"
                            if sentiment == "negative"
                            else "calm"
                            if sentiment == "positive"
                            else "neutral",
                            "requires_human_review": True,
                        },
                        sentiment_confidence,
                    )
                )
            elif kind == "summary":
                result.append(
                    GeneratedInsight(
                        kind,
                        {
                            "summary_en": self._summary(text, complaint.subject),
                            "summary_ar": self._summary(text, complaint.subject),
                            "ticket_no": complaint.ticket_no,
                            "requires_human_review": True,
                        },
                        Decimal("0.8200"),
                    )
                )
            elif kind == "reply":
                result.append(
                    GeneratedInsight(
                        kind,
                        {
                            "reply_en": self._reply_en(complaint, classification[1], sentiment),
                            "reply_ar": self._reply_ar(complaint, classification[2], sentiment),
                            "auto_send": False,
                            "requires_human_review": True,
                        },
                        Decimal("0.7600"),
                    )
                )
        return result

    @staticmethod
    def _classify(lowered: str) -> tuple[tuple[str, str, str], Decimal]:
        scored: list[tuple[int, tuple[str, str, str, tuple[str, ...]]]] = []
        for rule in _CATEGORY_RULES:
            score = sum(1 for phrase in rule[3] if phrase in lowered)
            if score:
                scored.append((score, rule))
        if not scored:
            return ("other", "Other", "أخرى"), Decimal("0.5500")
        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0]
        confidence = min(
            Decimal("0.9700"), Decimal("0.6800") + Decimal(best[0]) * Decimal("0.0700")
        )
        return (best[1][0], best[1][1], best[1][2]), confidence

    @staticmethod
    def _sentiment(lowered: str) -> tuple[str, Decimal]:
        negative = sum(1 for phrase in _NEGATIVE if phrase in lowered)
        positive = sum(1 for phrase in _POSITIVE if phrase in lowered)
        if negative == positive:
            return "neutral", Decimal("0.6200")
        if negative > positive:
            return "negative", min(
                Decimal("0.9600"),
                Decimal("0.7000") + Decimal(negative - positive) * Decimal("0.0600"),
            )
        return "positive", min(
            Decimal("0.9600"), Decimal("0.7000") + Decimal(positive - negative) * Decimal("0.0600")
        )

    @staticmethod
    def _summary(text: str, subject: str) -> str:
        sentences = _sentences(text)
        if not sentences:
            return subject[:240]
        keywords = (
            "delay",
            "claim",
            "approval",
            "document",
            "payment",
            "تأخر",
            "مطالبة",
            "موافقة",
            "مستند",
            "دفع",
        )
        ranked = sorted(
            sentences,
            key=lambda sentence: sum(1 for word in keywords if word in sentence.lower()),
            reverse=True,
        )
        chosen: list[str] = []
        total = 0
        for sentence in ranked:
            if total + len(sentence) > 360 and chosen:
                break
            chosen.append(sentence)
            total += len(sentence)
            if len(chosen) >= 2:
                break
        return " ".join(chosen)[:360]

    @staticmethod
    def _reply_en(complaint: Complaint, label: str, sentiment: str) -> str:
        opening = (
            "We are sorry for the inconvenience and understand your concern. "
            if sentiment == "negative"
            else "Thank you for contacting us. "
        )
        return (
            f"{opening}We are reviewing your {label.lower()} complaint "
            f"(ticket {complaint.ticket_no}) and will update you through this ticket "
            "as soon as the review progresses. Please do not share passwords or one-time codes."
        )

    @staticmethod
    def _reply_ar(complaint: Complaint, label: str, sentiment: str) -> str:
        opening = "نأسف للإزعاج ونتفهم قلقك. " if sentiment == "negative" else "شكراً لتواصلك معنا. "
        return (
            f"{opening}نراجع حالياً شكواك المتعلقة بـ{label} "
            f"(رقم التذكرة {complaint.ticket_no}) وسنحدّثك عبر هذه التذكرة فور تقدم المراجعة. "
            "يرجى عدم مشاركة كلمات المرور أو رموز التحقق."
        )
