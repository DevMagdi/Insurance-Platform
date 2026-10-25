"""Unified CRM: Excel parsing, pipeline stages, tasks, handlers, stats, permissions."""

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
    CrmBulkStage,
    CrmLeadCreate,
    CrmLeadMove,
    CrmLeadUpdate,
    CrmStageCreate,
    CrmStageReorder,
    CrmStageUpdate,
    CrmTaskCreate,
)
from app.domain.enums import OwnershipLevel
from app.main import app
from app.models import Base, CrmLead
from app.models.crm_module import CrmImportBatch, CrmPipelineStage, CrmTask
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


def test_schemas_normalize_phone_email_and_validate_stage_codes() -> None:
    lead = CrmLeadCreate(full_name="Mona", phone="01012345678", email="Mona@Example.com")
    assert lead.phone == "+201012345678" and lead.email == "mona@example.com"
    assert lead.stage_code == "new" and lead.priority.value == "normal"
    with pytest.raises(ValidationError):
        CrmLeadCreate(full_name="Mona", phone="123")
    with pytest.raises(ValidationError):
        CrmBulkStage(ids=[uuid4()], stage_code="Not A Stage!")
    with pytest.raises(ValidationError):
        CrmStageCreate(code="won_lost", name_ar="x", name_en="x", is_won=True, is_lost=True)
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
        "lead_no": "LEAD-2026-ABC123",
        "source": "manual",
        "stage_code": "new",
        "priority": "normal",
        "insurance_type": "general",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return CrmLead(**values)


def make_stage(code: str = "new", **overrides: Any) -> CrmPipelineStage:
    values: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": CrmModuleRepository.tenant_id,
        "code": code,
        "name_ar": code,
        "name_en": code,
        "sort_order": 10,
        "is_won": False,
        "is_lost": False,
        "is_converted": False,
        "is_active": True,
    }
    values.update(overrides)
    return CrmPipelineStage(**values)


def audits(db: FakeDB) -> list[Any]:
    return [row for row in db.added if row.__class__.__name__ == "AuditLog"]


async def test_lead_crud_bulk_and_activity_timeline() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    db.by_entity[CrmPipelineStage] = [make_stage("new")]
    created = await api.create_lead(CrmLeadCreate(full_name="Mona", phone="01012345678"), ctx, None)
    assert created["stage_code"] == "new" and created["lead_no"].startswith("LEAD-")
    lead = next(row for row in db.added if isinstance(row, CrmLead))
    db.by_entity[CrmLead] = [lead]
    db.by_entity[CrmPipelineStage] = [make_stage("contacted")]
    updated = await api.update_lead(lead.id, CrmLeadUpdate(stage_code="contacted"), ctx, None)
    assert updated["stage_code"] == "contacted"
    activity = await api.add_activity(
        lead.id, api.CrmActivityCreate(type="call", description="Called, interested"), ctx, None
    )
    assert activity["type"] == "call"
    db.by_entity[CrmPipelineStage] = [make_stage("needs_info")]
    bulk = await api.bulk_stage(CrmBulkStage(ids=[lead.id], stage_code="needs_info"), ctx, None)
    assert bulk == {"updated": 1}
    assert lead.stage_code == "needs_info"
    detail = await api.get_lead(lead.id, ctx)
    assert detail["id"] == str(lead.id) and "tasks" in detail
    assert (await api.delete_lead(lead.id, ctx, None)) == {"deleted": True}
    actions = {row.action for row in audits(db)}
    assert {"crm_lead_created", "crm_lead_updated", "crm_lead_deleted"} <= actions
    with pytest.raises(HTTPException, match="not found"):
        await api.get_lead(uuid4(), make_ctx(FakeDB()))


async def test_stage_lifecycle_protects_data_and_the_intake_stage() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    created = await api.create_stage(
        CrmStageCreate(code="site_visit", name_ar="زيارة موقع", name_en="Site visit"), ctx, None
    )
    assert created["code"] == "site_visit"
    stage = next(row for row in db.added if isinstance(row, CrmPipelineStage))
    db.by_entity[CrmPipelineStage] = [stage]
    db.scalar_value = 3  # leads still in the stage
    with pytest.raises(HTTPException) as raised:
        await api.delete_stage(stage.id, ctx, None)
    assert raised.value.status_code == 409
    db.scalar_value = 0
    assert (await api.delete_stage(stage.id, ctx, None)) == {"deleted": True}
    # The intake stage can never be removed or deactivated.
    intake = make_stage("new")
    db.by_entity[CrmPipelineStage] = [intake]
    with pytest.raises(HTTPException) as raised:
        await api.update_stage(intake.id, CrmStageUpdate(is_active=False), ctx, None)
    assert raised.value.status_code == 422
    # Reorder must list every stage exactly once.
    db.by_entity[CrmPipelineStage] = [make_stage("a"), make_stage("b")]
    with pytest.raises(HTTPException) as raised:
        await api.reorder_stages(CrmStageReorder(ids=[uuid4()]), ctx, None)
    assert raised.value.status_code == 422
    order = [stage.id for stage in db.by_entity[CrmPipelineStage]][::-1]
    result = await api.reorder_stages(CrmStageReorder(ids=order), ctx, None)
    assert [row["sort_order"] for row in result["stages"]] == [10, 20]
    # Moving into an inactive/unknown stage is refused.
    lead = make_lead()
    db.by_entity[CrmLead] = [lead]
    db.by_entity[CrmPipelineStage] = [make_stage("ghost", is_active=False)]
    with pytest.raises(HTTPException) as raised:
        await api.move_lead(lead.id, CrmLeadMove(stage_code="ghost"), ctx, None)
    assert raised.value.status_code == 422


async def test_move_to_lost_stage_stamps_closed_at_and_reason() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    lead = make_lead()
    db.by_entity[CrmLead] = [lead]
    db.by_entity[CrmPipelineStage] = [make_stage("lost", is_lost=True)]
    moved = await api.move_lead(
        lead.id, CrmLeadMove(stage_code="lost", lost_reason="Chose a competitor"), ctx, None
    )
    assert moved["stage_code"] == "lost" and moved["closed_at"] is not None
    assert lead.lost_reason == "Chose a competitor"
    # Reopening clears the close stamp.
    db.by_entity[CrmPipelineStage] = [make_stage("contacted")]
    moved = await api.move_lead(lead.id, CrmLeadMove(stage_code="contacted"), ctx, None)
    assert moved["closed_at"] is None


async def test_task_lifecycle_arms_the_lead_followup() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    lead = make_lead()
    db.by_entity[CrmLead] = [lead]
    due = datetime.now(UTC)
    db.scalar_value = due  # refresh_next_followup reads min(due_at)
    created = await api.create_task(
        lead.id, CrmTaskCreate(type="call", title="Chase", due_at=due), ctx, None
    )
    assert created["status"] == "pending" and created["lead_no"] == lead.lead_no
    task = next(row for row in db.added if isinstance(row, CrmTask))
    assert lead.next_followup_at == due
    db.by_entity[CrmTask] = [task]
    db.scalar_value = None
    done = await api.complete_task(task.id, ctx, None)
    assert done["status"] == "done" and done["completed_at"] is not None
    assert lead.next_followup_at is None
    actions = {row.action for row in audits(db)}
    assert {"crm_task_created", "crm_task_completed"} <= actions
    assert (await api.delete_task(task.id, ctx, None)) == {"deleted": True}


async def test_convert_refuses_a_second_conversion() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    lead = make_lead(converted_corporate_client_id=uuid4())
    db.by_entity[CrmLead] = [lead]
    with pytest.raises(HTTPException) as raised:
        await api.convert_lead(lead.id, api.CrmLeadConvert(), ctx, None)
    assert raised.value.status_code == 409


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
    actor = Actor(id=STAFF, scope="staff")
    scope = AccessScope(actor=actor, all_companies=True, ownership=OwnershipLevel.ALL)
    db.scalar_value = 4
    perf_row = SimpleNamespace(
        _mapping={
            "staff_id": str(STAFF),
            "full_name": "Sales",
            "leads": 4,
            "won": 1,
            "lost": 1,
            "converted": 1,
            "won_value": 100,
            "pipeline_value": 250,
            "avg_close_days": None,
            "tasks_done": 2,
            "tasks_overdue": 1,
        }
    )
    db.text_queue = [
        [("new", 2), ("converted", 1)],  # stage groups
        [("whatsapp", 3)],  # source groups
        [],  # daily series
        [perf_row],  # sales performance (top performers)
    ]
    db.by_entity[CrmPipelineStage] = [make_stage("new"), make_stage("converted", is_converted=True)]
    db.by_entity[CrmLead] = []
    db.by_entity[CrmImportBatch] = []
    stats = await repo.stats(db, scope)  # type: ignore[arg-type]
    assert stats["total_leads"] == 4
    assert len(stats["last_30_days"]) == 30
    assert stats["by_stage"][0]["count"] == 2 and stats["by_stage"][1]["count"] == 1
    assert stats["conversion_rate"] == 25.0
    assert stats["top_performers"][0]["conversion_rate"] == 25.0
    assert stats["due_tasks"]["overdue"] == 4  # FakeDB scalar


async def test_pipeline_board_counts_values_and_cards() -> None:
    db = FakeDB()
    repo = CrmModuleRepository()
    lead = make_lead(stage_code="negotiation", expected_premium=None)
    db.by_entity[CrmPipelineStage] = [
        make_stage("negotiation"),
        make_stage("won", is_won=True),
        make_stage("lost", is_lost=True),
    ]
    db.by_entity[CrmLead] = [lead]
    db.text_queue = [[("negotiation", 1, 900), ("won", 2, 500), ("lost", 1, 0)]]
    board = await repo.pipeline_board(db)  # type: ignore[arg-type]
    assert board["total"] == 4 and board["won"] == 2 and board["lost"] == 1
    assert board["win_rate"] == 66.7
    assert board["open_pipeline_value"] == "900"
    negotiation = board["stages"][0]
    assert negotiation["count"] == 1 and negotiation["leads"][0]["id"] == str(lead.id)


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
        "/api/crm/leads/bulk-stage",
        "/api/crm/leads/{lead_id}/move",
        "/api/crm/leads/{lead_id}/convert",
        "/api/crm/leads/{lead_id}/tasks",
        "/api/crm/stages",
        "/api/crm/stages/reorder",
        "/api/crm/pipeline",
        "/api/crm/tasks",
        "/api/crm/reports/sales-performance",
        "/api/crm/imports",
        "/api/crm/imports/template.xlsx",
        "/api/crm/imports/{batch_id}/errors.csv",
        "/api/crm/stats",
    ):
        assert required in paths, required
    # The split sales router is gone: CRM lives only under /api/crm.
    assert not any(path.startswith("/api/staff/crm") for path in paths)
    convert_perm = paths["/api/crm/leads/{lead_id}/convert"]["post"]["x-required-permission"]
    assert convert_perm == "crm.convert"
    assert paths["/api/crm/leads"]["post"]["x-required-permission"] == "crm.manage"
    assert paths["/api/crm/stats"]["get"]["x-required-permission"] == "crm.view"
    for table in (
        "crm_leads",
        "crm_pipeline_stages",
        "crm_tasks",
        "crm_import_batches",
        "crm_import_errors",
        "crm_activities",
    ):
        assert table in Base.metadata.tables, table
    for gone in ("leads", "pipeline_stages", "lead_activities"):
        assert gone not in Base.metadata.tables, gone


async def test_create_lead_with_followup_and_stage_validation() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    db.by_entity[CrmPipelineStage] = [make_stage("new")]
    when = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    db.scalar_value = when
    created = await api.create_lead(
        CrmLeadCreate(full_name="Sara", company_name="Nile", next_followup_at=when), ctx, None
    )
    lead = next(row for row in db.added if isinstance(row, CrmLead))
    task = next(row for row in db.added if isinstance(row, CrmTask))
    assert task.title == "Follow-up" and task.status == "pending"
    assert created["next_followup_at"] is not None and lead.next_followup_at == when
    db.by_entity[CrmPipelineStage] = []
    with pytest.raises(HTTPException) as raised:
        await api.create_lead(CrmLeadCreate(full_name="Sara", stage_code="nope"), ctx, None)
    assert raised.value.status_code == 422


async def test_task_update_cancel_and_list_endpoints() -> None:
    db = FakeDB()
    ctx = make_ctx(db)
    lead = make_lead()
    task = CrmTask(
        id=uuid4(),
        tenant_id=CrmModuleRepository.tenant_id,
        lead_id=lead.id,
        type="call",
        title="Ring",
        due_at=datetime.now(UTC),
        status="pending",
    )
    db.by_entity[CrmTask] = [task]
    db.by_entity[CrmLead] = [lead]
    db.by_entity[CrmPipelineStage] = [make_stage("new")]
    from app.domain.crm_module_schemas import CrmTaskUpdate

    updated = await api.update_task(task.id, CrmTaskUpdate(status="cancelled"), ctx, None)
    assert updated["status"] == "cancelled" and updated["completed_at"] is None
    db.text_queue = [[(task, lead)]]
    db.scalar_value = 0
    listing = await api.list_tasks(ctx)
    assert listing["tasks"][0]["title"] == "Ring" and listing["due_counts"]["overdue"] == 0
    stages = await api.list_stages(ctx)
    assert stages["stages"][0]["code"] == "new" and stages["stages"][0]["count"] == 0
    page = await api.list_leads(
        ctx, None, None, None, None, None, None, None, None, "created_at", "desc", 1, 20
    )
    assert page["items"][0]["lead_no"] == lead.lead_no
    perf_row = {
        "staff_id": str(STAFF),
        "full_name": "Sales",
        "leads": 1,
        "won": 0,
        "lost": 0,
        "converted": 0,
        "won_value": "0",
        "pipeline_value": "10",
        "avg_close_days": None,
        "tasks_done": 0,
        "tasks_overdue": 0,
    }
    db.text_queue = [[SimpleNamespace(_mapping=perf_row)]]
    report = await api.sales_performance(ctx)
    assert report["salespeople"][0]["full_name"] == "Sales"
