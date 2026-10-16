"""Strict request schemas; no PII is accepted through untyped dictionaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    CommunicationChannel,
    ComplaintPriority,
    ComplaintStatus,
    InsuranceType,
    OwnershipLevel,
)
from app.domain.permissions import PERMISSIONS
from app.security.national_id import validate_national_id


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Mobile = Field(pattern=r"^\+201[0-9]{9}$")
Ticket = Field(pattern=r"^CMP-[0-9]{4}-[0-9]{6,}$")


class MobileOTPRequest(StrictModel):
    mobile: str = Mobile


class CustomerVerifyRequest(MobileOTPRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")
    national_id: str | None = None
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    whatsapp_available: bool = False

    @field_validator("national_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return validate_national_id(value) if value is not None else None


class StaffLoginRequest(StrictModel):
    email: str = Field(min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=256)


class StaffInviteRequest(StrictModel):
    email: str = Field(min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    full_name: str = Field(min_length=2, max_length=120)


class StaffProfileEdit(StaffInviteRequest):
    """Profile only; roles, scopes and credentials have separate guarded flows."""

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class RoleSpec(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=300)
    permissions: set[str] = Field(default_factory=set, max_length=40)

    @field_validator("permissions")
    @classmethod
    def known_permissions(cls, value: set[str]) -> set[str]:
        from app.domain.permissions import PERMISSIONS

        if not value <= PERMISSIONS:
            raise ValueError("Unknown permission code")
        return value


class RoleClone(StrictModel):
    name: str = Field(min_length=2, max_length=80)


class CompanySpec(StrictModel):
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,59}$")
    name_en: str = Field(min_length=2, max_length=120)
    name_ar: str = Field(min_length=2, max_length=120)
    contact_emails: list[str] = Field(default_factory=list, max_length=10)
    supported_types: set[InsuranceType] = Field(min_length=1, max_length=3)
    is_active: bool = True

    @field_validator("contact_emails")
    @classmethod
    def contact_addresses(cls, values: list[str]) -> list[str]:
        import re

        if any(
            not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) or v != v.lower() or len(v) > 254
            for v in values
        ):
            raise ValueError("Contact emails must be lowercase addresses")
        return values


class CategorySpec(StrictModel):
    company_id: UUID | None = None
    parent_id: UUID | None = None
    name_en: str = Field(min_length=2, max_length=120)
    name_ar: str = Field(min_length=2, max_length=120)
    is_active: bool = True


class SlaRuleSpec(StrictModel):
    company_id: UUID | None = None
    insurance_type: InsuranceType | None = None
    priority: ComplaintPriority | None = None
    response_minutes: int = Field(ge=1, le=525600)
    resolution_minutes: int = Field(ge=1, le=525600)
    business_hours: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("business_hours")
    @classmethod
    def continuous_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            raise ValueError("Business-hours calendars are not implemented; use continuous SLA")
        return value


class TemplateSpec(StrictModel):
    company_id: UUID | None = None
    title: str = Field(min_length=2, max_length=100)
    body_en: str = Field(min_length=2, max_length=2000)
    body_ar: str = Field(min_length=2, max_length=2000)
    is_active: bool = True


class InviteAcceptRequest(StrictModel):
    token: str = Field(min_length=40, max_length=128)
    password: str = Field(min_length=12, max_length=128)


class ChangePasswordRequest(StrictModel):
    challenge: str = Field(min_length=64, max_length=1500)
    password: str = Field(min_length=12, max_length=128)


class ChallengeRequest(StrictModel):
    challenge: str = Field(min_length=64, max_length=1500)


class TotpVerifyRequest(ChallengeRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")


class TotpEnrollRequest(StrictModel):
    challenge: str | None = Field(default=None, min_length=64, max_length=1500)


class TotpConfirmRequest(StrictModel):
    code: str = Field(pattern=r"^[0-9]{6}$")
    challenge: str | None = Field(default=None, min_length=64, max_length=1500)


class GuestTrackRequest(StrictModel):
    national_id: str
    ticket_no: str = Ticket

    @field_validator("national_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_national_id(value)


class ComplaintCreate(StrictModel):
    national_id: str
    mobile: str = Mobile
    company_id: UUID
    insurance_type: InsuranceType
    policy_number: str = Field(min_length=5, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9/_-]*$")
    subject: str = Field(min_length=8, max_length=160)
    description: str = Field(min_length=20, max_length=10000)

    @field_validator("national_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_national_id(value)


class ComplaintFilter(StrictModel):
    status: ComplaintStatus | None = None
    company_id: UUID | None = None
    insurance_type: InsuranceType | None = None
    assigned_agent_id: UUID | None = None
    priority: ComplaintPriority | None = None
    category_id: UUID | None = None
    channel: CommunicationChannel | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    ticket_no: str | None = Field(default=None, max_length=30)
    search: str | None = Field(default=None, min_length=2, max_length=120)
    sort_by: Literal["created_at", "updated_at", "priority", "status"] = "created_at"
    sort_dir: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def date_window(self) -> ComplaintFilter:
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError(f"{name} must include a timezone")
                setattr(self, name, value.astimezone(UTC))
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not follow date_to")
        return self


class ExportRequest(StrictModel):
    kind: Literal["complaints", "analytics"]
    filters: ComplaintFilter

    @model_validator(mode="after")
    def bounded_window(self) -> ExportRequest:
        start, end = self.filters.date_from, self.filters.date_to
        if start is None or end is None or end - start > timedelta(days=366):
            raise ValueError("An export requires a date range of at most 366 days")
        return self


class AssignmentRequest(StrictModel):
    agent_id: UUID
    team_id: UUID | None = None


class EscalationRequest(StrictModel):
    reason: str = Field(min_length=10, max_length=500)


class MergeRequest(StrictModel):
    target_id: UUID


class BulkAction(StrictModel):
    action: Literal["assign", "status"]
    complaint_ids: list[UUID] = Field(min_length=1, max_length=50)
    assignment: AssignmentRequest | None = None
    status: ComplaintStatus | None = None

    @model_validator(mode="after")
    def one_action(self) -> BulkAction:
        if len(set(self.complaint_ids)) != len(self.complaint_ids):
            raise ValueError("Duplicate complaint IDs")
        if self.action == "assign" and (self.assignment is None or self.status is not None):
            raise ValueError("Assignment required for assign action")
        if self.action == "status" and (self.status is None or self.assignment is not None):
            raise ValueError("Status required for status action")
        if self.status in {ComplaintStatus.TRANSFERRED, ComplaintStatus.REOPENED}:
            raise ValueError("Transfer/reopen require their own workflows")
        return self


class SavedViewCreate(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    criteria: ComplaintFilter


class ReplyRequest(StrictModel):
    body: str = Field(min_length=1, max_length=5000)


class RevealReason(StrictModel):
    reason: str = Field(min_length=10, max_length=200)


class StatusChangeRequest(StrictModel):
    status: ComplaintStatus

    @model_validator(mode="after")
    def permitted_status(self) -> StatusChangeRequest:
        if self.status not in {
            ComplaintStatus.ASSIGNED,
            ComplaintStatus.IN_REVIEW,
            ComplaintStatus.RESOLVED,
            ComplaintStatus.TRANSFERRED,
        }:
            raise ValueError("This status transition requires a different workflow")
        return self


class CsatRequest(StrictModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


AiInsightKind = Literal["classification", "sentiment", "summary", "reply"]


def _default_ai_kinds() -> list[AiInsightKind]:
    return ["classification", "sentiment", "summary", "reply"]


class AiInsightGenerateRequest(StrictModel):
    kinds: list[AiInsightKind] = Field(
        default_factory=_default_ai_kinds,
        min_length=1,
        max_length=4,
    )

    @field_validator("kinds")
    @classmethod
    def unique_kinds(cls, value: list[AiInsightKind]) -> list[AiInsightKind]:
        if len(set(value)) != len(value):
            raise ValueError("AI insight kinds must be unique")
        return value


class AiInsightReviewRequest(StrictModel):
    review_status: Literal["accepted", "rejected"]


class NotificationPreferences(StrictModel):
    """Customer delivery choices; channels stay opt-out, never opt-in by surprise."""

    email: bool = True
    whatsapp: bool = True
    sms: bool = True


class CustomerProfileUpdate(StrictModel):
    """Self-service profile edit; identity fields (mobile, national ID) are immutable here."""

    full_name: str = Field(min_length=2, max_length=120)
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    whatsapp_available: bool = False
    notification_preferences: NotificationPreferences = Field(
        default_factory=NotificationPreferences
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None


class AccessSpec(StrictModel):
    role_ids: list[UUID] = Field(default_factory=list, max_length=30)
    allow: list[str] = Field(default_factory=list, max_length=50)
    deny: list[str] = Field(default_factory=list, max_length=50)
    all_companies: bool = False
    company_ids: list[UUID] = Field(default_factory=list, max_length=100)
    insurance_types: list[InsuranceType] = Field(default_factory=list, max_length=3)
    ownership: OwnershipLevel
    team_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check_scope(self) -> AccessSpec:
        if self.all_companies == bool(self.company_ids):
            raise ValueError("Select all_companies or at least one company ID, not both")
        if set(self.allow) & set(self.deny):
            raise ValueError("An override cannot both allow and deny")
        if (set(self.allow) | set(self.deny)) - PERMISSIONS:
            raise ValueError("Unknown permission code")
        return self


class UploadFilePlan(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(pattern=r"^(application/pdf|image/png|image/jpeg)$")
    size_bytes: int = Field(ge=1, le=10_485_760)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("filename")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or any(ord(ch) < 32 for ch in value):
            raise ValueError("Filename must not contain paths or controls")
        return value


class UploadPlan(StrictModel):
    files: list[UploadFilePlan] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def total_limit(self) -> UploadPlan:
        if sum(item.size_bytes for item in self.files) > 25_000_000:
            raise ValueError("Combined uploads exceed the 25 MB limit")
        return self


class SessionView(StrictModel):
    id: UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    current: bool


class TotpPolicyRequest(StrictModel):
    required: bool
