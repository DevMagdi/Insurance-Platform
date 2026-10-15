"""SLA, templates, provider configuration, saved views and API credentials."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ComplaintPriority, InsuranceType
from app.models.base import Base
from app.models.identity import INSURANCE_ENUM
from app.models.tickets import PRIORITY_ENUM


class SlaRule(Base):
    __tablename__ = "sla_rules"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    insurance_type: Mapped[InsuranceType | None] = mapped_column(INSURANCE_ENUM)
    priority: Mapped[ComplaintPriority | None] = mapped_column(PRIORITY_ENUM)
    response_minutes: Mapped[int] = mapped_column(Integer)
    resolution_minutes: Mapped[int] = mapped_column(Integer)
    alert_before_minutes: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    business_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CannedReply(Base):
    __tablename__ = "canned_replies"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str] = mapped_column(Text)
    body_en: Mapped[str] = mapped_column(Text)
    body_ar: Mapped[str] = mapped_column(Text)
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    created_by_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SavedFilter(Base):
    __tablename__ = "saved_filters"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    owner_staff_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    name: Mapped[str] = mapped_column(Text)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IntegrationConfig(Base):
    __tablename__ = "integrations_config"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    provider: Mapped[str] = mapped_column(Text)
    config_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_ref: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    url: Mapped[str] = mapped_column(Text)
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    signing_secret_ref: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_delivery_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    owner_staff_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    key_prefix: Mapped[str] = mapped_column(Text, unique=True)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    permission_codes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssignmentRule(Base):
    __tablename__ = "assignment_rules"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    insurance_type: Mapped[InsuranceType | None] = mapped_column(INSURANCE_ENUM)
    category_id: Mapped[UUID | None] = mapped_column(ForeignKey("categories.id"))
    priority: Mapped[ComplaintPriority | None] = mapped_column(PRIORITY_ENUM)
    target_team_id: Mapped[UUID | None] = mapped_column(ForeignKey("teams.id"))
    target_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    strategy: Mapped[str] = mapped_column(Text, server_default="least_workload")
    last_assigned_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("100"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReportSchedule(Base):
    __tablename__ = "report_schedules"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    owner_staff_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    name: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    cron_expression: Mapped[str] = mapped_column(Text)
    time_zone: Mapped[str] = mapped_column(Text, server_default="Africa/Cairo")
    recipient_emails: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    format: Mapped[str] = mapped_column(Text, server_default="csv")
    next_run_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_run_at: Mapped[datetime | None]
    last_state: Mapped[str | None] = mapped_column(Text)
    claim_expires_at: Mapped[datetime | None]


class ReportRun(Base):
    __tablename__ = "report_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    schedule_id: Mapped[UUID] = mapped_column(ForeignKey("report_schedules.id", ondelete="CASCADE"))
    owner_staff_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    attempt: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    state: Mapped[str] = mapped_column(Text, server_default="running")
    row_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]


class AnalyticsRollupState(Base):
    """Single row describing the materialized analytics rollup freshness."""

    __tablename__ = "analytics_rollup_state"

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    last_attempt_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    refreshed_rows: Mapped[int | None] = mapped_column(Integer)


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    owner_staff_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    kind: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text, server_default="csv")
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    private_storage_key: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[datetime | None]


class ExportJobRow(Base):
    __tablename__ = "export_job_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["complaint_id", "complaint_created_at"],
            ["complaints.id", "complaints.created_at"],
            ondelete="CASCADE",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("export_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    complaint_id: Mapped[UUID] = mapped_column(primary_key=True)
    complaint_created_at: Mapped[datetime]
