"""Domain values aligned with PostgreSQL enum labels in 001_schema.sql."""

from enum import StrEnum


class InsuranceType(StrEnum):
    HEALTH = "health"
    GENERAL = "general"
    BOTH = "both"


class ComplaintStatus(StrEnum):
    NEW = "new"
    ASSIGNED = "assigned"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    TRANSFERRED = "transferred"


class ComplaintPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class StaffStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class OwnershipLevel(StrEnum):
    OWN = "own"
    TEAM = "team"
    ALL = "all"


class MessageVisibility(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"


class CommunicationChannel(StrEnum):
    PORTAL = "portal"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    PHONE = "phone"
    AI_VOICE = "ai_voice"
    SMS = "sms"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class AttachmentScanStatus(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"


class PaymentFrequency(StrEnum):
    """Billing cadence of a brokerage contract with a corporate client."""

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semi_annual"
    ANNUAL = "annual"


class ContractStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    TERMINATED = "terminated"


class IntakeSource(StrEnum):
    """Where a request physically entered the platform."""

    PORTAL = "portal"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    PHONE = "phone"
    SMS = "sms"
    STAFF = "staff"
    CORPORATE = "corporate"


class IntakeState(StrEnum):
    PENDING = "pending"
    CREATED = "created"
    LINKED = "linked"
    IGNORED = "ignored"
    FAILED = "failed"


# --- Phase 2 (Sales CRM, issuing, policy lifecycle) ---------------------------
# Stored as checked TEXT (not native enums) so tenants can extend pipeline
# stages without a schema migration; these enums are the API contract.






class InstallmentPlan(StrEnum):
    """Premium billing cadence for a brokerage policy (Phase 2 spec)."""

    MONTHLY = "monthly"  # شهري
    QUARTERLY = "quarterly"  # ربع سنوي
    SEMI_ANNUAL = "semi_annual"  # نصف سنوي
    CASH = "cash"  # كاش (دفعة واحدة)


class InstallmentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    OVERDUE = "overdue"
    WAIVED = "waived"


class IssuingStatus(StrEnum):
    DRAFT = "draft"
    INTERNAL_REVIEW = "internal_review"
    SENT_TO_INSURER = "sent_to_insurer"
    ISSUED = "issued"
    REJECTED = "rejected"


class ChecklistItemStatus(StrEnum):
    PENDING = "pending"
    RECEIVED = "received"
    VERIFIED = "verified"
    REJECTED = "rejected"


class PaymentProviderName(StrEnum):
    FAWRY = "fawry"
    PAYMOB = "paymob"
    MANUAL = "manual"


class ImportKind(StrEnum):
    EMPLOYEES = "employees"
    CLIENTS = "clients"
    LEADS = "leads"


# --- Unified CRM module — the only CRM (crm_* tables, /api/crm) ---------------


class CrmLeadPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class CrmTaskType(StrEnum):
    CALL = "call"
    MEETING = "meeting"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    TASK = "task"


class CrmTaskStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


class CrmLeadSource(StrEnum):
    MANUAL = "manual"
    WHATSAPP = "whatsapp"
    CALL = "call"
    EMAIL = "email"
    PORTAL = "portal"
    IMPORT = "import"
    OTHER = "other"


class CrmActivityType(StrEnum):
    CALL = "call"
    NOTE = "note"
    STAGE_CHANGE = "stage_change"
    TASK = "task"
    CONVERSION = "conversion"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    MEETING = "meeting"
    ASSIGNMENT = "assignment"


class ContractDocumentKind(StrEnum):
    CONTRACT = "contract"
    POLICY = "policy"
    ENDORSEMENT = "endorsement"
    INVOICE = "invoice"
    OTHER = "other"
