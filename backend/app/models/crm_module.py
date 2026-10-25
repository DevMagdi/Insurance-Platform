"""Isolated CRM module mappings (``crm_*`` tables, migration 040).

Separate from the B2B sales pipeline in ``app.models.crm``: these are
person-level leads that mostly arrive through Excel imports, tracked with a
fixed status lifecycle (new → contacted → qualified → converted/lost), an
import-batch ledger with per-row errors, and an activity timeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_TENANT_DEFAULT = text("'00000000-0000-4000-8000-0000000000a1'::uuid")


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
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default="manual")
    status: Mapped[str] = mapped_column(Text, server_default="new")
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    import_batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("crm_import_batches.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmImportError(Base):
    __tablename__ = "crm_import_errors"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("crm_import_batches.id"))
    row_number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CrmActivity(Base):
    __tablename__ = "crm_activities"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("crm_leads.id"))
    type: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
