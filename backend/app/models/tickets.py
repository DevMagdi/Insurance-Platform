"""Complaint identity, timeline, conversation, attachments and immutable audit mappings."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, INET, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AttachmentScanStatus,
    CommunicationChannel,
    ComplaintPriority,
    ComplaintStatus,
    InsuranceType,
    MessageDirection,
    MessageVisibility,
)
from app.models.base import Base
from app.models.identity import INSURANCE_ENUM

STATUS_ENUM = ENUM(
    ComplaintStatus,
    name="complaint_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
PRIORITY_ENUM = ENUM(
    ComplaintPriority,
    name="complaint_priority",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
VISIBILITY_ENUM = ENUM(
    MessageVisibility,
    name="message_visibility",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
CHANNEL_ENUM = ENUM(
    CommunicationChannel,
    name="communication_channel",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
DIRECTION_ENUM = ENUM(
    MessageDirection,
    name="message_direction",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
SCAN_ENUM = ENUM(
    AttachmentScanStatus,
    name="attachment_scan_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("categories.id"))
    name_en: Mapped[str] = mapped_column(Text)
    name_ar: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ComplaintKey(Base):
    __tablename__ = "complaint_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    created_at: Mapped[datetime]
    ticket_no: Mapped[str] = mapped_column(Text, unique=True)


class Complaint(Base):
    __tablename__ = "complaints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "created_at", "ticket_no"],
            ["complaint_keys.id", "complaint_keys.created_at", "complaint_keys.ticket_no"],
        ),
        ForeignKeyConstraint(
            ["policy_id", "customer_id", "company_id", "insurance_type", "policy_number"],
            [
                "policies.id",
                "policies.customer_id",
                "policies.company_id",
                "policies.insurance_type",
                "policies.policy_number",
            ],
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(primary_key=True, server_default=func.now())
    ticket_no: Mapped[str] = mapped_column(Text)
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"))
    company_id: Mapped[UUID] = mapped_column(ForeignKey("insurance_companies.id"))
    insurance_type: Mapped[InsuranceType] = mapped_column(INSURANCE_ENUM)
    policy_id: Mapped[UUID | None] = mapped_column(ForeignKey("policies.id"))
    # Revision 036: a request carries a policy number OR a medical card
    # number; the database CHECK requires at least one of them.
    policy_number: Mapped[str | None] = mapped_column(Text)
    card_number: Mapped[str | None] = mapped_column(Text)
    intake_source: Mapped[str] = mapped_column(Text, server_default="portal")
    intake_reference: Mapped[str | None] = mapped_column(Text)
    corporate_client_id: Mapped[UUID | None] = mapped_column(ForeignKey("corporate_clients.id"))
    subject: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[ComplaintStatus] = mapped_column(STATUS_ENUM, server_default="new")
    priority: Mapped[ComplaintPriority] = mapped_column(PRIORITY_ENUM, server_default="normal")
    category_id: Mapped[UUID | None] = mapped_column(ForeignKey("categories.id"))
    sentiment: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[CommunicationChannel] = mapped_column(CHANNEL_ENUM, server_default="portal")
    assigned_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    team_id: Mapped[UUID | None] = mapped_column(ForeignKey("teams.id"))
    sla_due_at: Mapped[datetime | None]
    # DB-managed fields must be omitted from customer INSERTs: app_runtime is
    # intentionally not granted INSERT on these columns, even to write NULL.
    response_due_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    sla_breached_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    # Revision 012: at-risk alerting and response-clock bookkeeping.
    sla_alert_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    sla_alerted_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    first_response_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    response_breached_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    merged_into_id: Mapped[UUID | None] = mapped_column(server_default=text("NULL"))
    merged_into_created_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(server_default=text("NULL"))
    reopened_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    csat_score: Mapped[int | None] = mapped_column(SmallInteger, server_default=text("NULL"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, coalesce(subject, '') || ' ' || "
            "coalesce(description, ''))",
            persisted=True,
        ),
    )


class ComplaintEvent(Base):
    __tablename__ = "complaint_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"], ["complaints.id", "complaints.created_at"]
        ),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    occurred_at: Mapped[datetime] = mapped_column(primary_key=True, server_default=func.now())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    event_type: Mapped[str] = mapped_column(Text)
    from_status: Mapped[ComplaintStatus | None] = mapped_column(STATUS_ENUM)
    to_status: Mapped[ComplaintStatus | None] = mapped_column(STATUS_ENUM)
    actor_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    actor_customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"))
    visibility: Mapped[MessageVisibility] = mapped_column(VISIBILITY_ENUM, server_default="public")
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )


class ComplaintMessage(Base):
    __tablename__ = "complaint_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"], ["complaints.id", "complaints.created_at"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[MessageVisibility] = mapped_column(VISIBILITY_ENUM, server_default="public")
    channel: Mapped[CommunicationChannel] = mapped_column(CHANNEL_ENUM, server_default="portal")
    direction: Mapped[MessageDirection] = mapped_column(DIRECTION_ENUM, server_default="inbound")
    sender_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    sender_customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"))
    external_message_id: Mapped[str | None] = mapped_column(Text)
    sender_external: Mapped[str | None] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(Text, server_default="sent")
    delivered_at: Mapped[datetime | None]
    read_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Attachment(Base):
    __tablename__ = "attachments"
    # Pending files are deliberately invisible to SELECT RLS. INSERT RETURNING
    # would therefore fail; generate keys locally and skip server-default reads.
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"], ["complaints.id", "complaints.created_at"]
        ),
        ForeignKeyConstraint(
            ["message_id", "complaint_id", "complaint_created_at"],
            [
                "complaint_messages.id",
                "complaint_messages.complaint_id",
                "complaint_messages.complaint_created_at",
            ],
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid()
    )
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    message_id: Mapped[UUID | None]
    uploaded_by_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    uploaded_by_customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"))
    visibility: Mapped[MessageVisibility] = mapped_column(VISIBILITY_ENUM, server_default="public")
    # Deprecated object-storage locator: nullable and unused since revision 24.
    storage_key: Mapped[str | None] = mapped_column(Text, unique=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    original_filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(Text)
    scan_status: Mapped[AttachmentScanStatus] = mapped_column(SCAN_ENUM, server_default="pending")
    upload_id: Mapped[UUID | None] = mapped_column(ForeignKey("attachment_uploads.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC), server_default=func.now()
    )


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"], ["complaints.id", "complaints.created_at"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    recipient_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    recipient_customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"))
    complaint_id: Mapped[UUID | None]
    complaint_created_at: Mapped[datetime | None]
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    read_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CallLog(Base):
    __tablename__ = "call_logs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"], ["complaints.id", "complaints.created_at"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    direction: Mapped[MessageDirection] = mapped_column(DIRECTION_ENUM)
    provider_call_id: Mapped[str | None] = mapped_column(Text)
    transcript_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    recording_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    recording_storage_key: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime | None]
    sender_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    scheduled_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(Text, server_default="completed")
    provider: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]


class AuditLog(Base):
    __tablename__ = "audit_logs"
    # Staff may create their own audit trail without audit.view, but must not
    # SELECT it. Avoid INSERT RETURNING so PostgreSQL enforces only INSERT RLS.
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"], ["complaints.id", "complaints.created_at"]
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid()
    )
    created_at: Mapped[datetime] = mapped_column(
        primary_key=True, default=lambda: datetime.now(UTC), server_default=func.now()
    )
    actor_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[UUID | None]
    complaint_id: Mapped[UUID | None]
    complaint_created_at: Mapped[datetime | None]
    before_state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    after_state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    request_id: Mapped[UUID | None]
    ip_address: Mapped[str | None] = mapped_column(INET)


class AiInsight(Base):
    __tablename__ = "ai_insights"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(Text)
    payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    provider_model: Mapped[str | None] = mapped_column(Text)
    integration_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("integration_jobs.id"))
    review_status: Mapped[str] = mapped_column(Text, server_default="suggested")
    reviewed_by_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DuplicateCandidate(Base):
    __tablename__ = "duplicate_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["first_complaint_id", "first_complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
        ForeignKeyConstraint(
            ["second_complaint_id", "second_complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    first_complaint_id: Mapped[UUID]
    first_complaint_created_at: Mapped[datetime]
    second_complaint_id: Mapped[UUID]
    second_complaint_created_at: Mapped[datetime]
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    decision: Mapped[str] = mapped_column(Text, server_default="pending")
    reviewed_by_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CsatResponse(Base):
    __tablename__ = "csat_responses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"))
    score: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now())
