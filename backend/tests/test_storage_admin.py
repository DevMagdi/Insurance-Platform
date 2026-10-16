"""Built-in storage inventory stays bounded, permission-gated and metadata-only."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.main import app
from app.services.storage import S3ObjectStorage, StorageObject, StorageObjectPage


class FakeS3Client:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.arguments: dict[str, object] | None = None

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.arguments = kwargs
        return self.response


async def test_s3_inventory_is_delimiter_aware_and_never_reads_object_bodies() -> None:
    modified = datetime(2026, 10, 16, 12, tzinfo=UTC)
    client = FakeS3Client(
        {
            "CommonPrefixes": [{"Prefix": "attachments/claims/"}],
            "Contents": [
                {"Key": "attachments/", "Size": 0},  # folder marker is not a file
                {
                    "Key": "attachments/claim.pdf",
                    "Size": 2048,
                    "LastModified": modified,
                    "ETag": '"etag"',
                },
                {"Key": "outside/hidden.txt", "Size": 12},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "next-token",
        }
    )
    storage = S3ObjectStorage.__new__(S3ObjectStorage)
    storage.bucket = "complaint-attachments"
    storage.client = client  # type: ignore[assignment]

    page = await storage.list_objects("attachments/", "previous-token", 25)

    assert page.folders == ("attachments/claims/",)
    assert page.objects == (StorageObject("attachments/claim.pdf", 2048, modified, '"etag"'),)
    assert page.is_truncated and page.next_token == "next-token"
    assert client.arguments == {
        "Bucket": "complaint-attachments",
        "Prefix": "attachments/",
        "Delimiter": "/",
        "MaxKeys": 25,
        "ContinuationToken": "previous-token",
    }


@pytest.fixture
async def storage_admin_client(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
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
    page = StorageObjectPage(
        folders=("attachments/",),
        objects=(StorageObject("reports/file.pdf", 512, None, '"abc"'),),
        is_truncated=True,
        next_token="cursor-2",
    )
    storage = SimpleNamespace(list_objects=AsyncMock(return_value=page))
    monkeypatch.setattr(app.state, "storage", storage, raising=False)
    monkeypatch.setattr(
        app.state, "settings", SimpleNamespace(s3_bucket="complaint-attachments"), raising=False
    )

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            yield client, context, storage, page
    finally:
        app.dependency_overrides.clear()


async def test_storage_api_returns_only_bounded_metadata_and_audits_the_inventory(
    storage_admin_client: tuple,
) -> None:
    client, context, storage, _ = storage_admin_client

    response = await client.get(
        "/api/staff/storage",
        params={"prefix": "reports/", "continuation_token": "opaque-cursor", "limit": 25},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "bucket": "complaint-attachments",
        "provider": "S3-compatible",
        "status": "online",
        "prefix": "reports/",
        "folders": ["attachments/"],
        "objects": [
            {"key": "reports/file.pdf", "size_bytes": 512, "last_modified": None, "etag": '"abc"'}
        ],
        "is_truncated": True,
        "next_page_token": "cursor-2",
    }
    storage.list_objects.assert_awaited_once_with("reports/", "opaque-cursor", 25)
    context.db.flush.assert_awaited_once()
    [event] = context.db.add.call_args.args
    assert event.action == "storage_inventory_viewed"
    assert event.resource_type == "storage_bucket"
    assert event.after_state == {
        "bucket": "complaint-attachments",
        "prefix": "reports/",
        "limit": 25,
        "folders_returned": 1,
        "objects_returned": 1,
    }
    assert "body" not in response.json() and "access_key" not in response.json()


async def test_storage_api_rejects_missing_permission_and_non_global_scope(
    storage_admin_client: tuple,
) -> None:
    client, context, storage, _ = storage_admin_client

    context.permissions = frozenset()
    denied = await client.get("/api/staff/storage")
    assert denied.status_code == 403
    storage.list_objects.assert_not_awaited()

    context.permissions = frozenset({"storage.manage"})
    context.scope = AccessScope(
        context.actor,
        company_ids=frozenset({uuid4()}),
        ownership=OwnershipLevel.OWN,
    )
    scoped = await client.get("/api/staff/storage")
    assert scoped.status_code == 403
    storage.list_objects.assert_not_awaited()
    context.db.add.assert_not_called()


async def test_storage_api_bounds_listing_requests_and_masks_provider_errors(
    storage_admin_client: tuple,
) -> None:
    client, _, storage, _ = storage_admin_client

    oversized = await client.get("/api/staff/storage", params={"limit": 501})
    assert oversized.status_code == 422
    storage.list_objects.assert_not_awaited()

    storage.list_objects.side_effect = RuntimeError(
        "provider endpoint and credentials must not leak"
    )
    unavailable = await client.get("/api/staff/storage")
    assert unavailable.status_code == 503
    assert "provider endpoint" not in unavailable.text
