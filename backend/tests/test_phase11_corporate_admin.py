"""Staff administration of corporate clients.

Setting up a contracted company is the step that makes the portal usable at
all: without it there is no login to hand over, no contract to show and no
employee linked to a request. These tests pin the authorization (the
``corporate.manage`` capability and CSRF), the validation the operator sees,
and the fact that every write leaves an audit row that does not contain the
password.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from app.api import corporate_admin as admin_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.main import app
from app.models.tickets import AuditLog
from app.security.passwords import StaffPasswords

CLIENT_ID = UUID("f787c4ff-9ac6-46b0-910d-6113477d4c48")

CONTRACT = {
    "corporate_client_id": str(CLIENT_ID),
    "contract_no": "CT-ACME-1",
    "contract_value": "2500000.00",
    "currency": "EGP",
    "payment_frequency": "quarterly",
    "starts_on": "2026-01-01",
    "renews_on": "2027-01-01",
    "status": "active",
    "headcount": 430,
}

CLIENT = {
    "slug": "acme-eg",
    "name_ar": "شركة أكمي",
    "name_en": "Acme Egypt",
    "contact_mobile": "+201012345678",
    "is_active": True,
}


class FakeSession:
    """Captures the audit rows a handler writes, without a database."""

    def __init__(self) -> None:
        self.added: list[AuditLog] = []

    def add(self, row: AuditLog) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    def begin_nested(self) -> FakeSession:
        return self

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


def _dbapi_error(sqlstate: str, message: str = "duplicate key") -> DBAPIError:
    orig = SimpleNamespace(sqlstate=sqlstate, diag=SimpleNamespace(message_primary=message))
    return DBAPIError("INSERT", {}, orig)  # type: ignore[arg-type]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    db = FakeSession()
    ctx = RequestContext(
        db,  # type: ignore[arg-type]
        actor,
        uuid4(),
        1,
        AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL),
        frozenset({"corporate.view", "corporate.manage"}),
        {},
        SimpleNamespace(),
    )
    service = SimpleNamespace(
        directory=AsyncMock(return_value={"items": [], "total": 0}),
        save_client=AsyncMock(return_value={"id": CLIENT_ID}),
        save_contract=AsyncMock(return_value={"id": uuid4()}),
        create_user=AsyncMock(return_value={"id": uuid4()}),
        link_member=AsyncMock(return_value={"linked": True}),
    )
    monkeypatch.setattr(admin_api, "_service", lambda: service)
    # The password hasher normally arrives with the auth service at startup;
    # these tests never run the lifespan.
    monkeypatch.setattr(
        app.state, "auth", SimpleNamespace(passwords=StaffPasswords()), raising=False
    )

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        yield ctx, db, service
    finally:
        app.dependency_overrides.clear()


async def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        cookies={"csrf_token": "known"},
    )


HEADERS = {"X-CSRF-Token": "known"}


async def test_registering_a_company_records_who_did_it(api: tuple) -> None:
    ctx, db, service = api
    async with await _client() as client:
        response = await client.put("/api/staff/corporate/clients", json=CLIENT, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"id": str(CLIENT_ID)}
    service.save_client.assert_awaited_once()
    assert len(db.added) == 1
    audit = db.added[0]
    assert audit.action == "corporate_client_saved"
    assert audit.actor_staff_id == ctx.actor.id
    assert audit.after_state["slug"] == "acme-eg"


async def test_amending_a_company_is_a_different_audit_action(api: tuple) -> None:
    _, db, _ = api
    async with await _client() as client:
        response = await client.put(
            "/api/staff/corporate/clients",
            json={**CLIENT, "id": str(CLIENT_ID)},
            headers=HEADERS,
        )

    assert response.status_code == 200
    # An operator reading the trail can tell a new company from an edit.
    assert db.added[0].action == "corporate_client_updated"


async def test_a_taken_slug_is_a_conflict_not_a_server_error(api: tuple) -> None:
    _, _, service = api
    service.save_client.side_effect = _dbapi_error("23505")
    async with await _client() as client:
        response = await client.put("/api/staff/corporate/clients", json=CLIENT, headers=HEADERS)

    assert response.status_code == 409
    assert "slug" in response.json()["detail"]


async def test_the_contract_terms_the_company_will_see_are_recorded(api: tuple) -> None:
    _, db, service = api
    async with await _client() as client:
        response = await client.put(
            "/api/staff/corporate/contracts", json=CONTRACT, headers=HEADERS
        )

    assert response.status_code == 200
    saved = service.save_contract.await_args.args[1]
    assert saved.contract_no == "CT-ACME-1"
    assert saved.payment_frequency.value == "quarterly"
    assert str(saved.contract_value) == "2500000.00"
    after = db.added[0].after_state
    assert after["renews_on"] == "2027-01-01"
    assert after["payment_frequency"] == "quarterly"


async def test_a_contract_cannot_renew_before_it_starts(api: tuple) -> None:
    _, _, service = api
    async with await _client() as client:
        response = await client.put(
            "/api/staff/corporate/contracts",
            json={**CONTRACT, "starts_on": "2027-01-01", "renews_on": "2026-01-01"},
            headers=HEADERS,
        )

    assert response.status_code == 422
    # Rejected before it reaches the database.
    service.save_contract.assert_not_awaited()


async def test_a_lowercase_currency_is_accepted_and_normalised(api: tuple) -> None:
    _, _, service = api
    async with await _client() as client:
        response = await client.put(
            "/api/staff/corporate/contracts", json={**CONTRACT, "currency": "egp"}, headers=HEADERS
        )

    assert response.status_code == 200
    assert service.save_contract.await_args.args[1].currency == "EGP"


async def test_an_unknown_payment_frequency_is_refused(api: tuple) -> None:
    _, _, service = api
    async with await _client() as client:
        response = await client.put(
            "/api/staff/corporate/contracts",
            json={**CONTRACT, "payment_frequency": "weekly"},
            headers=HEADERS,
        )

    assert response.status_code == 422
    service.save_contract.assert_not_awaited()


async def test_issuing_a_portal_login_never_audits_the_password(api: tuple) -> None:
    _, db, service = api
    async with await _client() as client:
        response = await client.post(
            "/api/staff/corporate/users",
            json={
                "corporate_client_id": str(CLIENT_ID),
                "username": "acme.hr",
                "full_name": "Acme HR",
                "password": "Corp0rate-Portal-2026",
            },
            headers=HEADERS,
        )

    assert response.status_code == 201
    service.create_user.assert_awaited_once()
    audit = db.added[0]
    assert audit.action == "corporate_user_created"
    assert audit.after_state == {"username": "acme.hr", "full_name": "Acme HR"}
    assert "Corp0rate-Portal-2026" not in str(audit.after_state)


async def test_a_weak_portal_password_is_refused(api: tuple) -> None:
    _, _, service = api
    async with await _client() as client:
        response = await client.post(
            "/api/staff/corporate/users",
            json={
                "corporate_client_id": str(CLIENT_ID),
                "username": "acme.hr",
                "full_name": "Acme HR",
                "password": "aaaaaaaaaaaaaa",
            },
            headers=HEADERS,
        )

    assert response.status_code == 422
    service.create_user.assert_not_awaited()


async def test_a_taken_username_is_a_conflict(api: tuple) -> None:
    _, _, service = api
    service.create_user.side_effect = _dbapi_error("23505")
    async with await _client() as client:
        response = await client.post(
            "/api/staff/corporate/users",
            json={
                "corporate_client_id": str(CLIENT_ID),
                "username": "acme.hr",
                "full_name": "Acme HR",
                "password": "Corp0rate-Portal-2026",
            },
            headers=HEADERS,
        )

    assert response.status_code == 409


async def test_linking_an_employee_records_the_company_not_the_person(api: tuple) -> None:
    _, db, service = api
    customer_id = uuid4()
    async with await _client() as client:
        response = await client.post(
            "/api/staff/corporate/members",
            json={
                "customer_id": str(customer_id),
                "corporate_client_id": str(CLIENT_ID),
                "employee_code": "E-0001",
            },
            headers=HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {"linked": True}
    service.link_member.assert_awaited_once()
    audit = db.added[0]
    # The resource is the company; the customer is detail, not the subject.
    assert audit.resource_id == CLIENT_ID
    assert audit.after_state["employee_code"] == "E-0001"


async def test_the_directory_is_readable_without_the_manage_capability(api: tuple) -> None:
    ctx, _, service = api
    object.__setattr__(ctx, "permissions", frozenset({"corporate.view"}))
    async with await _client() as client:
        response = await client.get("/api/staff/corporate")

    assert response.status_code == 200
    service.directory.assert_awaited_once()


async def test_reading_the_directory_is_not_enough_to_change_anything(api: tuple) -> None:
    ctx, _, service = api
    object.__setattr__(ctx, "permissions", frozenset({"corporate.view"}))
    async with await _client() as client:
        for method, url, payload in (
            ("put", "/api/staff/corporate/clients", CLIENT),
            ("put", "/api/staff/corporate/contracts", CONTRACT),
        ):
            response = await getattr(client, method)(url, json=payload, headers=HEADERS)
            assert response.status_code == 403, url

    service.save_client.assert_not_awaited()
    service.save_contract.assert_not_awaited()


async def test_a_corporate_reader_cannot_reach_the_staff_admin_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The portal's own session must not double as an administration session."""
    actor = Actor(uuid4(), "corporate")
    ctx = RequestContext(
        SimpleNamespace(),  # type: ignore[arg-type]
        actor,
        uuid4(),
        1,
        AccessScope(actor, all_companies=False, ownership=OwnershipLevel.OWN),
        frozenset({"corporate.view", "corporate.manage"}),
        {},
        SimpleNamespace(),
        corporate_client_id=CLIENT_ID,
    )

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with await _client() as client:
            assert (await client.get("/api/staff/corporate")).status_code == 403
            response = await client.put(
                "/api/staff/corporate/clients", json=CLIENT, headers=HEADERS
            )
            assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


async def test_a_write_without_a_csrf_token_is_rejected(api: tuple) -> None:
    _, _, service = api
    async with await _client() as client:
        response = await client.put("/api/staff/corporate/clients", json=CLIENT)

    assert response.status_code == 403
    service.save_client.assert_not_awaited()
