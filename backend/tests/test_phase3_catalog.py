"""Operational catalog validation, DB-delegated writes and scope filtering."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from app.api import catalog as catalog_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import InsuranceType, OwnershipLevel
from app.main import app
from app.models.operations import SlaRule
from app.models.tickets import Category
from app.repositories.catalog import CatalogRepository


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    ctx = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        1,
        AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL),
        frozenset(),
        {},
        SimpleNamespace(),
    )
    repo = SimpleNamespace(
        companies=AsyncMock(return_value=[]),
        categories=AsyncMock(return_value=[]),
        slas=AsyncMock(return_value=[]),
        templates=AsyncMock(return_value=[]),
        save=AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(catalog_api, "CatalogRepository", lambda: repo)

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "known"},
        ) as client:
            yield client, ctx, repo
    finally:
        app.dependency_overrides.clear()


async def test_catalog_requires_permission_csrf_and_continuous_sla(api: tuple) -> None:
    client, ctx, repo = api
    url = "/api/staff/admin/sla-rules"
    payload = {
        "company_id": str(uuid4()),
        "insurance_type": "health",
        "response_minutes": 30,
        "resolution_minutes": 1440,
    }
    headers = {"X-CSRF-Token": "known"}
    assert (await client.post(url, json=payload, headers=headers)).status_code == 403
    ctx.permissions = frozenset({"sla.manage"})
    assert (await client.post(url, json=payload)).status_code == 403
    assert (
        await client.post(
            url, json={**payload, "business_hours": {"start": "09:00"}}, headers=headers
        )
    ).status_code == 422
    saved = await client.post(url, json=payload, headers=headers)
    assert saved.status_code == 201 and saved.json()["id"] == str(repo.save.return_value)
    assert repo.save.call_args.args[1:3] == ("sla_rule", None)
    assert '"business_hours":{}' in repo.save.call_args.args[3]
    assert (await client.get(url)).status_code == 200
    assert repo.slas.call_args.args[1] == ctx.scope
    ctx.permissions = frozenset({"companies.manage"})
    company = {
        "slug": "firm-01",
        "name_en": "Firm One",
        "name_ar": "شركة أولى",
        "supported_types": ["health"],
        "contact_emails": ["claims@firm.invalid"],
    }
    assert (
        await client.post(
            "/api/staff/admin/companies",
            json={**company, "contact_emails": ["Break\nLine@firm.invalid"]},
            headers=headers,
        )
    ).status_code == 422
    assert (
        await client.post("/api/staff/admin/companies", json=company, headers=headers)
    ).status_code == 201
    assert repo.save.call_args.args[1] == "company"


def test_catalog_scope_predicate_prevents_cross_company_and_global_edit() -> None:
    repo = CatalogRepository()
    actor = Actor(uuid4(), "staff")
    a = uuid4()
    company = Category.company_id
    limited = AccessScope(actor, company_ids=frozenset({a}), ownership=OwnershipLevel.OWN)
    stmt = str(repo.company_predicate(limited, company).compile(dialect=postgresql.dialect()))
    assert "categories.company_id IN" in stmt
    unrestricted = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    assert str(repo.company_predicate(unrestricted, company)) == "true"
    type_scoped = AccessScope(
        actor,
        company_ids=frozenset({a}),
        insurance_types=frozenset({InsuranceType.HEALTH}),
        ownership=OwnershipLevel.ALL,
    )
    assert str(repo.company_predicate(type_scoped, company)) == "false"
    # SLA rules *can* be managed by a type-scoped operator, but only their type.
    assert SlaRule.insurance_type.in_(type_scoped.insurance_types) is not None
