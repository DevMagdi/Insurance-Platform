"""CRM ownership and reassignment against a disposable migrated PostgreSQL database.

A module-scoped fixture seeds isolated sellers, a manager, and leads through the
admin connection. The API is exercised with the restricted app_runtime role so
these checks cover real SQL, RLS, activity persistence, and ownership filters.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import crm_module as api
from app.api.deps import RequestContext
from app.db import Actor, make_engine, scoped_transaction
from app.domain.access import AccessScope
from app.domain.crm_module_schemas import CrmBulkAssign
from app.domain.enums import OwnershipLevel

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class CrmWorld:
    runtime_url: str
    seller_a: UUID
    seller_b: UUID
    manager: UUID
    lead_a: UUID
    lead_b: UUID


def _admin_dsn() -> str:
    url = os.getenv("TEST_DATABASE_ADMIN_URL", "")
    if not url:
        pytest.skip("Set TEST_DATABASE_ADMIN_URL for real PostgreSQL CRM tests")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="module")
def crm_world() -> Iterator[CrmWorld]:
    admin_url = _admin_dsn()
    runtime_url = os.getenv("TEST_DIRECT_DATABASE_URL", "")
    if not runtime_url:
        pytest.skip("Set TEST_DIRECT_DATABASE_URL for real PostgreSQL CRM tests")

    tag = uuid4().hex[:12]
    sellers = {"a": uuid4(), "b": uuid4()}
    manager = uuid4()
    leads = {"a": uuid4(), "b": uuid4()}
    world = CrmWorld(runtime_url, sellers["a"], sellers["b"], manager, leads["a"], leads["b"])

    try:
        with psycopg.connect(admin_url, prepare_threshold=None) as conn, conn.cursor() as cur:
            for role, staff_id in (*sellers.items(), ("manager", manager)):
                cur.execute(
                    "INSERT INTO public.staff_users (id, email, full_name, password_hash, status) "
                    "VALUES (%s, %s, %s, %s, 'active')",
                    (
                        staff_id,
                        f"crm-{tag}-{role}@example.test",
                        f"CRM {role}",
                        "test-only-no-login",
                    ),
                )
            for role, lead_id in leads.items():
                cur.execute(
                    "INSERT INTO public.crm_leads "
                    "(id, lead_no, full_name, phone, source, stage_code, priority, insurance_type, "
                    "assigned_to, created_by) "
                    "VALUES (%s, %s, %s, %s, 'manual', 'new', 'normal', 'general', %s, %s)",
                    (
                        lead_id,
                        f"LEAD-CRM-{tag}-{role}",
                        f"CRM lead {role}",
                        f"+201{uuid4().int % 1_000_000_000:09d}",
                        sellers[role],
                        sellers[role],
                    ),
                )

        yield world
    finally:
        with psycopg.connect(admin_url, prepare_threshold=None) as conn, conn.cursor() as cur:
            staff_ids = [*sellers.values(), manager]
            lead_ids = list(leads.values())
            cur.execute(
                "DELETE FROM public.audit_logs WHERE actor_staff_id = ANY(%s) "
                "OR resource_id = ANY(%s)",
                (staff_ids, lead_ids),
            )
            cur.execute("DELETE FROM public.crm_activities WHERE lead_id = ANY(%s)", (lead_ids,))
            cur.execute("DELETE FROM public.crm_leads WHERE id = ANY(%s)", (lead_ids,))
            cur.execute("DELETE FROM public.staff_users WHERE id = ANY(%s)", (staff_ids,))


def _context(db: AsyncSession, actor_id: UUID, *, manager: bool = False) -> RequestContext:
    actor = Actor(actor_id, "staff")
    return RequestContext(
        db=db,
        actor=actor,
        session_id=uuid4(),
        token_version=1,
        scope=AccessScope(actor=actor, all_companies=True, ownership=OwnershipLevel.ALL),
        permissions=(
            frozenset({"crm.view", "crm.manage", "crm.convert"})
            if manager
            else frozenset({"crm.view", "crm.manage"})
        ),
        snapshot=None,
        complaints=SimpleNamespace(),  # CRM handlers do not use the complaint service.
    )


@pytest.mark.asyncio
async def test_seller_scope_manager_reassignment_and_activity_are_persisted(
    crm_world: CrmWorld,
) -> None:
    engine = make_engine(crm_world.runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with scoped_transaction(factory, Actor(crm_world.seller_a, "staff")) as db:
            seller_a = _context(db, crm_world.seller_a)
            result = await api.list_leads(seller_a)
            assert [UUID(item["id"]) for item in result["items"]] == [crm_world.lead_a]
            with pytest.raises(HTTPException) as raised:
                await api.get_lead(crm_world.lead_b, seller_a)
            assert raised.value.status_code == 404

        async with scoped_transaction(factory, Actor(crm_world.seller_b, "staff")) as db:
            seller_b = _context(db, crm_world.seller_b)
            result = await api.list_leads(seller_b)
            assert [UUID(item["id"]) for item in result["items"]] == [crm_world.lead_b]

        async with scoped_transaction(factory, Actor(crm_world.manager, "staff")) as db:
            manager = _context(db, crm_world.manager, manager=True)
            result = await api.bulk_assign(
                CrmBulkAssign(ids=[crm_world.lead_b], assigned_to=crm_world.seller_a), manager
            )
            assert result == {"updated": 1}

        async with scoped_transaction(factory, Actor(crm_world.seller_a, "staff")) as db:
            seller_a = _context(db, crm_world.seller_a)
            result = await api.list_leads(seller_a)
            assert {UUID(item["id"]) for item in result["items"]} == {
                crm_world.lead_a,
                crm_world.lead_b,
            }
            reassigned = await api.get_lead(crm_world.lead_b, seller_a)
            assignment = next(
                activity
                for activity in reassigned["activities"]
                if activity["type"] == "assignment"
            )
            assert assignment["created_by"] == str(crm_world.manager)
            assert "Bulk assigned to" in assignment["description"]
    finally:
        await engine.dispose()
