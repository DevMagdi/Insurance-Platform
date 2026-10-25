"""Unified CRM mappings — the platform's only CRM (``crm_*`` tables).

Migration 040 introduced the isolated import-driven module; migration
041 unified the B2B sales pipeline (formerly ``leads`` /
``pipeline_stages`` / ``lead_activities``, now dropped) into these same
tables: one lead model (person + optional company), tenant-customisable
pipeline stages, scheduled tasks/follow-ups with Celery reminders, the
Excel import ledger, and the activity timeline. See
``docs/CRM_MODULE.md``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.tenancy import TENANT_DEFAULT_SQL as _TENANT_DEFAULT


class CrmPipelineStage(Base):
    """A tenant-customisable sales pipeline stage (حالات قابلة للتخصيص)."""

    __tablename__ = "crm_pipeline_stages"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    code: Mapped[str] = mapped_column(Text)
    name_ar: Mapped[str] = mapped_column(Text)
    name_en: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_won: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_lost: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_converted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmImportBatch(Base):
    __tablename__ = "crm_import_batches"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    file_name: Mapped[str] = mapped_column(Text)
    uploaded_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    total_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    success_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, server_default="completed")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmLead(Base):
    __tablename__ = "crm_leads"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    # Human reference carried over from the unified B2B leads (LEAD-2026-…).
    lead_no: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default="manual")
    # Pipeline position: a code into crm_pipeline_stages for this tenant,
    # so tenants can add/rename stages without DDL. Terminal-ness (and
    # therefore `closed_at`) comes from the stage's won/lost/converted flag.
    stage_code: Mapped[str] = mapped_column(Text, server_default="new")
    priority: Mapped[str] = mapped_column(Text, server_default="normal")
    insurance_type: Mapped[str] = mapped_column(Text, server_default="general")
    insurer_company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    expected_premium: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    converted_corporate_client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("corporate_clients.id")
    )
    converted_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    # Denormalised earliest open task due time; the Tasks tab and the
    # Celery reminder job read it for due badges.
    next_followup_at: Mapped[datetime | None]
    import_batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("crm_import_batches.id"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmTask(Base):
    """A scheduled call/meeting/email/WhatsApp/task against a lead."""

    __tablename__ = "crm_tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("crm_leads.id"))
    type: Mapped[str] = mapped_column(Text, server_default="task")
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    completed_at: Mapped[datetime | None]
    # Stamped by the Celery beat reminder job when the task falls due.
    reminder_sent_at: Mapped[datetime | None]
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmImportError(Base):
    __tablename__ = "crm_import_errors"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("crm_import_batches.id"))
    row_number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmActivity(Base):
    __tablename__ = "crm_activities"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("crm_leads.id"))
    # Set when the timeline entry was produced by a task lifecycle event.
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("crm_tasks.id"))
    type: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
