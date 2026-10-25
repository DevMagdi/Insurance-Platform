"""Multi-tenancy root: the ``tenants`` table and the default tenant.

Every CRM and insurance-operations table carries ``tenant_id`` from day
one (spec rule: every entity is multi-tenant). The single existing
brokerage deployment is the default tenant seeded by migration 039; new
tenants are ordinary rows in ``tenants``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# The brokerage that already runs on this platform (seeded by migration 039).
DEFAULT_TENANT_ID = UUID("00000000-0000-4000-8000-0000000000a1")
TENANT_DEFAULT_SQL = text("'00000000-0000-4000-8000-0000000000a1'::uuid")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
