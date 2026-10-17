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
