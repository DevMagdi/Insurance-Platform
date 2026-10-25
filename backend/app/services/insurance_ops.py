"""Pure domain logic for insurance operations (policy lifecycle).

Database-free so it is unit-testable offline: installment schedules, the
~5% brokerage commission and settlement state, and the deterministic
insurance-type classifier used when no LLM provider is configured.
Spreadsheet parsing shared with CRM lives in `app.services.tabular`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.domain.enums import InstallmentPlan

CENT = Decimal("0.01")
DEFAULT_COMMISSION_RATE = Decimal("0.05")

# (count, months between installments) per premium plan.
_PLAN_SHAPE: dict[InstallmentPlan, tuple[int, int]] = {
    InstallmentPlan.MONTHLY: (12, 1),
    InstallmentPlan.QUARTERLY: (4, 3),
    InstallmentPlan.SEMI_ANNUAL: (2, 6),
    InstallmentPlan.CASH: (1, 0),
}


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    # Clamp to the last day of the target month (Jan 31 + 1mo -> Feb 28/29).
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (next_month - date(year, month, 1)).days
    return date(year, month, min(day.day, last_day))


@dataclass(frozen=True)
class InstallmentSpec:
    sequence_no: int
    due_date: date
    amount: Decimal


def installment_schedule(
    premium: Decimal, plan: InstallmentPlan, starts_on: date
) -> list[InstallmentSpec]:
    """Split ``premium`` per plan; the final installment absorbs rounding."""
    if premium <= 0:
        raise ValueError("Premium must be positive")
    count, step = _PLAN_SHAPE[plan]
    each = (premium / count).quantize(CENT, rounding=ROUND_HALF_UP)
    specs: list[InstallmentSpec] = []
    allocated = Decimal("0")
    for index in range(count):
        amount = premium - allocated if index == count - 1 else each
        allocated += amount
        specs.append(InstallmentSpec(index + 1, _add_months(starts_on, step * index), amount))
    return specs


def commission_amount(premium: Decimal, rate: Decimal = DEFAULT_COMMISSION_RATE) -> Decimal:
    """Brokerage commission on a premium (spec default ≈5%)."""
    if premium < 0 or rate < 0 or rate > 1:
        raise ValueError("Premium and commission rate must be within range")
    return (premium * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def settlement_status(expected: Decimal, received: Decimal) -> str:
    if received <= 0:
        return "pending"
    if received < expected:
        return "partial"
    return "settled"


_HEALTH_WORDS = ("طبي", "صحي", "مستشفى", "عيادة", "health", "medical", "clinic", "hospital")
_GENERAL_WORDS = ("سيارة", "سيارات", "حريق", "ممتلكات", "general", "motor", "fire", "property")


def classify_insurance_type(text: str) -> str:
    """Deterministic طبي/عام classifier (AI fallback when no LLM is wired).

    Mirrors the ticket auto-classification in the Phase 2 AI spec; returns
    ``both`` when medical and general signals appear together.
    """
    lowered = text.lower()
    health = any(word in lowered for word in _HEALTH_WORDS)
    general = any(word in lowered for word in _GENERAL_WORDS)
    if health and general:
        return "both"
    return "health" if health else "general"
