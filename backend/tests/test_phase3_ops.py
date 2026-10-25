"""Phase 3 administration: SLA calendars, reports, integrations, duplicates.

Offline tests: repository calls are stubbed, and the SQL side of every guard is
enforced by PostgreSQL (verified separately against a real database).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api import catalog as catalog_api
from app.api import operations as operations_api
from app.api import reports as reports_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import (
    AssignmentRuleSpec,
    ComplaintFilter,
    ConnectionTestRequest,
    ExportRequest,
    IntegrationConfigSpec,
    ReportScheduleSpec,
    SandboxSendRequest,
    SlaRuleSpec,
)
from app.main import app
from app.services.reports import ReportService

CSRF = {"X-CSRF-Token": "known"}


def report_spec(**changes: object) -> ReportScheduleSpec:
    payload: dict[str, object] = {
        "name": "Weekly ops digest",
        "filters": {"window_days": 7, "priority": "high"},
        "cron_expression": "0 7 * * 0",
        "time_zone": "Africa/Cairo",
        "recipient_emails": ["Ops@Example.test"],
        "format": "xlsx",
        "next_run_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    payload.update(changes)
    return ReportScheduleSpec.model_validate(payload)


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    ctx = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        3,
        AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL),
        frozenset(),
        {},
        SimpleNamespace(
            parent=AsyncMock(return_value=SimpleNamespace(created_at=datetime.now(UTC)))
        ),
    )
    schedule_id = uuid4()
    reports = SimpleNamespace(
        schedules=AsyncMock(return_value=[]),
        save=AsyncMock(return_value=schedule_id),
        delete=AsyncMock(return_value=True),
        queue_now=AsyncMock(return_value=True),
        runs=AsyncMock(return_value=[]),
    )
    catalog_repo = SimpleNamespace(
        simulate_sla=AsyncMock(
            return_value={"matched": True, "sla_due_at": "2026-10-18T15:00:00Z"}
        ),
        test_business_hours=AsyncMock(return_value={"valid": True, "due_at": None}),
        save=AsyncMock(return_value=uuid4()),
        slas=AsyncMock(return_value=[]),
    )
    ops = SimpleNamespace(
        rules=AsyncMock(return_value=[]),
        save=AsyncMock(return_value=uuid4()),
        delete=AsyncMock(return_value=True),
        preview=AsyncMock(return_value={"matched": True, "candidates": []}),
        settings=AsyncMock(return_value=[]),
        open_candidates=AsyncMock(return_value=[]),
        scan_ticket=AsyncMock(return_value=2),
    )
    monkeypatch.setattr(reports_api, "ReportScheduleRepository", lambda: reports)
    monkeypatch.setattr(catalog_api, "CatalogRepository", lambda: catalog_repo)
    monkeypatch.setattr(operations_api, "AssignmentRuleRepository", lambda: ops)
    monkeypatch.setattr(operations_api, "DuplicateCandidateRepository", lambda: ops)

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "known"},
        ) as client:
            yield client, ctx, reports, catalog_repo, ops
    finally:
        app.dependency_overrides.clear()


def test_sla_business_hours_calendar_is_validated() -> None:
    spec = SlaRuleSpec.model_validate(
        {
            "company_id": None,
            "insurance_type": "health",
            "priority": "high",
            "response_minutes": 60,
            "resolution_minutes": 480,
            "alert_before_minutes": 30,
            "business_hours": {
                "time_zone": "Africa/Cairo",
                "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
                "holidays": ["2026-01-07"],
            },
        }
    )
    assert spec.business_hours is not None
    assert spec.business_hours.time_zone == "Africa/Cairo"
    assert spec.business_hours.weekly[0].weekday == 0
    assert "2026-01-07" in spec.business_hours.holidays
    for broken in (
        {"time_zone": "Mars/Olympus", "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}]},
        {"time_zone": "Africa/Cairo", "weekly": []},
        {"time_zone": "Africa/Cairo", "weekly": [{"weekday": 7, "start": "09:00", "end": "17:00"}]},
        {"time_zone": "Africa/Cairo", "weekly": [{"weekday": 0, "start": "17:00", "end": "09:00"}]},
        {
            "time_zone": "Africa/Cairo",
            "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
            "holidays": ["07/01/2026"],
        },
    ):
        with pytest.raises(ValidationError):
            SlaRuleSpec.model_validate(
                {
                    "response_minutes": 60,
                    "resolution_minutes": 120,
                    "business_hours": broken,
                }
            )
    assert (
        SlaRuleSpec.model_validate(
            {"response_minutes": 60, "resolution_minutes": 120}
        ).business_hours
        is None
    )


def test_report_integration_and_assignment_schemas_fail_closed() -> None:
    spec = report_spec()
    assert spec.recipient_emails == ["ops@example.test"]  # lowercased and de-duplicated
    assert spec.filters.window_days == 7
    with pytest.raises(ValidationError):  # absolute dates cannot be scheduled
        report_spec(filters={"window_days": 7, "date_from": "2026-10-01T00:00:00Z"})
    with pytest.raises(ValidationError):  # unknown time zone
        report_spec(time_zone="Nowhere/Here")
    with pytest.raises(ValidationError):  # cron characters that are not supported
        report_spec(cron_expression="0 7 * * MON")
    with pytest.raises(ValidationError):  # duplicate recipients
        report_spec(recipient_emails=["Ops@Example.test", "ops@example.test"])
    with pytest.raises(ValidationError):  # a partial-day window is not a scheduled window
        report_spec(filters={"window_days": 0})

    # The vault-name secret_ref path is gone; credentials are an encrypted patch.
    assert not hasattr(IntegrationConfigSpec, "secret_ref")
    partial = IntegrationConfigSpec.model_validate(
        {"provider": "smtp", "enabled": False, "credentials": {"from_email": "ops@example.test"}}
    )
    assert partial.credentials == {"from_email": "ops@example.test"}
    with pytest.raises(ValidationError):  # unknown credential keys are rejected
        IntegrationConfigSpec.model_validate(
            {"provider": "smtp", "credentials": {"vault_path": "vault/smtp"}}
        )
    with pytest.raises(ValidationError):  # clearing must also disable
        IntegrationConfigSpec.model_validate(
            {"provider": "smtp", "enabled": True, "clear_credentials": True}
        )
    with pytest.raises(ValidationError):  # inbound-only providers need a company
        IntegrationConfigSpec.model_validate({"provider": "imap", "enabled": False})
    with pytest.raises(ValidationError):  # scanning is platform-wide only
        IntegrationConfigSpec.model_validate(
            {"provider": "clamav", "company_id": str(uuid4()), "enabled": False}
        )
    with pytest.raises(ValidationError):  # app-password IMAP is not a Gmail/Outlook path
        IntegrationConfigSpec.model_validate(
            {
                "provider": "imap",
                "company_id": str(uuid4()),
                "enabled": False,
                "credentials": {
                    "host": "imap.example.test",
                    "username": "user@gmail.com",
                    "password": "app-password",
                },
            }
        )
    with pytest.raises(ValidationError):  # sandbox recipients are addresses or E.164 numbers
        IntegrationConfigSpec.model_validate(
            {
                "provider": "twilio_sms",
                "enabled": False,
                "credentials": {"sandbox_recipient": "not-a-number"},
            }
        )
    with pytest.raises(ValidationError):  # SMS country allowlist format
        IntegrationConfigSpec.model_validate(
            {
                "provider": "twilio_sms",
                "enabled": False,
                "credentials": {"allowed_country_codes": "Egypt"},
            }
        )
    probe = ConnectionTestRequest.model_validate({"provider": "gmail_oauth"})
    assert probe.company_id is None
    sandbox = SandboxSendRequest.model_validate({"provider": "twilio_sms", "confirm": True})
    assert sandbox.confirm

    with pytest.raises(ValidationError):
        AssignmentRuleSpec.model_validate({"strategy": "round_robin"})
    rule = AssignmentRuleSpec.model_validate(
        {"strategy": "by_company", "target_team_id": str(uuid4())}
    )
    assert rule.strategy == "by_company" and rule.sort_order == 100

    assert (
        ExportRequest.model_validate(
            {
                "kind": "complaints",
                "filters": {"date_from": "2026-10-01T00:00:00Z", "date_to": "2026-10-08T00:00:00Z"},
            }
        ).format
        == "csv"
    )
    with pytest.raises(ValidationError):
        ExportRequest.model_validate(
            {
                "kind": "complaints",
                "format": "docx",
                "filters": {"date_from": "2026-10-01T00:00:00Z", "date_to": "2026-10-08T00:00:00Z"},
            }
        )


def test_cron_schedule_is_evaluated_in_the_schedule_time_zone() -> None:
    cairo = ReportService.next_run_at(
        "0 7 * * 0", "Africa/Cairo", datetime(2026, 10, 15, 12, tzinfo=UTC)
    )
    assert cairo == datetime(2026, 10, 18, 4, 0, tzinfo=UTC)  # Sunday 07:00 Cairo = UTC+3
    assert ReportService.next_run_at(
        "*/15 * * * *", "UTC", datetime(2026, 10, 15, 12, 7, tzinfo=UTC)
    ) == datetime(2026, 10, 15, 12, 15, tzinfo=UTC)
    # Month rollover and DST: 06:30 Cairo on the 1st is 04:30 UTC before the change.
    assert ReportService.next_run_at(
        "30 6 1 * *", "Africa/Cairo", datetime(2026, 10, 15, 12, tzinfo=UTC)
    ) == datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    assert ReportService.next_run_at(
        "0 0 29 2 *", "UTC", datetime(2027, 3, 1, tzinfo=UTC)
    ) == datetime(2028, 2, 29, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError):  # February never has 31 days
        ReportService.next_run_at("0 0 31 2 *", "UTC", datetime(2026, 3, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        ReportService.next_run_at("0 0 * * ", "UTC", datetime(2026, 3, 1, tzinfo=UTC))


async def test_reports_api_requires_permission_and_csrf(api: tuple) -> None:
    client, ctx, repo, _, _ = api
    url = "/api/staff/reports/schedules"
    body = report_spec().model_dump(mode="json")
    assert (await client.get(url)).status_code == 403
    ctx.permissions = frozenset({"reports.manage"})
    assert (await client.post(url, json=body)).status_code == 403  # CSRF header missing
    assert (
        await client.post(url, json=body, headers={"X-CSRF-Token": "known"})
    ).status_code == 403  # analytics.view/export still missing
    ctx.permissions = frozenset({"reports.manage", "analytics.view", "analytics.export"})
    created = await client.post(url, json=body, headers={"X-CSRF-Token": "known"})
    assert created.status_code == 201 and created.json()["id"] == str(repo.save.return_value)
    payload = repo.save.call_args.args[2]
    assert '"window_days":7' in payload and '"format":"xlsx"' in payload
    assert repo.save.call_args.args[1] is None
    assert (await client.delete(url, headers={"X-CSRF-Token": "known"})).status_code in (404, 405)
    schedule_id = repo.save.return_value
    assert (await client.get(f"{url}/{schedule_id}/runs?page_size=101")).status_code == 422
    assert (
        await client.post(f"{url}/{schedule_id}/run", headers={"X-CSRF-Token": "known"})
    ).status_code == 202
    repo.queue_now.return_value = False
    assert (
        await client.post(f"{url}/{schedule_id}/run", headers={"X-CSRF-Token": "known"})
    ).status_code == 404


async def test_sla_simulation_and_calendar_preview_are_permission_bound(api: tuple) -> None:
    client, ctx, _, repo, _ = api
    simulate = {
        "company_id": str(uuid4()),
        "insurance_type": "health",
        "priority": "urgent",
        "created_at": "2026-10-15T09:00:00Z",
    }
    denied = await client.post("/api/staff/admin/sla-rules/simulate", json=simulate, headers=CSRF)
    assert denied.status_code == 403
    ctx.permissions = frozenset({"sla.manage"})
    preview = await client.post("/api/staff/admin/sla-rules/simulate", json=simulate, headers=CSRF)
    assert preview.status_code == 200 and preview.json()["matched"] is True
    assert repo.simulate_sla.call_args.args[1:4] == (
        UUID(simulate["company_id"]),
        "health",
        "urgent",
    )
    calendar = {
        "business_hours": {
            "time_zone": "Africa/Cairo",
            "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
        },
        "minutes": 480,
        "from_at": "2026-10-15T09:00:00Z",
    }
    response = await client.post(
        "/api/staff/admin/sla-rules/business-hours/preview", json=calendar, headers=CSRF
    )
    assert response.status_code == 200 and response.json()["valid"] is True
    assert repo.test_business_hours.call_args.args[1]["time_zone"] == "Africa/Cairo"
    repo.simulate_sla.return_value = None
    assert (
        await client.post("/api/staff/admin/sla-rules/simulate", json=simulate, headers=CSRF)
    ).status_code == 403
    # An empty calendar means continuous coverage and is not a validation error.
    assert SlaRuleSpec.model_validate({"response_minutes": 30, "resolution_minutes": 60})
    assert (
        await client.post(
            "/api/staff/admin/sla-rules/business-hours/preview",
            json={**calendar, "business_hours": None},
            headers=CSRF,
        )
    ).status_code == 200


async def test_integration_assignment_and_duplicate_routes(api: tuple) -> None:
    client, ctx, _, _, repo = api
    # The vault-name secret_ref route is retired: real credentials live only in
    # the encrypted integration-connections store.
    assert (await client.get("/api/staff/admin/integrations")).status_code == 404
    assert (
        await client.post(
            "/api/staff/admin/integrations",
            json={"provider": "email", "secret_ref": "vault/smtp", "is_enabled": True},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 404
    assert (await client.get("/api/staff/admin/integration-connections")).status_code == 403

    ctx.permissions = frozenset({"complaints.assign"})
    rule = {
        "company_id": None,
        "strategy": "least_workload",
        "target_team_id": str(uuid4()),
        "sort_order": 10,
    }
    assert (
        await client.post(
            "/api/staff/admin/assignment-rules", json=rule, headers={"X-CSRF-Token": "known"}
        )
    ).status_code == 201
    assert repo.save.call_args.args[1] is None
    assert repo.save.call_args.args[2].strategy == "least_workload"
    assert (
        await client.post(
            "/api/staff/admin/assignment-rules/preview",
            json={"company_id": str(uuid4()), "insurance_type": "health"},
            headers=CSRF,
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/staff/admin/assignment-rules",
            json={**rule, "target_team_id": None},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 422

    ctx.permissions = frozenset({"complaints.merge"})
    assert (await client.get("/api/staff/admin/duplicates?limit=500")).status_code == 422
    assert (await client.get("/api/staff/admin/duplicates")).json() == []
    complaint_id = uuid4()
    scanned = await client.post(
        f"/api/staff/admin/duplicates/scan/{complaint_id}", headers={"X-CSRF-Token": "known"}
    )
    assert scanned.status_code == 202 and scanned.json() == {"candidates": 2}
    assert ctx.complaints.parent.await_count == 1


def test_attachment_and_pdf_renderers_are_bounded() -> None:
    from app.services.render import csv_bytes, pdf_bytes, tabular_bytes, xlsx_bytes

    headers = ["ticket_no", "subject"]
    rows = [["CMP-2026-000001", "=cmd|'/c calc'!A1"], ["CMP-2026-000002", "Late refund"]]
    csv_data = csv_bytes(headers, rows)
    assert csv_data.startswith(b"ticket_no,subject") and b"CMP-2026-000002" in csv_data
    xlsx_data = xlsx_bytes("complaints", headers, rows)
    assert xlsx_data[:2] == b"PK"
    pdf_data = pdf_bytes("Complaints", headers, rows)
    assert pdf_data.startswith(b"%PDF-1.4") and pdf_data.rstrip().endswith(b"%%EOF")
    assert tabular_bytes("csv", "x", headers, rows) == csv_data
    with pytest.raises(ValueError):
        tabular_bytes("docx", "x", headers, rows)


async def test_report_worker_reauthorizes_and_records_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker

    run = SimpleNamespace(
        run_id=uuid4(),
        schedule_id=uuid4(),
        owner_staff_id=uuid4(),
        name="Weekly ops digest",
        filters={"window_days": 7},
        format="csv",
        recipients=["ops@example.test"],
        attempt=1,
        cron_expression="0 7 * * 0",
        time_zone="Africa/Cairo",
    )
    row = SimpleNamespace(
        ticket_no="CMP-2026-000001",
        created_at=datetime.now(UTC),
        company_id=uuid4(),
        insurance_type="health",
        status="new",
        priority="normal",
        subject="Late refund",
        policy_number="POL-1",
        sla_due_at=None,
        sla_breached=False,
        resolved_at=None,
        csat_score=None,
    )
    claims: list[SimpleNamespace] = [run]
    report_rows: list[SimpleNamespace] = [row]
    finishes: list[dict] = []

    async def query(engine: object, sql: str, params: dict):
        if "claim_due_reports" in sql:
            return claims
        if "report_rows" in sql:
            return report_rows
        if "finish_report_run" in sql:
            finishes.append(params)
            return [SimpleNamespace(finished=True)]
        return []

    monkeypatch.setattr(worker, "_worker_query", query)
    sender = SimpleNamespace(send_report=AsyncMock(return_value=True))
    context = {"engine": object(), "delivery": sender}

    assert await worker.deliver_report_runs(context) == "processed 1 scheduled report(s)"
    assert finishes[-1]["state"] == "sent" and finishes[-1]["rows"] == 1
    following = finishes[-1]["next"]
    assert following > datetime.now(UTC)
    # Sun 07:00 Cairo wall-clock; the UTC hour differs across Egypt's DST change.
    Cairo = following.astimezone(ZoneInfo("Africa/Cairo"))
    assert (Cairo.weekday(), Cairo.hour, Cairo.minute) == (6, 7, 0)
    assert finishes[-1]["error"] is None
    _, subject, body, name, media, data = sender.send_report.call_args.args
    assert subject.startswith("Scheduled complaint report") and name.endswith(".csv")
    assert "confidential" in body and "1 ticket(s)" in body
    assert media.startswith("text/csv") and b"CMP-2026-000001" in data

    # An unconfigured provider is a recorded failure, never a silent success.
    sender.send_report.return_value = False
    await worker.deliver_report_runs(context)
    assert finishes[-1]["state"] == "failed"
    assert finishes[-1]["error"] == "delivery provider unavailable"

    # An empty scope is recorded as empty instead of mailing an empty sheet.
    claims[:] = []
    report_rows.clear()
    claims.append(run)
    assert await worker.deliver_report_runs(context) == "processed 1 scheduled report(s)"
    assert finishes[-1]["state"] == "empty" and finishes[-1]["rows"] == 0

    # A renderer error still finishes the run so the schedule cannot wedge.
    report_rows.append(row)
    sender.send_report.side_effect = RuntimeError("SMTP down")
    await worker.deliver_report_runs(context)
    assert finishes[-1]["state"] == "failed" and "SMTP down" in finishes[-1]["error"]


async def test_maintenance_jobs_only_call_worker_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker

    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        return [SimpleNamespace(inserted=4, rows=9)]

    monkeypatch.setattr(worker, "_worker_query", query)
    engine = {"engine": object()}
    assert await worker.scan_duplicates(engine) == "recorded 4 duplicate candidate(s)"
    assert await worker.refresh_analytics_rollup(engine) == (
        "refreshed analytics rollup with 9 row(s)"
    )
    assert all("app_security." in sql for sql, _ in calls)
    assert calls[0][1] == {"limit": 50} and calls[1][1] == {}
    cron_names = {job.name for job in worker.WorkerSettings.cron_jobs}
    assert {
        "cron:deliver_report_runs",
        "cron:scan_duplicates",
        "cron:refresh_analytics_rollup",
    } <= cron_names


async def test_analytics_rollup_uses_the_materialized_view_only_when_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fakeredis.aioredis

    from app.repositories.analytics import whole_utc_days
    from app.services.analytics import AnalyticsService

    cache = fakeredis.aioredis.FakeRedis()
    try:
        live = {
            "kpis": {"total": 2},
            "time_series": [{"date": "2026-10-01", "arrived": 2}],
            "by_status": [],
            "by_company": [],
            "by_type": [],
            "by_agent": [],
        }
        rollup_rows = [
            {
                "day": date(2026, 10, 1),
                "company_id": uuid4(),
                "insurance_type": "health",
                "priority": "high",
                "category_id": uuid4(),
                "channel": "portal",
                "status": "new",
                "agent_id": uuid4(),
                "team_id": uuid4(),
                "sla_breached": True,
                "response_breached": False,
                "reopened": False,
                "total": 2,
                "resolved_count": 2,
                "resolution_hours_sum": 20.0,
                "csat_sum": 9.0,
                "csat_count": 2,
                "first_response_count": 2,
                "first_response_hours_sum": 4.0,
            }
        ]
        calls: list[object] = []

        async def rows_for(db: object, scope: object, filters: ComplaintFilter):
            # Mirrors AnalyticsRepository.rollup_rows: the day granularity
            # refuses partial days and free-text search, so the service falls back.
            calls.append(filters)
            return rollup_rows if whole_utc_days(filters) else None

        repo = SimpleNamespace(
            report=AsyncMock(return_value=live),
            rollup_fresh=AsyncMock(return_value=True),
            rollup_rows=rows_for,
        )
        service = AnalyticsService(repo, cache)
        actor = Actor(uuid4(), "staff")
        scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
        aligned = ComplaintFilter(
            date_from=datetime(2026, 10, 1, tzinfo=UTC),
            date_to=datetime(2026, 10, 2, tzinfo=UTC),
        )
        assert whole_utc_days(aligned) is True
        result = await service.rollup(SimpleNamespace(), scope, aligned, 1)
        assert result["source"] == "rollup" and result["kpis"]["total"] == 2
        assert result["kpis"]["avg_resolution_hours"] == 10.0
        assert result["kpis"]["csat_avg"] == 4.5
        assert result["kpis"]["sla_breaches"] == 2
        assert result["time_series"] == [{"date": "2026-10-01", "arrived": 2}]

        # A stale view, a partial day and a search fall back to the live query.
        partial = ComplaintFilter(
            date_from=datetime(2026, 10, 1, 6, tzinfo=UTC),
            date_to=datetime(2026, 10, 2, tzinfo=UTC),
        )
        assert whole_utc_days(partial) is False
        fallback = await service.rollup(SimpleNamespace(), scope, partial, 1)
        assert fallback["source"] == "live" and len(calls) == 2
        repo.rollup_fresh.return_value = False
        stale = await service.rollup(SimpleNamespace(), scope, aligned, 2)
        assert stale["source"] == "live" and stale["rollup_fresh"] is False
    finally:
        await cache.aclose()


async def test_ticket_sla_state_reports_risk_without_leaking_other_tickets() -> None:
    from app.repositories.complaints import ComplaintRepository

    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    now = datetime.now(UTC)
    values = {
        "id": uuid4(),
        "ticket_no": "CMP-2026-000009",
        "status": "in_review",
        "created_at": now - timedelta(hours=5),
        "response_due_at": now - timedelta(hours=4),
        "sla_due_at": now + timedelta(minutes=30),
        "sla_alert_at": now - timedelta(minutes=10),
        "sla_alerted_at": None,
        "first_response_at": now - timedelta(hours=4),
        "response_breached_at": None,
        "sla_breached_at": None,
        "resolved_at": None,
    }
    row = SimpleNamespace(_mapping=values)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row)))
    state = await ComplaintRepository().sla_state(db, scope, values["id"])
    assert state is not None
    assert state["state"] == "at_risk" and state["remaining_minutes"] in (29, 30)
    assert state["responded"] is True
    sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "complaints.company_id IN" in sql.upper() or "true" in sql
    assert "national_id" not in sql

    breached = SimpleNamespace(_mapping={**values, "sla_breached_at": now})
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: breached))
    )
    breached_state = await ComplaintRepository().sla_state(db, scope, values["id"])
    assert breached_state is not None and breached_state["state"] == "breached"
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: None)))
    assert await ComplaintRepository().sla_state(db, scope, uuid4()) is None


async def test_worker_warns_before_breaching(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    calls: list[str] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append(sql)
        return [SimpleNamespace(processed=2)]

    monkeypatch.setattr(worker, "_worker_query", query)
    assert await worker.warn_sla({"engine": object()}) == (
        "warned 2 ticket(s) before their SLA deadline"
    )
    assert "process_sla_warnings" in calls[0]
    order = [job.name for job in worker.WorkerSettings.cron_jobs]
    assert order.index("cron:warn_sla") < order.index("cron:check_sla")


async def test_audit_exports_share_one_bounded_row_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from httpx import ASGITransport, AsyncClient

    from app.api import audit as audit_api

    actor = Actor(uuid4(), "staff")
    ctx = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        4,
        AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL),
        frozenset({"audit.view"}),
        {},
        SimpleNamespace(),
    )
    rows = [
        {
            "id": uuid4(),
            "created_at": datetime(2026, 10, 15, 9, 0, tzinfo=UTC),
            "actor_staff_id": actor.id,
            "action": "=cmd|calc",
            "resource_type": "complaint",
            "resource_id": uuid4(),
            "complaint_id": None,
        }
    ]
    repo = SimpleNamespace(events=AsyncMock(return_value=rows))
    monkeypatch.setattr(audit_api, "AuditRepository", lambda: repo)
    limits = SimpleNamespace(check=AsyncMock())
    original_auth = getattr(app.state, "auth", None)
    app.state.auth = SimpleNamespace(limits=limits)

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            csv_response = await client.get("/api/staff/audit/events.csv")
            xlsx_response = await client.get("/api/staff/audit/events.xlsx")
    finally:
        app.dependency_overrides.clear()
        if original_auth is None:
            del app.state.auth
        else:
            app.state.auth = original_auth
    assert csv_response.status_code == 200 and xlsx_response.status_code == 200
    assert csv_response.headers["X-Export-Limit"] == "2000"
    assert csv_response.content.startswith(b"id,created_at")
    assert b"'=cmd|calc" in csv_response.content  # formula cells are neutralised
    assert xlsx_response.content[:2] == b"PK"
    assert limits.check.await_count == 2
    assert all(call.args[3] == 2000 for call in repo.events.call_args_list)
