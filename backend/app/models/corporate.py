"""Corporate clients: the companies contracted with the brokerage.

An employee of a contracted company is an ordinary ``Customer`` carrying a
``corporate_client_id``; their requests therefore reuse every existing
workflow, SLA and analytics feature unchanged. What is new here is the
commercial relationship (contract value, payment cadence, renewal date), the
documents attached to it, and the username/password logins that let the client
see its own file and nothing else.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AttachmentScanStatus,
    CommunicationChannel,
    ContractStatus,
    IntakeState,
    PaymentFrequency,
    StaffStatus,
)
from app.models.base import Base

PAYMENT_FREQUENCY_ENUM = ENUM(
    PaymentFrequency,
    name="payment_frequency",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
CONTRACT_STATUS_ENUM = ENUM(
    ContractStatus,
    name="contract_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
INTAKE_STATE_ENUM = ENUM(
    IntakeState,
    name="intake_state",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
STAFF_STATUS_ENUM = ENUM(
    StaffStatus,
    name="staff_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
SCAN_STATUS_ENUM = ENUM(
    AttachmentScanStatus,
    name="attachment_scan_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
CHANNEL_ENUM = ENUM(
    CommunicationChannel,
    name="communication_channel",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)


class CorporateClient(Base):
    __tablename__ = "corporate_clients"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name_ar: Mapped[str] = mapped_column(Text)
    name_en: Mapped[str] = mapped_column(Text)
    tax_id: Mapped[str | None] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    contact_mobile: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    # Revision 039: every client belongs to a brokerage tenant (default tenant
    # for the existing deployment).
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey("tenants.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CorporateContract(Base):
    __tablename__ = "corporate_contracts"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    corporate_client_id: Mapped[UUID] = mapped_column(ForeignKey("corporate_clients.id"))
    contract_no: Mapped[str] = mapped_column(Text, unique=True)
    contract_value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(Text, server_default="EGP")
    payment_frequency: Mapped[PaymentFrequency] = mapped_column(PAYMENT_FREQUENCY_ENUM)
    starts_on: Mapped[date] = mapped_column(Date)
    renews_on: Mapped[date] = mapped_column(Date)
    status: Mapped[ContractStatus] = mapped_column(CONTRACT_STATUS_ENUM, server_default="active")
    headcount: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CorporateContractDocument(Base):
    __tablename__ = "corporate_contract_documents"
    # Pending documents are invisible to client SELECT RLS; avoid INSERT
    # RETURNING for the same reason attachments do.
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    contract_id: Mapped[UUID] = mapped_column(ForeignKey("corporate_contracts.id"))
    title: Mapped[str] = mapped_column(Text)
    doc_kind: Mapped[str] = mapped_column(Text, server_default="contract")
    original_filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(Text)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    scan_status: Mapped[AttachmentScanStatus] = mapped_column(
        SCAN_STATUS_ENUM, server_default="pending"
    )
    visible_to_client: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    uploaded_by_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CorporateUser(Base):
    __tablename__ = "corporate_users"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    corporate_client_id: Mapped[UUID] = mapped_column(ForeignKey("corporate_clients.id"))
    username: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[StaffStatus] = mapped_column(STAFF_STATUS_ENUM, server_default="active")
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Revision 039: the company owner, as opposed to a client contact person.
    is_owner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    token_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RequestIntakeEvent(Base):
    """Durable record of every inbound channel event, matched or not.

    Nothing a customer sends is ever silently dropped: an event that could not
    become a request stays here with its error code for staff triage.
    """

    __tablename__ = "request_intake_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    channel: Mapped[CommunicationChannel] = mapped_column(CHANNEL_ENUM)
    external_event_id: Mapped[str] = mapped_column(Text)
    sender: Mapped[str] = mapped_column(Text)
    sender_name: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, server_default="")
    media_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    state: Mapped[IntakeState] = mapped_column(INTAKE_STATE_ENUM, server_default="pending")
    complaint_id: Mapped[UUID | None]
    complaint_created_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
