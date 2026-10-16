"""Insurers, customers and policy identities. Ciphertext is never returned implicitly."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, ForeignKey, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import InsuranceType
from app.models.base import Base

INSURANCE_ENUM = ENUM(
    InsuranceType,
    name="insurance_type",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)


class InsuranceCompany(Base):
    __tablename__ = "insurance_companies"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name_en: Mapped[str] = mapped_column(Text)
    name_ar: Mapped[str] = mapped_column(Text)
    contact_emails: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    supported_types: Mapped[list[InsuranceType]] = mapped_column(
        ARRAY(INSURANCE_ENUM),
        server_default=text("ARRAY['health','general','both']::insurance_type[]"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    full_name: Mapped[str] = mapped_column(Text)
    national_id_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    national_id_blind_index: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    national_id_last4: Mapped[str | None] = mapped_column(Text)
    token_version: Mapped[int] = mapped_column(server_default=text("0"))
    mobile: Mapped[str] = mapped_column(Text)
    whatsapp_available: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    email: Mapped[str | None] = mapped_column(Text)
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text('\'{"email": true, "whatsapp": true, "sms": true}\'::jsonb')
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"))
    company_id: Mapped[UUID] = mapped_column(ForeignKey("insurance_companies.id"))
    insurance_type: Mapped[InsuranceType] = mapped_column(INSURANCE_ENUM)
    policy_number: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[date | None] = mapped_column(Date)
    expires_at: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
