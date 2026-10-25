"""Insurance-operations handlers + repository exercised offline with a fake session.

Sales CRM handler tests live in ``test_crm_module`` since the unification.

The PostgreSQL side (RLS policies, SECURITY DEFINER converters/importers,
gateway payment recording) is covered by the integration suite; here we pin
the handler logic: audit rows on every write, the issuing checklist gate,
fail-closed payment intents, per-row import errors and scope predicates.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, UploadFile

from app.api import insurance_ops as crm_api
from app.api.deps import RequestContext
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.insurance_ops_schemas import (
    BrokeragePolicyCreate,
    ChecklistItemUpdate,
    InstallmentPay,
    IssuingCreate,
    IssuingUpdate,
    PaymentIntentCreate,
    SettlementUpdate,
)
from app.models.insurance_ops import (
    BrokeragePolicy,
    CommissionSettlement,
    IssuingChecklistItem,
    IssuingChecklistTemplate,
    IssuingRequest,
    PolicyInstallment,
)
from app.repositories.insurance_ops import InsuranceOpsRepository as CrmRepository

STAFF = UUID("11111111-1111-4111-8111-111111111111")
INSURER = UUID("22222222-2222-4222-8222-222222222222")
CLIENT = UUID("33333333-3333-4333-8333-333333333333")


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> Any:
        return iter(list(self.rows))

    def scalar_one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeDB:
    """Routes SELECTs to per-entity registries; captures every added row."""

    def __init__(self) -> None:
        self.by_entity: dict[Any, list[Any]] = {}
        self.text_queue: list[list[Any]] = []
        self.aggregate_rows: list[Any] = []
        self.scalar_value: Any = None
        self.added: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        descriptions = getattr(stmt, "column_descriptions", None)
        if descriptions and len(descriptions) > 1:
            return FakeResult(list(self.aggregate_rows))
        if descriptions:
            entity = descriptions[0].get("entity")
            if entity is not None:
                return FakeResult(list(self.by_entity.get(entity, [])))
            return FakeResult(list(self.aggregate_rows))
        if self.text_queue:
            return FakeResult(self.text_queue.pop(0))
        return FakeResult([])

    async def scalar(self, stmt: Any, params: Any = None) -> Any:
        return self.scalar_value

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        yield


def make_ctx(db: FakeDB, *, all_companies: bool = True) -> RequestContext:
    actor = Actor(id=STAFF, scope="staff")
    scope = AccessScope(
        actor=actor,
        all_companies=all_companies,
        company_ids=frozenset() if all_companies else frozenset({INSURER}),
        ownership=OwnershipLevel.ALL,
    )
    complaints = SimpleNamespace(
        protector=SimpleNamespace(encrypt=lambda s: b"cipher", blind_index=lambda s: b"blind")
    )
    return RequestContext(
        db,  # type: ignore[arg-type]
        actor,
        uuid4(),
        1,
        scope,
        frozenset({"crm.view", "crm.manage", "crm.convert"}),
        None,
        complaints,  # type: ignore[arg-type]
    )


def audits(db: FakeDB) -> list[Any]:
    return [row for row in db.added if row.__class__.__name__ == "AuditLog"]


async def test_policy_creation_builds_installments_and_settlement() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    result = await crm_api.create_policy(
        BrokeragePolicyCreate(
            corporate_client_id=CLIENT,
            insurer_company_id=INSURER,
            policy_no="POL-100",
            insurance_type="health",
            starts_on=date(2026, 1, 1),
            ends_on=date(2027, 1, 1),
            premium=Decimal("120000"),
            installment_plan="quarterly",
        ),
        ctx,
        None,
    )
    installments = [row for row in db.added if isinstance(row, PolicyInstallment)]
    settlements = [row for row in db.added if isinstance(row, CommissionSettlement)]
    assert len(installments) == 4
    assert settlements[0].expected_amount == Decimal("6000.00")  # 5% of premium
    assert result["policy_no"] == "POL-100"
    settlement = settlements[0]
    db.by_entity[CommissionSettlement] = [settlement]
    updated = await crm_api.update_settlement(
        settlement.id, SettlementUpdate(received_amount=Decimal("6000")), ctx, None
    )
    assert updated["status"] == "settled"
    installment = installments[0]
    db.by_entity[PolicyInstallment] = [installment]
    paid = await crm_api.pay_installment(installment.id, InstallmentPay(), ctx, None)
    assert paid["status"] == "paid" and installment.paid_at is not None


async def test_payment_intent_fails_closed_without_gateway_credentials() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(paymob_api_key=None, fawry_merchant_code=None)
            )
        )
    )
    with pytest.raises(HTTPException) as raised:
        await crm_api.payment_intent(
            uuid4(),
            PaymentIntentCreate(provider="paymob"),
            request,
            ctx,
            None,  # type: ignore[arg-type]
        )
    assert raised.value.status_code == 503


async def test_issuing_checklist_gates_submission_to_insurer() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    db.by_entity[IssuingChecklistTemplate] = [
        IssuingChecklistTemplate(
            id=uuid4(),
            tenant_id=CrmRepository.tenant_id,
            insurance_type="health",
            document_name="السجل التجاري",
            is_required=True,
            sort_order=1,
        )
    ]
    created = await crm_api.create_issuing(
        IssuingCreate(insurer_company_id=INSURER, insurance_type="health"), ctx, None
    )
    request_row = next(row for row in db.added if isinstance(row, IssuingRequest))
    item_row = next(row for row in db.added if isinstance(row, IssuingChecklistItem))
    assert created["checklist"] == []  # detail reads through the (fake) session
    db.by_entity[IssuingRequest] = [request_row]
    db.scalar_value = item_row.id  # a required document is still missing
    with pytest.raises(HTTPException, match="still missing"):
        await crm_api.update_issuing(
            request_row.id, IssuingUpdate(status="sent_to_insurer"), ctx, None
        )
    db.by_entity[IssuingChecklistItem] = [item_row]
    received = await crm_api.update_checklist_item(
        item_row.id, ChecklistItemUpdate(status="received"), ctx, None
    )
    assert received["status"] == "received"
    db.scalar_value = None
    sent = await crm_api.update_issuing(
        request_row.id,
        IssuingUpdate(status="sent_to_insurer", insurer_reference="INS-1"),
        ctx,
        None,
    )
    assert sent["status"] == "sent_to_insurer" and request_row.submitted_at is not None


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


async def test_excel_import_reports_per_row_errors_and_audits() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    employees = b"full_name,mobile\nMona,01012345678\nNoMobile,\n"
    result = await crm_api.import_employees(ctx, None, _upload("staff.csv", employees), CLIENT)
    assert result["rows_succeeded"] == 1 and result["rows_failed"] == 1
    clients = b"company_name,contact_name\nNile Co,Sara\n"
    result = await crm_api.import_clients(ctx, None, _upload("clients.csv", clients))
    assert result["rows_succeeded"] == 1
    assert {row.action for row in audits(db)} == {"excel_import_completed"}
    with pytest.raises(HTTPException, match="xlsx"):
        await crm_api.import_clients(ctx, None, _upload("clients.txt", b"x"))


async def test_repository_reads_scope_and_aggregate() -> None:
    db = FakeDB()
    repo = CrmRepository()
    ctx = make_ctx(db)
    policy = BrokeragePolicy(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        corporate_client_id=CLIENT,
        insurer_company_id=INSURER,
        policy_no="POL-9",
        insurance_type="general",
        starts_on=date(2026, 1, 1),
        ends_on=date(2026, 12, 10),
        premium=Decimal("10000"),
        currency="EGP",
        installment_plan="cash",
        commission_rate=Decimal("0.05"),
        commission_amount=Decimal("500"),
        status="active",
    )
    db.by_entity.update(
        {
            BrokeragePolicy: [policy],
            PolicyInstallment: [],
            CommissionSettlement: [],
            IssuingRequest: [],
        }
    )
    assert (await repo.policies(db, ctx.scope))[0]["policy_no"] == "POL-9"  # type: ignore[arg-type]
    detail = await repo.policy_detail(db, ctx.scope, policy.id)  # type: ignore[arg-type]
    assert detail is not None and detail["installments"] == []
    assert await repo.renewals(db, ctx.scope, 365)  # type: ignore[arg-type]
    assert await repo.issuing(db, ctx.scope) == []  # type: ignore[arg-type]
    insurer_row = SimpleNamespace(
        _mapping={
            "insurer_company_id": str(INSURER),
            "insurer_name_ar": "النيل",
            "insurer_name_en": "Nile",
            "requests": 9,
            "resolved": 7,
            "sla_breaches": 1,
            "avg_resolution_hours": Decimal("4.50"),
        },
        avg_resolution_hours=Decimal("4.50"),
    )
    db.text_queue = [[insurer_row]]
    report = await repo.insurer_performance(  # type: ignore[arg-type]
        db, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    )
    assert report[0]["avg_resolution_hours"] == "4.50"


async def test_client_folder_and_document_search_reads() -> None:
    db = FakeDB()
    repo = CrmRepository()
    client_row = SimpleNamespace(
        _mapping={
            "id": str(CLIENT),
            "slug": "nile",
            "name_ar": "النيل",
            "name_en": "Nile",
            "contact_name": None,
            "contact_email": None,
            "contact_mobile": None,
            "is_active": True,
        }
    )
    contract_row = SimpleNamespace(
        _mapping={
            "id": str(uuid4()),
            "contract_no": "CT-1",
            "contract_value": Decimal("1000"),
            "currency": "EGP",
            "payment_frequency": "monthly",
            "starts_on": date(2026, 1, 1),
            "renews_on": date(2027, 1, 1),
            "status": "active",
        }
    )
    status_row = SimpleNamespace(status="resolved", n=4)
    db.text_queue = [[client_row], [contract_row], [], [], [status_row]]
    db.by_entity[BrokeragePolicy] = []
    folder = await repo.client_folder(db, CLIENT)  # type: ignore[arg-type]
    assert folder is not None
    assert folder["contracts"][0]["contract_value"] == "1000"
    assert folder["tickets_by_status"] == {"resolved": 4}
    db.text_queue = [[], []]
    db.by_entity[BrokeragePolicy] = []
    search = await repo.document_search(db, "وثيقة")  # type: ignore[arg-type]
    assert search == {"documents": [], "policies": [], "tickets": []}


async def test_lookup_endpoints_and_lists() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    db.scalar_value = {"full_name": "Mona", "corporate_name_ar": "النيل"}
    found = await crm_api.caller_lookup(ctx, "01012345678")
    assert found["caller"]["full_name"] == "Mona"
    with pytest.raises(HTTPException, match="valid Egyptian"):
        await crm_api.caller_lookup(ctx, "12345678901")
    assert (await crm_api.list_policies(ctx))["policies"] == []
    assert (await crm_api.renewals(ctx, 30))["renewals"] == []
    assert (await crm_api.list_issuing(ctx))["issuing"] == []
    assert (await crm_api.list_imports(ctx))["imports"] == []
    db.text_queue = [[], [], [], []]
    assert (await crm_api.document_search(ctx, "abc"))["tickets"] == []
    db.text_queue = [[]]
    assert (await crm_api.insurer_performance(ctx, 90))["insurers"] == []
    db.text_queue = [[]]
    with pytest.raises(HTTPException, match="Client not found"):
        await crm_api.client_folder(CLIENT, ctx)
    with pytest.raises(HTTPException, match="Policy not found"):
        await crm_api.get_policy(uuid4(), ctx)
