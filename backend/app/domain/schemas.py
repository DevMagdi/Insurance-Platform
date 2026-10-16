"""Strict request schemas; no PII is accepted through untyped dictionaries."""

from __future__ import annotations

import re
import zoneinfo
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
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


def _require_tz(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value


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


class BusinessHoursWindow(StrictModel):
    """One working window; weekday 0=Sunday to 6=Saturday, Cairo-local clock time."""

    weekday: int = Field(ge=0, le=6)
    start: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

    @model_validator(mode="after")
    def ordered(self) -> BusinessHoursWindow:
        if self.end <= self.start:
            raise ValueError("A working window must end after it starts")
        return self


class BusinessHoursCalendar(StrictModel):
    """Optional calendar: an empty object means continuous 24/7 coverage."""

    time_zone: str = Field(min_length=3, max_length=64)
    weekly: list[BusinessHoursWindow] = Field(min_length=1, max_length=21)
    holidays: list[str] = Field(default_factory=list, max_length=60)

    @field_validator("holidays")
    @classmethod
    def valid_holidays(cls, value: list[str]) -> list[str]:
        for item in value:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", item):
                raise ValueError("Holidays must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def known_time_zone(self) -> BusinessHoursCalendar:
        if self.time_zone not in zoneinfo.available_timezones():
            raise ValueError("Unknown IANA time zone")
        return self


class SlaRuleSpec(StrictModel):
    company_id: UUID | None = None
    insurance_type: InsuranceType | None = None
    priority: ComplaintPriority | None = None
    response_minutes: int = Field(ge=1, le=525600)
    resolution_minutes: int = Field(ge=1, le=525600)
    alert_before_minutes: int = Field(default=60, ge=0, le=43200)
    business_hours: BusinessHoursCalendar | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def ordered_targets(self) -> SlaRuleSpec:
        if self.resolution_minutes < self.response_minutes:
            raise ValueError("Resolution target cannot be shorter than the response target")
        return self


class SlaSimulationRequest(StrictModel):
    company_id: UUID | None = None
    insurance_type: InsuranceType
    priority: ComplaintPriority
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return _require_tz(value)


class BusinessHoursTestRequest(StrictModel):
    business_hours: BusinessHoursCalendar | None = None
    minutes: int = Field(ge=1, le=525600)
    from_at: datetime
    company_id: UUID | None = None
    insurance_type: InsuranceType | None = None

    @field_validator("from_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return _require_tz(value)


class ReportScheduleSpec(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    filters: ReportFilter
    cron_expression: str = Field(min_length=9, max_length=60, pattern=r"^[0-9*,/ -]+$")
    time_zone: str = Field(min_length=3, max_length=64)
    recipient_emails: list[str] = Field(min_length=1, max_length=10)
    format: Literal["csv", "xlsx"] = "csv"
    next_run_at: datetime
    is_active: bool = True

    @field_validator("recipient_emails")
    @classmethod
    def deliverable(cls, value: list[str]) -> list[str]:
        unique = sorted({item.strip().lower() for item in value})
        if len(unique) != len(value):
            raise ValueError("Recipient addresses must be unique")
        for item in unique:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", item) or len(item) > 254:
                raise ValueError("Invalid recipient address")
        return unique

    @field_validator("time_zone")
    @classmethod
    def zone(cls, value: str) -> str:
        if value not in zoneinfo.available_timezones():
            raise ValueError("Unknown IANA time zone")
        return value

    @field_validator("next_run_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return _require_tz(value)

    @model_validator(mode="after")
    def bounded_window(self) -> ReportScheduleSpec:
        if not self.recipient_emails:
            raise ValueError("At least one recipient is required")
        return self


class ReportScheduleView(StrictModel):
    """Read model for one owned schedule; filters stay structured."""

    id: UUID
    name: str
    filters: dict[str, Any]
    cron_expression: str
    time_zone: str
    recipient_emails: list[str]
    format: Literal["csv", "xlsx"]
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_state: str | None
    is_active: bool
    created_at: datetime

    @model_validator(mode="after")
    def deliverable_state(self) -> ReportScheduleView:
        if self.next_run_at is not None and self.next_run_at.tzinfo is None:
            raise ValueError("next_run_at must be timezone-aware")
        return self


class IntegrationSpec(StrictModel):
    """Provider settings; secrets are referenced by vault name, never inlined."""

    company_id: UUID | None = None
    provider: Literal["email", "whatsapp", "sms", "ai_voice", "virus_scan"]
    config: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:/-]{3,200}$")
    is_enabled: bool = False

    @field_validator("config")
    @classmethod
    def reference_not_secret(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        for key, setting in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", key):
                raise ValueError("Config keys are lower_snake_case")
            if re.search(r"(secret|token|password|api_key|private|credential|key)", key):
                raise ValueError("Credentials belong in the secret manager, not in config")
            if isinstance(setting, str) and len(setting) > 500:
                raise ValueError("Config values are limited to 500 characters")
        return value

    @model_validator(mode="after")
    def enabled_needs_secret(self) -> IntegrationSpec:
        if self.is_enabled and not self.secret_ref:
            raise ValueError("An enabled integration must reference a stored secret")
        return self


class AssignmentRuleSpec(StrictModel):
    company_id: UUID | None = None
    insurance_type: InsuranceType | None = None
    category_id: UUID | None = None
    priority: ComplaintPriority | None = None
    target_team_id: UUID | None = None
    target_agent_id: UUID | None = None
    strategy: Literal["round_robin", "least_workload", "by_company"] = "least_workload"
    sort_order: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True

    @model_validator(mode="after")
    def reachable_target(self) -> AssignmentRuleSpec:
        if self.target_team_id is None and self.target_agent_id is None:
            raise ValueError("A rule needs a target team, a target agent, or both")
        return self


class AssignmentPreview(StrictModel):
    company_id: UUID
    insurance_type: InsuranceType
    priority: ComplaintPriority = ComplaintPriority.NORMAL
    category_id: UUID | None = None


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


class ReportFilter(ComplaintFilter):
    """Scheduled reports are relative: the window moves with each run."""

    window_days: int = Field(default=7, ge=1, le=366)

    @model_validator(mode="after")
    def relative_window(self) -> ReportFilter:
        if self.date_from is not None or self.date_to is not None:
            raise ValueError("Scheduled reports cannot pin absolute dates")
        return self


class ExportRequest(StrictModel):
    kind: Literal["complaints", "analytics"]
    format: Literal["csv", "xlsx", "pdf"] = "csv"
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


IntegrationProviderName = Literal[
    "smtp",
    "sendgrid",
    "ses",
    "whatsapp_cloud",
    "email_webhook",
    "imap",
    "mock_llm",
    "openai",
    "mock_voice",
    "twilio",
    "vonage",
]


class IntegrationConfigSpec(StrictModel):
    company_id: UUID | None = None
    provider: IntegrationProviderName
    enabled: bool = True
    clear_credentials: bool = False
    credentials: dict[str, str] | None = Field(default=None, max_length=20)

    @field_validator("credentials")
    @classmethod
    def credentials_are_bounded(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        allowed = {
            "host",
            "port",
            "username",
            "password",
            "start_tls",
            "from_email",
            "api_key",
            "access_key_id",
            "secret_access_key",
            "region",
            "access_token",
            "phone_number_id",
            "app_secret",
            "verify_token",
            "api_version",
            "webhook_secret",
            "mailbox",
            "use_ssl",
            "model",
            "base_url",
            "account_sid",
            "auth_token",
            "from_number",
            "api_secret",
            "application_id",
            "private_key",
            "status_callback_url",
        }
        if not value.keys() <= allowed or any(not k or len(v) > 4096 for k, v in value.items()):
            raise ValueError("Unsupported or oversized integration credential")
        return value

    @model_validator(mode="after")
    def provider_configuration(self) -> IntegrationConfigSpec:
        required = {
            "smtp": {"host", "from_email"},
            "sendgrid": {"api_key", "from_email"},
            "ses": {"region", "from_email"},
            "whatsapp_cloud": {"access_token", "phone_number_id", "app_secret"},
            "email_webhook": {"webhook_secret"},
            "imap": {"host", "username", "password"},
            "mock_llm": set(),
            "openai": {"api_key"},
            "mock_voice": set(),
            "twilio": {
                "account_sid",
                "auth_token",
                "from_number",
                "webhook_secret",
                "status_callback_url",
            },
            "vonage": {
                "application_id",
                "private_key",
                "from_number",
                "webhook_secret",
                "status_callback_url",
            },
        }[self.provider]
        credentials = self.credentials or {}
        if self.clear_credentials and (self.enabled or self.credentials is not None):
            raise ValueError("Disconnecting must disable the provider and omit credentials")
        if self.enabled and not required <= credentials.keys() and self.credentials is not None:
            raise ValueError("Required credentials are missing for this provider")
        if self.enabled and required and self.credentials is None:
            raise ValueError("Credentials are required to connect this provider")
        if self.provider in {"email_webhook", "imap"} and self.company_id is None:
            raise ValueError("Inbound email integrations must be company-scoped")
        if "port" in credentials and (
            not credentials["port"].isdigit() or not 1 <= int(credentials["port"]) <= 65535
        ):
            raise ValueError("Provider port must be between 1 and 65535")
        if credentials.get("from_email") and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", credentials["from_email"]
        ):
            raise ValueError("Provider sender email is invalid")
        if credentials.get("base_url") and not credentials["base_url"].startswith("https://"):
            raise ValueError("AI provider endpoint must use HTTPS")
        if credentials.get("status_callback_url"):
            callback = urlsplit(credentials["status_callback_url"])
            if (
                callback.scheme != "https"
                or not callback.hostname
                or callback.username
                or callback.password
                or callback.hostname.lower() in {"localhost", "metadata.google.internal"}
                or callback.hostname.lower().endswith((".local", ".internal"))
            ):
                raise ValueError("Voice status callback must be a public HTTPS URL")
        if credentials.get("mailbox") and not re.fullmatch(
            r"[A-Za-z0-9_./ -]{1,128}", credentials["mailbox"]
        ):
            raise ValueError("IMAP mailbox name is invalid")
        return self


class SendComplaintEmailRequest(StrictModel):
    locale: Literal["ar", "en"] = "en"
    agent_note: str | None = Field(default=None, max_length=2000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def unique_attachments(self) -> SendComplaintEmailRequest:
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("Duplicate attachment IDs")
        return self


class WhatsAppSendRequest(StrictModel):
    body: str = Field(min_length=1, max_length=4096)
    template_name: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,128}$")
    language: Literal["ar", "en"] = "en"
    parameters: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("parameters")
    @classmethod
    def parameter_lengths(cls, value: list[str]) -> list[str]:
        if any(len(item) > 200 for item in value):
            raise ValueError("Template parameters must not exceed 200 characters")
        return value


class AIAnalysisRequest(StrictModel):
    locale: Literal["ar", "en"] = "en"


class AIReviewRequest(StrictModel):
    accept: bool


class VoiceConsentRequest(StrictModel):
    enabled: bool


class VoiceFollowupRequest(StrictModel):
    scheduled_at: datetime
    language: Literal["ar", "en"] = "en"

    @field_validator("scheduled_at")
    @classmethod
    def schedule_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Scheduled calls require a timezone-aware timestamp")
        return value.astimezone(UTC)


class ApiKeyCreate(StrictModel):
    permissions: set[str] = Field(min_length=1, max_length=40)
    all_companies: bool = False
    company_ids: list[UUID] = Field(default_factory=list, max_length=100)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def check_api_key_scope(self) -> ApiKeyCreate:
        if not self.permissions <= PERMISSIONS:
            raise ValueError("Unknown API key permission")
        if self.all_companies == bool(self.company_ids):
            raise ValueError("Select all_companies or one or more company IDs")
        if len(set(self.company_ids)) != len(self.company_ids):
            raise ValueError("Duplicate API key company IDs")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("API key expiry requires a timezone")
            self.expires_at = self.expires_at.astimezone(UTC)
        return self


class WebhookCreate(StrictModel):
    url: str = Field(min_length=8, max_length=2048)
    company_id: UUID | None = None
    event_types: set[Literal["complaint.status_changed", "complaint.message_received"]] = Field(
        min_length=1, max_length=3
    )

    @field_validator("url")
    @classmethod
    def public_https_url(cls, value: str) -> str:
        from ipaddress import ip_address
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise ValueError("Webhook URLs must be public HTTPS endpoints without credentials")
        if host.lower() in {"localhost", "metadata.google.internal"} or host.lower().endswith(
            (".localhost", ".local", ".internal")
        ):
            raise ValueError("Webhook host is not public")
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or address.is_multicast):
            raise ValueError("Webhook IP address must be publicly routable")
        return value
