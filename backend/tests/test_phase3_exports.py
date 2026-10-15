"""Async exports fail closed, sanitize CSV, and require permission + CSRF."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app import worker
from app.api import exports as exports_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import ExportRequest
from app.main import app
from app.services.exports import render_export


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    ctx = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        2,
        AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL),
        frozenset(),
        {},
        SimpleNamespace(),
    )
    job = uuid4()
    repo = SimpleNamespace(
        request=AsyncMock(return_value=job),
        list=AsyncMock(return_value=[{"id": job, "status": "queued"}]),
        authorize_download=AsyncMock(return_value=f"exports/{job}/1.csv"),
    )
    storage = SimpleNamespace(get=AsyncMock(return_value=b"ticket_no\r\nCMP-2026-000001\r\n"))
    auth = SimpleNamespace(limits=SimpleNamespace(check=AsyncMock()))
    monkeypatch.setattr(exports_api, "ExportRepository", lambda: repo)
    app.state.auth = auth
    app.state.storage = storage

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "known"},
        ) as client:
            yield client, ctx, repo, storage
    finally:
        app.dependency_overrides.clear()


async def test_async_export_permission_rate_csrf_and_download(api: tuple) -> None:
    client, ctx, repo, storage = api
    filters = {"date_from": "2026-09-01T00:00:00Z", "date_to": "2026-10-01T00:00:00Z"}
    path = "/api/staff/exports"
    payload = {"kind": "complaints", "filters": filters}
    header = {"X-CSRF-Token": "known"}
    assert (await client.post(path, json=payload, headers=header)).status_code == 403
    ctx.permissions = frozenset({"complaints.view", "complaints.export"})
    assert (await client.post(path, json=payload)).status_code == 403
    assert (
        await client.post(path, json={"kind": "analytics", "filters": filters}, headers=header)
    ).status_code == 403
    assert (
        await client.post(path, json={"kind": "complaints", "filters": {}}, headers=header)
    ).status_code == 422
    response = await client.post(path, json=payload, headers=header)
    assert response.status_code == 202 and response.json()["status"] == "queued"
    assert repo.request.call_args.args[1].filters.date_from is not None
    assert (await client.get(path)).json()[0]["status"] == "queued"
    repo.authorize_download.return_value = None
    assert (await client.get(f"{path}/{response.json()['id']}/download")).status_code == 404
    repo.authorize_download.return_value = f"exports/{response.json()['id']}/1.csv"
    exported = await client.get(f"{path}/{response.json()['id']}/download")
    assert exported.status_code == 200 and exported.content.startswith(b"ticket_no")
    assert exported.headers["cache-control"] == "no-store"
    storage.get.assert_awaited_once()


def test_export_window_and_spreadsheet_safety() -> None:
    with pytest.raises(ValidationError):
        ExportRequest.model_validate(
            {
                "kind": "complaints",
                "filters": {"date_from": "2025-01-01T00:00:00Z", "date_to": "2026-10-01T00:00:00Z"},
            }
        )
    row = SimpleNamespace(
        complaint_id=uuid4(),
        complaint_created_at=datetime.now(UTC),
        ticket_no="CMP-2026-000001",
        created_at=datetime.now(UTC),
        company_id=uuid4(),
        insurance_type="health",
        status="new",
        subject='=HYPERLINK("http://evil.invalid")',
        policy_number="+123456",
        sla_breached=True,
        resolved_at=None,
        csat_score=None,
    )
    data = render_export("complaints", [row]).decode("utf-8")
    assert "'=HYPERLINK" in data and "'+123456" in data
    analytic = render_export("analytics", [row]).decode("utf-8")
    assert str(row.company_id) in analytic and "sla_breaches" in analytic
    assert "HYPERLINK" not in analytic and "CMP-" not in analytic
    with pytest.raises(ValueError, match="1000-ticket"):
        render_export("analytics", [row] * 1001)


async def test_export_worker_uses_only_guarded_functions_and_private_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = uuid4()
    row = SimpleNamespace(
        complaint_id=uuid4(),
        complaint_created_at=datetime.now(UTC),
        ticket_no="CMP-2026-000002",
        created_at=datetime.now(UTC),
        company_id=uuid4(),
        insurance_type="health",
        status="new",
        subject="Safe subject",
        policy_number="POL-012345",
        sla_breached=False,
        resolved_at=None,
        csat_score=None,
    )
    calls: list[str] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append(sql)
        if "claim_exports" in sql:
            return [SimpleNamespace(job_id=job, attempt=1, kind="complaints")]
        if "export_rows" in sql:
            return [row]
        if "finish_export" in sql:
            return [SimpleNamespace(finished=True)]
        return []

    monkeypatch.setattr(worker, "_worker_query", query)
    storage = SimpleNamespace(put=AsyncMock(), delete=AsyncMock())
    result = await worker.export_pending({"engine": object(), "storage": storage})
    assert result == "processed 1 export job(s)"
    assert all("app_security." in call for call in calls)
    key, data, mime = storage.put.call_args.args
    assert key == f"exports/{job}/1.csv" and isinstance(data, BytesIO)
    assert mime.startswith("text/csv") and b"CMP-2026-000002" in data.getvalue()
    storage.delete.assert_not_awaited()
    assert await worker.export_pending({"engine": object(), "storage": None}) == (
        "export storage unavailable; jobs remain queued"
    )


async def test_cleanup_only_clears_key_after_successful_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = uuid4()
    calls: list[str] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append(sql)
        if "claim_export_cleanup" in sql:
            return [SimpleNamespace(job_id=job, storage_key="exports/example/1.csv")]
        return []

    monkeypatch.setattr(worker, "_worker_query", query)
    storage = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("offline")))
    await worker.clean_expired_exports({"engine": object(), "storage": storage})
    assert len(calls) == 1
    storage.delete.side_effect = None
    await worker.clean_expired_exports({"engine": object(), "storage": storage})
    assert any("finish_export_cleanup" in sql for sql in calls)
