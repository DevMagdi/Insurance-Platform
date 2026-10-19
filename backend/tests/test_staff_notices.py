"""Staff notices for customer activity + live directory revision endpoints.

Covers the three guarantees the live console depends on: a customer action
fans a scoped notification out to staff (SQL-side gates asserted statically),
the repository only fans out for customer actors, and the revision endpoints
stay behind the same permission/scope gate as the directory they version.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from app.api import platform, staff
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus, InsuranceType, OwnershipLevel
from app.main import app
from app.models.tickets import Complaint
from app.repositories.complaints import ComplaintRepository

SQL = (
    Path(__file__).resolve().parents[1] / "alembic" / "sql" / "035_staff_notices_live_directory.sql"
)


def _complaint(customer_id) -> Complaint:
    now = datetime.now(UTC)
    return Complaint(
        id=uuid4(),
        created_at=now,
        ticket_no="CMP-2026-000777",
        customer_id=customer_id,
        company_id=uuid4(),
        insurance_type=InsuranceType.HEALTH,
        policy_number="POL-000777",
        subject="Claim delay",
        description="Still waiting for the settlement.",
        status=ComplaintStatus.NEW,
        updated_at=now,
    )


def _body(sql: str, signature: str) -> str:
    """Extract one function body from a SQL revision (``$function$$``-wrapped)."""
    start = sql.index(signature)
    begin = sql.index("$function$", start) + len("$function$")
    end = sql.index("$function$", begin)
    return sql[begin:end]


def _db_for_reply() -> SimpleNamespace:
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock(), scalar=AsyncMock())
    return db


def _scalar(*, ticket: str = "CMP-2026-000777", notice_id=None, staff_notified: int = 2):
    """Session stand-in that answers each definer call with its own type."""
    notice_id = notice_id or uuid4()

    def answer(statement, params=None):
        sql = str(statement)
        if "queue_customer_notice" in sql:
            return notice_id
        if "queue_staff_notice" in sql:
            return staff_notified
        if "issue_ticket_number" in sql:
            return ticket
        return None

    return AsyncMock(side_effect=answer)


def _customer_scope() -> AccessScope:
    return AccessScope.for_customer(Actor(uuid4(), "customer"))


def _staff_scope() -> AccessScope:
    return AccessScope(
        Actor(uuid4(), "staff"),
        company_ids=frozenset({uuid4()}),
        ownership=OwnershipLevel.ALL,
    )


def _calls(db: SimpleNamespace, needle: str) -> list[tuple[str, dict]]:
    """Every definer call whose SQL mentions ``needle``, with its bind params."""
    found = []
    for call in db.scalar.call_args_list + db.execute.call_args_list:
        statement = call.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if needle in sql:
            found.append((sql, dict(call.kwargs) or (call.args[1] if len(call.args) > 1 else {})))
    return found


# --------------------------------------------------------------------------
# Migration contract
# --------------------------------------------------------------------------


def test_notice_fanout_is_definer_gated_and_scope_derived() -> None:
    sql = SQL.read_text(encoding="utf-8")
    assert "CREATE FUNCTION app_security.queue_staff_notice(" in sql
    fanout = _body(sql, "CREATE FUNCTION app_security.queue_staff_notice(")
    # Only a customer action fans out, and only for the two customer kinds.
    assert "app_security.actor_scope() <> 'customer'" in fanout
    assert "p_kind NOT IN ('created', 'reply')" in fanout
    assert "app_security.can_read_complaint_id(p_id, p_created)" in fanout
    # Every candidate is re-judged server-side: permission (deny-wins) + scope.
    assert "user_permission_overrides" in fanout and "effect = 'deny'" in fanout
    assert "app_security.staff_scope_covers(" in fanout
    assert "LIMIT 100" in fanout
    # The console opens the ticket straight from the notice.
    assert "'complaint_id', p_id" in fanout
    assert "INSERT INTO public.notifications" in fanout
    # No caller-supplied recipient: staff ids come from the tables only.
    assert "recipient_staff_id" in fanout
    assert "p_staff_id" not in fanout
    # Execute is granted to the runtime role and revoked from PUBLIC.
    assert (
        "REVOKE ALL ON FUNCTION app_security.queue_staff_notice(uuid, timestamptz, text)"
        " FROM PUBLIC" in sql
    )
    assert (
        "GRANT EXECUTE ON FUNCTION app_security.queue_staff_notice(uuid, timestamptz, text)"
        " TO app_runtime" in sql
    )


def test_scope_covers_mirrors_the_documented_scope_rules() -> None:
    sql = SQL.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION app_security.staff_scope_covers(" in sql
    covers = _body(sql, "CREATE OR REPLACE FUNCTION app_security.staff_scope_covers(")
    assert "user_company_scope" in covers
    assert "user_insurance_type_scope" in covers
    assert "user_ownership_level" in covers
    assert "o.level = 'own' AND p_agent_id = p_user_id" in covers
    assert "team_members" in covers


def test_directory_revisions_keep_the_directory_gate_and_stay_cheap() -> None:
    sql = SQL.read_text(encoding="utf-8")
    for name, gate in (
        ("customer_directory_revision", "app_security.has_global_scope()"),
        ("staff_directory_revision", "app_security.has_global_scope()"),
    ):
        assert f"CREATE FUNCTION app_security.{name}(" in sql
        body = _body(sql, f"CREATE FUNCTION app_security.{name}(")
        assert "app_security.has_permission('users.view')" in body
        assert gate in body
        assert "ELSE ''" in body  # callers outside the scope get no signature
    customers = _body(sql, "CREATE FUNCTION app_security.customer_directory_revision(")
    # The complaint signal must be index-backed (max(created_at)), never a
    # count over the partitioned table, so polling stays cheap at scale.
    assert "max(created_at) FROM public.complaints" in customers
    assert "count(*) FROM public.complaints" not in customers
    users = _body(sql, "CREATE FUNCTION app_security.staff_directory_revision(")
    assert "count(*) FROM public.staff_users" in users
    for name in ("customer_directory_revision", "staff_directory_revision"):
        assert f"REVOKE ALL ON FUNCTION app_security.{name}() FROM PUBLIC" in sql
        assert f"GRANT EXECUTE ON FUNCTION app_security.{name}() TO app_runtime" in sql


def test_migration_is_expand_only() -> None:
    sql = SQL.read_text(encoding="utf-8").lower()
    for forbidden in ("drop table", "drop column", "rename ", "alter column", "truncate"):
        assert forbidden not in sql


# --------------------------------------------------------------------------
# Repository wiring
# --------------------------------------------------------------------------


async def test_customer_create_and_reply_fan_out_to_staff() -> None:
    repo = ComplaintRepository()
    customer_id = uuid4()
    complaint = _complaint(customer_id)
    db = SimpleNamespace(
        add=Mock(),
        flush=AsyncMock(),
        execute=AsyncMock(),
        scalar=_scalar(staff_notified=3),
    )
    from app.domain.schemas import ComplaintCreate

    record = await repo.create(
        db,
        customer_id=customer_id,
        item=ComplaintCreate.model_validate(
            {
                "national_id": "29901010112345",
                "mobile": "+201123456789",
                "company_id": str(complaint.company_id),
                "insurance_type": "health",
                "policy_number": "POL-000777",
                "subject": "Claim delay",
                "description": "Still waiting for the settlement.",
            }
        ),
        policy_id=None,
        idempotency=SimpleNamespace(complaint_id=None, complaint_created_at=None, ticket_no=None),
    )
    assert record.ticket_no == "CMP-2026-000777"
    fanout = _calls(db, "queue_staff_notice")
    assert fanout and fanout[0][1]["kind"] == "created"

    reply_db = _db_for_reply()
    reply_db.scalar = _scalar(staff_notified=4)
    await repo.reply(reply_db, _customer_scope(), complaint, "The payment is still missing")
    reply_calls = _calls(reply_db, "queue_staff_notice")
    assert reply_calls and reply_calls[0][1]["kind"] == "reply"


async def test_staff_reply_never_fans_out_to_staff() -> None:
    repo = ComplaintRepository()
    complaint = _complaint(uuid4())
    db = _db_for_reply()
    db.scalar = _scalar(staff_notified=0)
    await repo.reply(db, _staff_scope(), complaint, "Internal handling note")
    assert _calls(db, "queue_staff_notice") == []
    # The customer-side notice queue is still used for staff public replies.
    assert _calls(db, "queue_customer_notice")


async def test_notify_staff_coerces_the_count() -> None:
    repo = ComplaintRepository()
    complaint = _complaint(uuid4())
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    assert await repo.notify_staff(db, complaint, "reply") == 0


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def test_revision_routes_are_registered() -> None:
    staff_paths = {route.path for route in staff.router.routes}
    platform_paths = {route.path for route in platform.router.routes}
    assert "/api/staff/users/revision" in staff_paths
    assert "/api/staff/customers/revision" in platform_paths


@pytest.mark.parametrize("path", ["/api/staff/users/revision", "/api/staff/customers/revision"])
async def test_revision_endpoints_require_users_view(path: str) -> None:
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.ALL)
    ctx = RequestContext(
        SimpleNamespace(scalar=AsyncMock(return_value="1|2")),
        actor,
        uuid4(),
        1,
        scope,
        frozenset(),  # no permissions at all
        {},
        SimpleNamespace(),
    )

    async def override() -> RequestContext:
        return ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            assert (await client.get(path)).status_code == 403
    finally:
        app.dependency_overrides.clear()


async def test_revision_endpoints_return_the_signature_for_global_admins() -> None:
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
    ctx = RequestContext(
        SimpleNamespace(scalar=AsyncMock(return_value="7|20261019120000000000|3|20261019")),
        actor,
        uuid4(),
        1,
        scope,
        frozenset({"users.view"}),
        {},
        SimpleNamespace(),
    )

    async def override() -> RequestContext:
        return ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            for path in ("/api/staff/users/revision", "/api/staff/customers/revision"):
                response = await client.get(path)
                assert response.status_code == 200, path
                assert re.fullmatch(r"[\d|T:.\-]+", response.json()["revision"]), path
    finally:
        app.dependency_overrides.clear()


async def test_scoped_staff_cannot_read_the_customer_directory_signature() -> None:
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(
        actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.ALL
    )  # company-scoped, not global
    ctx = RequestContext(
        SimpleNamespace(scalar=AsyncMock(return_value="")),
        actor,
        uuid4(),
        1,
        scope,
        frozenset({"users.view"}),
        {},
        SimpleNamespace(),
    )

    async def override() -> RequestContext:
        return ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            assert (await client.get("/api/staff/customers/revision")).status_code == 403
            # The users directory signature follows the staff_directory() gate
            # and degrades to an empty signature instead of leaking a count.
            response = await client.get("/api/staff/users/revision")
            assert response.status_code == 200
            assert response.json() == {"revision": ""}
    finally:
        app.dependency_overrides.clear()
