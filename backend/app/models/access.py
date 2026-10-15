"""RBAC, scoped access and immutable access-change log mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import InsuranceType, OwnershipLevel, PermissionEffect, StaffStatus
from app.models.base import Base
from app.models.identity import INSURANCE_ENUM

STAFF_ENUM = ENUM(
    StaffStatus,
    name="staff_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
EFFECT_ENUM = ENUM(
    PermissionEffect,
    name="permission_effect",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)
OWNERSHIP_ENUM = ENUM(
    OwnershipLevel,
    name="ownership_level",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[StaffStatus] = mapped_column(STAFF_ENUM, server_default="invited")
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    two_factor_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    totp_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    last_login_at: Mapped[datetime | None]
    token_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Permission(Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    resource: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str] = mapped_column(Text, server_default="")
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_code: Mapped[str] = mapped_column(ForeignKey("permissions.code"), primary_key=True)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"), primary_key=True)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)


class UserPermissionOverride(Base):
    __tablename__ = "user_permission_overrides"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"), primary_key=True)
    permission_code: Mapped[str] = mapped_column(ForeignKey("permissions.code"), primary_key=True)
    effect: Mapped[PermissionEffect] = mapped_column(EFFECT_ENUM)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, unique=True)
    lead_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"), primary_key=True)


class UserCompanyScope(Base):
    __tablename__ = "user_company_scope"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    all_companies: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class UserInsuranceTypeScope(Base):
    __tablename__ = "user_insurance_type_scope"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"), primary_key=True)
    insurance_type: Mapped[InsuranceType] = mapped_column(INSURANCE_ENUM, primary_key=True)


class UserOwnershipLevel(Base):
    __tablename__ = "user_ownership_level"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"), primary_key=True)
    level: Mapped[OwnershipLevel] = mapped_column(OWNERSHIP_ENUM)


class AccessChangeLog(Base):
    __tablename__ = "access_change_log"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    actor_staff_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    target_user_id: Mapped[UUID] = mapped_column(ForeignKey("staff_users.id"))
    change_type: Mapped[str] = mapped_column(Text)
    before_state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    after_state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
