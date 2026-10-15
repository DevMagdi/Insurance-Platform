"""Complaint service tests: idempotency, ownership, masking and audited exports."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus, InsuranceType, MessageVisibility, OwnershipLevel
from app.domain.schemas import ComplaintCreate, ComplaintFilter
from app.models.tickets import Complaint
from app.repositories.complaints import ComplaintRepository
from app.security.crypto import NationalIdProtector
from app.services.complaints import ComplaintService, safe_csv_cell

VALID_NID = "29901010112345"


def create_payload() -> dict[str, object]:
    return {
        "national_id": VALID_NID,
        "mobile": "+201123456789",
        "company_id": str(uuid4()),
        "insurance_type": "health",
        "policy_number": "POL-000123",
        "subject": "Claim processing delay",
        "description": "The payment has not arrived after the expected date.",
    }


@pytest.fixture
def fixture() -> tuple[ComplaintService, Mock, SimpleNamespace, AccessScope, Complaint]:
    protector = NationalIdProtector(b"e" * 32, b"b" * 32)
    repo = Mock(spec=ComplaintRepository)
    for name in (
        "identity_matches",
        "active_company",
        "reserve_idempotency",
        "get",
        "create",
        "policy_for_customer",
        "list",
        "count",
        "detail",
        "reply",
        "reopen",
        "csat",
        "change_status",
        "reveal_national_id",
        "audit_reveal",
    ):
        setattr(repo, name, AsyncMock())
    service = ComplaintService(repo, protector, "k" * 64)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    actor = Actor(uuid4(), "customer")
    scope = AccessScope.for_customer(actor)
    now = datetime.now(UTC)
    complaint = Complaint(
        id=uuid4(),
        created_at=now,
        ticket_no="CMP-2026-000123",
        customer_id=actor.id,
        company_id=uuid4(),
        insurance_type=InsuranceType.HEALTH,
        policy_number="POL-000123",
        subject="Claim processing delay",
        description="The payment has not arrived after the expected date.",
        status=ComplaintStatus.NEW,
        updated_at=now,
    )
    repo.get.return_value = complaint
    repo.identity_matches.return_value = True
    repo.active_company.return_value = True
    repo.policy_for_customer.return_value = None
    repo.create.return_value = complaint
    repo.reply.return_value = SimpleNamespace(
        id=uuid4(), created_at=now, body="Answer", visibility=MessageVisibility.PUBLIC
    )
    repo.reopen.return_value = True
    repo.csat.return_value = True
    repo.change_status.return_value = True
    repo.reveal_national_id.return_value = protector.encrypt(VALID_NID)
    return service, repo, db, scope, complaint


def test_search_filters_always_include_explicit_scope() -> None:
    repo = ComplaintRepository()
    actor, insurer = uuid4(), uuid4()
    scoped = AccessScope(
        Actor(actor, "staff"),
        company_ids=frozenset({insurer}),
        insurance_types=frozenset({InsuranceType.HEALTH}),
        ownership=OwnershipLevel.OWN,
    )
    filters = ComplaintFilter(
        status="new",
        company_id=insurer,
        insurance_type="health",
        search="delayed claim",
        assigned_agent_id=actor,
        ticket_no="CMP-2026-000123",
    )
    sql = str(repo.base_query(scoped, filters).compile(dialect=postgresql.dialect()))
    assert "complaints.company_id IN" in sql
    assert "complaints.company_id =" in sql
    assert "complaints.assigned_agent_id" in sql
    assert "complaints.insurance_type" in sql
    assert "plainto_tsquery" in sql and "search_vector" in sql
    assert "complaints.ticket_no" in sql
    assert "complaints.status" in sql
    customer = AccessScope.for_customer(Actor(uuid4(), "customer"))
    assert "complaints.customer_id" in str(repo.base_query(customer, ComplaintFilter()))


async def test_create_idempotency_replay_and_payload_collision(fixture: tuple) -> None:
    service, repo, db, scope, complaint = fixture
    item = ComplaintCreate.model_validate({**create_payload(), "company_id": complaint.company_id})
    key = "uuid:once-12345"
    request_digest = hmac.new(b"k" * 64, item.model_dump_json().encode(), hashlib.sha256).digest()
    idem = SimpleNamespace(
        request_digest=request_digest,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        complaint_id=None,
    )
    repo.reserve_idempotency.return_value = idem
    result, replay = await service.create(db, scope, item, key)
    assert result == complaint and replay is False
    assert repo.create.await_count == 1
    assert repo.identity_matches.call_args.args[2] == service.protector.blind_index(VALID_NID)
    idem.complaint_id = complaint.id
    result, replay = await service.create(db, scope, item, key)
    assert replay is True and result == complaint
    assert repo.create.await_count == 1
    idem.request_digest = b"n" * 32
    with pytest.raises(HTTPException) as exc:
        await service.create(db, scope, item, key)
    assert exc.value.status_code == 409
    idem.request_digest = request_digest
    idem.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as exc:
        await service.create(db, scope, item, key)
    assert exc.value.status_code == 409


async def test_create_rejects_mismatched_identity_and_company(fixture: tuple) -> None:
    service, repo, db, scope, complaint = fixture
    item = ComplaintCreate.model_validate({**create_payload(), "company_id": complaint.company_id})
    repo.identity_matches.return_value = False
    with pytest.raises(HTTPException) as exc:
        await service.create(db, scope, item, "unique-key-123")
    assert exc.value.status_code == 403
    repo.identity_matches.return_value = True
    repo.active_company.return_value = False
    with pytest.raises(HTTPException) as exc:
        await service.create(db, scope, item, "unique-key-123")
    assert exc.value.status_code == 422
    repo.reserve_idempotency.assert_not_awaited()


async def test_detail_masking_id_guessing_and_timeline_parent(fixture: tuple) -> None:
    service, repo, db, scope, complaint = fixture
    repo.detail.return_value = (complaint, "Jane", "1234", "Nile")
    detail = await service.detail(db, scope, complaint.id)
    assert detail["national_id_masked"] == "**********1234"
    assert "national_id" not in detail
    repo.detail.return_value = (complaint, "Jane", None, "Nile")
    assert (await service.detail(db, scope, complaint.id))["national_id_masked"] == "**************"
    repo.detail.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.detail(db, scope, uuid4())
    assert exc.value.status_code == 404
    repo.get.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.parent(db, scope, uuid4())
    assert exc.value.status_code == 404


async def test_reply_reopen_csat_and_status_guards(fixture: tuple) -> None:
    service, repo, db, scope, complaint = fixture
    response = await service.reply(db, scope, complaint.id, "Answer")
    assert response["body"] == "Answer"
    complaint.status = ComplaintStatus.RESOLVED
    with pytest.raises(HTTPException) as exc:
        await service.reply(db, scope, complaint.id, "Answer")
    assert exc.value.status_code == 409
    assert (await service.reopen(db, scope, complaint.id))["status"] == ComplaintStatus.REOPENED
    repo.reopen.return_value = False
    with pytest.raises(HTTPException) as exc:
        await service.reopen(db, scope, complaint.id)
    assert exc.value.status_code == 409
    assert (await service.csat(db, scope, complaint.id, 4, None))["submitted"]
    repo.csat.return_value = False
    with pytest.raises(HTTPException) as exc:
        await service.csat(db, scope, complaint.id, 4, None)
    assert exc.value.status_code == 409
    staff_scope = AccessScope(
        Actor(uuid4(), "staff"), all_companies=True, ownership=OwnershipLevel.ALL
    )
    assert (await service.change_status(db, staff_scope, complaint.id, ComplaintStatus.IN_REVIEW))[
        "status"
    ] == "in_review"
    repo.change_status.return_value = False
    with pytest.raises(HTTPException):
        await service.change_status(db, staff_scope, complaint.id, ComplaintStatus.RESOLVED)


async def test_national_id_reveal_requires_scope_and_audits(fixture: tuple) -> None:
    service, repo, db, _, complaint = fixture
    staff_scope = AccessScope(
        Actor(uuid4(), "staff"), all_companies=True, ownership=OwnershipLevel.ALL
    )
    with pytest.raises(HTTPException) as exc:
        await service.reveal_national_id(db, staff_scope, complaint.id, "short")
    assert exc.value.status_code == 400
    assert (
        await service.reveal_national_id(db, staff_scope, complaint.id, "Case verification needed")
        == VALID_NID
    )
    repo.audit_reveal.assert_awaited_once()
    repo.reveal_national_id.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.reveal_national_id(db, staff_scope, complaint.id, "Case verification needed")
    assert exc.value.status_code == 403


async def test_list_export_and_csv_formula_safety(fixture: tuple) -> None:
    service, repo, db, scope, complaint = fixture
    filters = ComplaintFilter(search="claim")
    repo.list.return_value = [complaint]
    repo.count.return_value = 1
    listed = await service.list(db, scope, filters)
    assert listed["total"] == 1 and listed["items"][0]["id"] == complaint.id
    with pytest.raises(HTTPException) as exc:
        await service.export(db, scope, filters)
    assert exc.value.status_code == 403
    staff_scope = AccessScope(
        Actor(uuid4(), "staff"), all_companies=True, ownership=OwnershipLevel.ALL
    )
    complaint.subject = '=HYPERLINK("https://evil")'
    output = await service.export(db, staff_scope, filters)
    assert b"'=HYPERLINK" in output
    assert db.add.called and db.flush.await_count == 1
    assert safe_csv_cell("+SUM(1,2)") == "'+SUM(1,2)"
    repo.list.return_value = [complaint] * 1001
    with pytest.raises(HTTPException) as exc:
        await service.export(db, staff_scope, filters)
    assert exc.value.status_code == 413


async def test_counters_return_status_breakdown_and_open_closed_total() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=[
                (ComplaintStatus.NEW, 2),
                (ComplaintStatus.IN_REVIEW, 3),
                (ComplaintStatus.RESOLVED, 4),
                (ComplaintStatus.TRANSFERRED, 1),
            ]
        )
    )
    scope = AccessScope(
        Actor(uuid4(), "staff"), company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN
    )
    counters = await ComplaintRepository().counters(
        db, scope, ComplaintFilter(status=ComplaintStatus.RESOLVED)
    )
    assert counters == {
        "new": 2,
        "assigned": 0,
        "in_review": 3,
        "resolved": 4,
        "reopened": 0,
        "transferred": 1,
        "total": 10,
        "closed": 4,
        "open": 6,
    }
    stmt = db.execute.call_args.args[0]
    query = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "complaints.status =" not in query  # status filter removed for breakdown
    assert "complaints.company_id" in query and "complaints.assigned_agent_id" in query
