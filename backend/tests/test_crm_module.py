"""CRM module: Excel parsing, mapping, dedup, handlers, stats, permissions."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook
from pydantic import ValidationError

from app.api import crm_module as api
from app.api.deps import RequestContext, require_permission
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.crm_module_schemas import (
    CrmBulkStatus,
    CrmLeadCreate,
    CrmLeadUpdate,
)
from app.domain.enums import OwnershipLevel
from app.main import app
from app.models import Base, CrmLead
from app.models.crm_module import CrmImportBatch
from app.repositories.crm_module import CrmModuleRepository
from app.services.crm_import import dedupe, map_rows, normalize_source, parse_leads_file

STAFF = UUID("11111111-1111-4111-8111-111111111111")


def _xlsx(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_parse_xlsx_csv_with_arabic_and_english_headers() -> None:
    content = _xlsx(
        [
            ["اسم العميل", "رقم الهاتف", "الايميل", "مصدر العميل", "تعليق"],
            ["Mona Ali", "01012345678", "MONA@example.com", "واتساب", "مهتمة"],
        ]
    )
    rows = parse_leads_file("leads.xlsx", content)
    assert rows[0]["full_name"] == "Mona Ali"
    assert rows[0]["phone"] == "01012345678"
    rows = parse_leads_file(
        "leads.csv", b"Full Name,Mobile Number,E-mail\nKarim,01112345678,k@x.com\n"
    )
    assert rows[0]["full_name"] == "Karim" and rows[0]["phone"] == "01112345678"
    with pytest.raises(ValueError, match="xlsx"):
        parse_leads_file("leads.pdf", b"x")
    with pytest.raises(ValueError, match="too large"):
        parse_leads_file("leads.csv", b"x" * (5_242_881))


def test_map_rows_normalizes_and_reports_each_bad_row() -> None:
    rows = [
        {
            "full_name": "Mona",
            "phone": "01012345678",
            "email": "Mona@Example.com",
            "source": "واتساب",
        },
        {"full_name": "", "phone": "01012345678"},
        {"full_name": "Bad Phone", "phone": "123"},
        {"full_name": "Bad Email", "email": "nope"},
        {"full_name": "No Contact"},
    ]
    candidates, errors = map_rows(rows)
    assert len(candidates) == 1
    assert candidates[0].phone == "+201012345678"
    assert candidates[0].email == "mona@example.com"
    assert candidates[0].source == "whatsapp"
    assert [error.reason for error in errors] == [
        "missing_full_name",
        "invalid_phone",
        "invalid_email",
        "missing_contact",
    ]
    assert normalize_source("موقع الشركة") == "portal"
    assert normalize_source(None) == "import"


def test_dedupe_against_database_and_within_file() -> None:
    candidates, _ = map_rows(
        [
            {"full_name": "Existing", "phone": "01012345678"},
            {"full_name": "First", "phone": "01112345678", "email": "a@x.com"},
            {"full_name": "Copy In File", "phone": "01112345678"},
            {"full_name": "Mail Copy", "email": "A@x.com"},
            {"full_name": "Fresh", "email": "fresh@x.com"},
        ]
    )
    accepted, errors, duplicates = dedupe(candidates, {"+201012345678"}, set())
    assert [candidate.full_name for candidate in accepted] == ["First", "Fresh"]
    assert duplicates == 3
    assert [error.reason for error in errors] == [
        "duplicate_phone",
        "duplicate_phone",
        "duplicate_email",
    ]


def test_schemas_normalize_phone_email_and_validate_status() -> None:
    lead = CrmLeadCreate(full_name="Mona", phone="01012345678", email="Mona@Example.com")
    assert lead.phone == "+201012345678" and lead.email == "mona@example.com"
    assert lead.status.value == "new"
    with pytest.raises(ValidationError):
        CrmLeadCreate(full_name="Mona", phone="123")
    with pytest.raises(ValidationError):
        CrmBulkStatus(ids=[uuid4()], status="archived")  # type: ignore[arg-type]
    assert CrmLeadUpdate(phone="").phone is None


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> Any:
        return iter(list(self.rows))

    def scalar_one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeDB:
    def __init__(self) -> None:
        self.by_entity: dict[Any, list[Any]] = {}
        self.text_queue: list[list[Any]] = []
        self.scalar_value: Any = 0
        self.added: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        descriptions = getattr(stmt, "column_descriptions", None)
        if descriptions and len(descriptions) > 1:
            return FakeResult(list(self.text_queue.pop(0)) if self.text_queue else [])
        if descriptions:
            entity = descriptions[0].get("entity")
            if entity is not None:
                return FakeResult(list(self.by_entity.get(entity, [])))
        return FakeResult(list(self.text_queue.pop(0)) if self.text_queue else [])

    async def scalar(self, stmt: Any, params: Any = None) -> Any:
        return self.scalar_value

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        yield


def make_ctx(db: FakeDB, permissions: frozenset[str] | None = None) -> RequestContext:
    actor = Actor(id=STAFF, scope="staff")
    return RequestContext(
        db,  # type: ignore[arg-type]
        actor,
        uuid4(),
        1,
        AccessScope(actor=actor, all_companies=True, ownership=OwnershipLevel.ALL),
        permissions if permissions is not None else frozenset({"crm.view", "crm.manage"}),
        None,
        SimpleNamespace(),  # type: ignore[arg-type]
    )


def make_lead(**overrides: Any) -> CrmLead:
    values: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": CrmModuleRepository.tenant_id,
        "full_name": "Mona",
        "phone": "+201012345678",
        "source": "manual",
        "status": "new",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return CrmLead(**values)


def audits(db: FakeDB) -> list[Any]:
    return [row for row in db.added if row.__class__.__name__ == "AuditLog"]


async def test_lead_crud_bulk_and_activity_timeline() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    created = await api.create_lead(CrmLeadCreate(full_name="Mona", phone="01012345678"), ctx, None)
    assert created["status"] == "new"
    lead = next(row for row in db.added if isinstance(row, CrmLead))
    db.by_entity[CrmLead] = [lead]
    updated = await api.update_lead(lead.id, CrmLeadUpdate(status="contacted"), ctx, None)
    assert updated["status"] == "contacted"
    activity = await api.add_activity(
        lead.id, api.CrmActivityCreate(type="call", description="Called, interested"), ctx, None
    )
    assert activity["type"] == "call"
    assert (await api.bulk_status(CrmBulkStatus(ids=[lead.id], status="qualified"), ctx, None)) == {
        "updated": 1
    }
    assert lead.status == "qualified"
    detail = await api.get_lead(lead.id, ctx)
    assert detail["id"] == str(lead.id)
    assert (await api.delete_lead(lead.id, ctx, None)) == {"deleted": True}
    actions = {row.action for row in audits(db)}
    assert {"crm_lead_created", "crm_lead_updated", "crm_lead_deleted"} <= actions
    with pytest.raises(HTTPException, match="not found"):
        await api.get_lead(uuid4(), make_ctx(FakeDB()))


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


async def test_import_endpoint_preview_summary_and_error_ledger() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    db.text_queue = [[]]  # no existing emails
    csv = (
        b"full_name,phone,email,source\n"
        b"Mona,01012345678,mona@x.com,whatsapp\n"
        b"Copy,01012345678,,\nBad,123,,\nFresh,,fresh@x.com,\n"
    )
    preview = await api.preview_import(ctx, None, _upload("leads.csv", csv))
    assert preview["total_rows"] == 4 and preview["valid_rows"] == 3
    result = await api.import_leads(ctx, None, _upload("leads.csv", csv))
    assert result["success_rows"] == 2  # Mona + Fresh; Copy is a duplicate
    assert result["duplicates"] == 1 and result["failed_rows"] == 2
    batch = next(row for row in db.added if isinstance(row, CrmImportBatch))
    assert batch.status == "completed_with_errors"
    assert audits(db)[-1].action == "crm_import_completed"
    template = await api.import_template(ctx)
    assert template.media_type and "spreadsheet" in template.media_type
    assert len(template.body) > 100


async def test_stats_endpoint_shape() -> None:
    db = FakeDB()
    repo = CrmModuleRepository()
    db.scalar_value = 4
    db.text_queue = [
        [("new", 2), ("converted", 1)],  # status groups (len>1 descriptions path unused)
        [("whatsapp", 3)],
        [],
    ]
    # grouped() uses select(column, count) -> FakeDB serves aggregate queue.
    db.by_entity[CrmLead] = []
    db.by_entity[CrmImportBatch] = []
    stats = await repo.stats(db)  # type: ignore[arg-type]
    assert stats["total_leads"] == 4
    assert len(stats["last_30_days"]) == 30
    assert "conversion_rate" in stats and "recent_imports" in stats


def test_permission_gates_and_openapi_contract() -> None:
    gate = require_permission("crm.manage")
    denied = make_ctx(FakeDB(), permissions=frozenset({"crm.view"}))
    with pytest.raises(HTTPException) as raised:
        gate.verify(denied)
    assert raised.value.status_code == 403
    gate.verify(make_ctx(FakeDB()))  # crm.manage present
    assert require_permission("crm.view").code == "crm.view"
    paths = app.openapi()["paths"]
    for required in (
        "/api/crm/leads",
        "/api/crm/leads/bulk-assign",
        "/api/crm/imports",
        "/api/crm/imports/template.xlsx",
        "/api/crm/imports/{batch_id}/errors.csv",
        "/api/crm/stats",
    ):
        assert required in paths, required
    assert paths["/api/crm/leads"]["post"]["x-required-permission"] == "crm.manage"
    assert paths["/api/crm/stats"]["get"]["x-required-permission"] == "crm.view"
    for table in ("crm_leads", "crm_import_batches", "crm_import_errors", "crm_activities"):
        assert table in Base.metadata.tables, table
