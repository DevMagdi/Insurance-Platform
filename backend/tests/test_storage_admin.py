"""Built-in storage inventory stays bounded, permission-gated and metadata-only."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, authenticated_context
from app.api.storage_admin import FORBIDDEN_DETAIL, STORE_NAME
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.main import app
from app.repositories.storage import encode_cursor

OBJECT_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class FakeDb:
    """Minimal session double: only the inventory statements are expected."""

    def __init__(self, objects: list[SimpleNamespace], totals: list[SimpleNamespace]) -> None:
        self.objects = objects
        self.totals = totals
        self.statements: list[str] = []
        self.add = Mock()
        self.flush = AsyncMock()

    async def execute(self, statement: object, params: dict | None = None) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if "storage_totals" in sql:
            return FakeResult(self.totals)
        return FakeResult(self.objects)


def totals_rows() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(kind="attachment", objects=2, bytes=4096, attention=1, missing_content=0),
        SimpleNamespace(kind="export", objects=1, bytes=512, attention=0, missing_content=0),
        SimpleNamespace(kind="recording", objects=0, bytes=0, attention=0, missing_content=0),
    ]


def object_row(kind: str = "attachment") -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        object_id=OBJECT_ID,
        object_key=f"{kind}s/{OBJECT_ID}",
        size_bytes=2048,
        last_modified=datetime(2026, 10, 16, 12, tzinfo=UTC),
        checksum_sha256="a" * 64 if kind == "attachment" else None,
        details={"filename": "report.pdf", "encrypted": True},
    )


@pytest.fixture
async def storage_admin_client(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    db = FakeDb([object_row()], totals_rows())
    context = RequestContext(
        db,
        actor,
        uuid4(),
        1,
        scope,
        frozenset({"storage.manage"}),
        {"id": str(actor.id)},
        SimpleNamespace(),
    )
    # The inventory must work without any storage client on app.state.
    monkeypatch.delattr(app.state, "storage", raising=False)

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            yield client, context, db
    finally:
        app.dependency_overrides.clear()


async def test_root_lists_logical_folders_and_database_totals(
    storage_admin_client: tuple,
) -> None:
    client, _context, _db = storage_admin_client

    response = await client.get("/api/staff/storage")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["backend"] == "postgresql"
    assert body["store"] == STORE_NAME
    assert body["status"] == "online"
    assert body["folders"] == ["attachments/", "exports/", "recordings/"]
    assert body["objects"] == []
    assert body["is_truncated"] is False and body["next_page_token"] is None
    assert body["totals"] == {
        "objects": 3,
        "bytes": 4608,
        "pending_scans": 1,
        "attachment_metadata_without_content": 0,
        "by_kind": {
            "attachment": {"objects": 2, "bytes": 4096, "attention": 1, "missing_content": 0},
            "export": {"objects": 1, "bytes": 512, "attention": 0, "missing_content": 0},
            "recording": {"objects": 0, "bytes": 0, "attention": 0, "missing_content": 0},
        },
    }
    # No bucket, no provider-supplied object key and no file bytes leave the API.
    assert "bucket" not in body
    assert '"content"' not in response.text


async def test_folder_page_returns_metadata_only_and_audits_the_view(
    storage_admin_client: tuple,
) -> None:
    client, context, db = storage_admin_client

    response = await client.get(
        "/api/staff/storage", params={"prefix": "attachments/", "limit": 25}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["prefix"] == "attachments/"
    assert body["folders"] == []
    assert body["objects"] == [
        {
            "key": f"attachments/{OBJECT_ID}",
            "kind": "attachment",
            "size_bytes": 2048,
            "last_modified": "2026-10-16T12:00:00Z",
            "checksum_sha256": "a" * 64,
            "details": {"filename": "report.pdf", "encrypted": True},
        }
    ]
    inventory_call = next(sql for sql in db.statements if "storage_inventory" in sql)
    assert "storage_inventory" in inventory_call
    context.db.flush.assert_awaited_once()
    [event] = context.db.add.call_args.args
    assert event.action == "storage_inventory_viewed"
    assert event.resource_type == "storage_store"
    assert event.after_state == {
        "store": STORE_NAME,
        "prefix": "attachments/",
        "limit": 25,
        "objects_returned": 1,
    }


async def test_pagination_cursor_is_opaque_and_kind_bound(storage_admin_client: tuple) -> None:
    client, _context, db = storage_admin_client
    db.objects = [object_row(), object_row("export")]

    first = await client.get("/api/staff/storage", params={"prefix": "attachments/", "limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["is_truncated"] is True
    assert body["next_page_token"] == encode_cursor("attachment", OBJECT_ID)

    continued = await client.get(
        "/api/staff/storage",
        params={"prefix": "attachments/", "continuation_token": body["next_page_token"]},
    )
    assert continued.status_code == 200
    # A cursor minted for one folder cannot be replayed against another.
    mismatched = await client.get(
        "/api/staff/storage",
        params={"prefix": "exports/", "continuation_token": body["next_page_token"]},
    )
    assert mismatched.status_code == 422
    malformed = await client.get(
        "/api/staff/storage", params={"prefix": "exports/", "continuation_token": "not-a-cursor"}
    )
    assert malformed.status_code == 422
    unknown = await client.get("/api/staff/storage", params={"prefix": "buckets/"})
    assert unknown.status_code == 422


async def test_storage_api_rejects_missing_permission_and_non_global_scope(
    storage_admin_client: tuple,
) -> None:
    client, context, db = storage_admin_client

    context.permissions = frozenset()
    denied = await client.get("/api/staff/storage")
    assert denied.status_code == 403
    assert db.statements == []

    context.permissions = frozenset({"storage.manage"})
    context.scope = AccessScope(
        context.actor,
        company_ids=frozenset({uuid4()}),
        ownership=OwnershipLevel.OWN,
    )
    scoped = await client.get("/api/staff/storage")
    assert scoped.status_code == 403
    assert scoped.json()["detail"] == FORBIDDEN_DETAIL
    assert db.statements == []
    context.db.add.assert_not_called()


async def test_storage_api_bounds_requests_and_masks_database_errors(
    storage_admin_client: tuple,
) -> None:
    client, _context, db = storage_admin_client

    oversized = await client.get("/api/staff/storage", params={"limit": 201})
    assert oversized.status_code == 422
    assert db.statements == []

    db.execute = AsyncMock(side_effect=DBAPIError("SELECT 1", {}, Exception("internal SQL")))
    unavailable = await client.get("/api/staff/storage", params={"prefix": "exports/"})
    assert unavailable.status_code == 503
    assert "internal SQL" not in unavailable.text
