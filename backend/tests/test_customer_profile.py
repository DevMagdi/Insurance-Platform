"""Admin full-profile national ID reveal: permission, audit, migration gates."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db import Actor
from app.domain.access import AccessScope
from app.security.crypto import NationalIdProtector
from app.services.complaints import ComplaintService
from app.settings import Settings

VALID_NID = "29901010112345"
SQL = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "sql"
    / "034_customer_national_id_and_scanner.sql"
)


def make_service() -> tuple[ComplaintService, Mock, SimpleNamespace, AccessScope]:
    protector = NationalIdProtector(b"e" * 32, b"b" * 32)
    repo = Mock()
    repo.reveal_customer_national_id = AsyncMock(return_value=protector.encrypt(VALID_NID))
    repo.audit_customer_reveal = AsyncMock()
    service = ComplaintService(repo, protector, "k" * 64)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock())
    actor = Actor(uuid4(), "staff")
    scope = AccessScope.for_staff(
        actor,
        {
            "id": str(actor.id),
            "all_companies": True,
            "company_ids": [],
            "insurance_types": [],
            "ownership": "all",
            "team_ids": [],
        },
    )
    return service, repo, db, scope


async def test_authorized_staff_reveal_full_national_id_with_audit() -> None:
    service, repo, db, scope = make_service()
    customer_id = uuid4()
    value = await service.reveal_customer_national_id(
        db, scope, customer_id, "Identity verification for case"
    )
    assert value == VALID_NID
    repo.reveal_customer_national_id.assert_awaited_once_with(db, customer_id)
    repo.audit_customer_reveal.assert_awaited_once()
    assert repo.audit_customer_reveal.await_args.kwargs["customer_id"] == customer_id


async def test_reveal_denied_when_sql_gate_returns_no_ciphertext() -> None:
    service, repo, db, scope = make_service()
    repo.reveal_customer_national_id.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.reveal_customer_national_id(db, scope, uuid4(), "Identity verification")
    assert exc.value.status_code == 403
    repo.audit_customer_reveal.assert_not_awaited()


async def test_reveal_requires_reason_and_staff_actor() -> None:
    service, repo, db, scope = make_service()
    with pytest.raises(HTTPException) as exc:
        await service.reveal_customer_national_id(db, scope, uuid4(), "short")
    assert exc.value.status_code == 400
    customer_scope = AccessScope.for_customer(Actor(uuid4(), "customer"))
    with pytest.raises(HTTPException):
        await service.reveal_customer_national_id(
            db, customer_scope, uuid4(), "Identity verification for case"
        )
    repo.reveal_customer_national_id.assert_not_awaited()


def test_migration_gates_reveal_and_adds_last4_and_scanner_probe() -> None:
    sql = SQL.read_text(encoding="utf-8")
    assert "read_customer_profile_national_id_ciphertext" in sql
    assert "complaints.view_full_national_id" in sql
    assert "has_global_scope()" in sql
    assert "'national_id_last4', c.national_id_last4" in sql
    assert "virus_scanner_configured()" in sql
    assert "GRANT EXECUTE ON FUNCTION app_security.public_platform_logo() TO app_worker" in sql
    # Full ID must never be part of the directory payload.
    directory = sql.split("customer_directory()")[1].split("$function$")[0]
    assert "national_id_ciphertext" not in directory


def test_customer_reveal_route_is_registered() -> None:
    from app.api import platform

    paths = {route.path for route in platform.router.routes}
    assert "/api/staff/customers/{customer_id}/national-id:reveal" in paths


def test_settings_fixture_still_constructs() -> None:
    Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
