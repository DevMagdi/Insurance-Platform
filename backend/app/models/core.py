"""Phase 2 sessions, invitations, idempotency, upload reservations and outbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import MessageVisibility
from app.models.base import Base
from app.models.tickets import VISIBILITY_ENUM


class SecuritySettings(Base):
    __tablename__ = "security_settings"

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    staff_2fa_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    subject_id: Mapped[UUID]
    subject_kind: Mapped[str] = mapped_column(Text)
    refresh_digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    family_id: Mapped[UUID]
    rotated_from_id: Mapped[UUID | None] = mapped_column(ForeignKey("auth_sessions.id"))
    token_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StaffInvitation(Base):
    __tablename__ = "staff_invitations"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    staff_user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    token_digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]


class ComplaintIdempotency(Base):
    __tablename__ = "complaint_idempotency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at", "ticket_no"],
            ["complaint_keys.id", "complaint_keys.created_at", "complaint_keys.ticket_no"],
        ),
    )

    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    key_digest: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary)
    complaint_id: Mapped[UUID | None]
    complaint_created_at: Mapped[datetime | None]
    ticket_no: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]


class AttachmentUpload(Base):
    __tablename__ = "attachment_uploads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    uploaded_by_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    uploaded_by_customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"))
    visibility: Mapped[MessageVisibility] = mapped_column(VISIBILITY_ENUM, server_default="public")
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    declared_mime: Mapped[str] = mapped_column(String(128))
    expected_checksum_sha256: Mapped[str | None] = mapped_column(Text)
    max_size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, server_default="reserved")
    expires_at: Mapped[datetime]
    finalized_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InsurerOutbox(Base):
    __tablename__ = "insurer_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    recipient_email: Mapped[str] = mapped_column(Text)
    ticket_no: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    complaint_id: Mapped[UUID]
    complaint_created_at: Mapped[datetime]
    recipient_customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
