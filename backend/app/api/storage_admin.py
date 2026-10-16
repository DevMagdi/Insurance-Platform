"""First-party, read-only object-storage inventory for authorized administrators.

This UI is deliberately served by the insurance platform itself. It speaks only
through the configured S3-compatible SDK and never exposes storage credentials or
raw object contents. Business files remain downloadable only through their
scoped, audited application routes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.deps import RequestContext, require_permission
from app.domain.enums import OwnershipLevel
from app.models.tickets import AuditLog

router = APIRouter(prefix="/api/staff/storage", tags=["storage"])
logger = logging.getLogger(__name__)


def require_global_storage_scope(ctx: RequestContext) -> None:
    """Storage inventory is cross-company metadata and therefore needs global scope."""
    if (
        not ctx.scope.all_companies
        or ctx.scope.company_ids
        or ctx.scope.insurance_types
        or ctx.scope.ownership != OwnershipLevel.ALL
    ):
        raise HTTPException(status_code=403, detail="Global storage administration required")


@router.get("")
async def browse_storage(
    request: Request,
    response: Response,
    prefix: str = Query(default="", max_length=1024),
    continuation_token: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(require_permission("storage.manage")),
) -> dict[str, Any]:
    """Return one bounded page of bucket/folder/object metadata, never file bytes."""
    require_global_storage_scope(ctx)
    if "\x00" in prefix or len(prefix.encode("utf-8")) > 1024:
        raise HTTPException(status_code=422, detail="Invalid storage prefix")

    storage = getattr(request.app.state, "storage", None)
    settings = getattr(request.app.state, "settings", None)
    bucket = getattr(settings, "s3_bucket", None)
    if storage is None or not isinstance(bucket, str) or not bucket:
        raise HTTPException(status_code=503, detail="Private object storage is not configured")

    try:
        listing = await storage.list_objects(prefix, continuation_token, limit)
    except Exception:
        # Do not return SDK/provider messages: they can contain deployment details.
        logger.exception("Storage inventory request failed")
        raise HTTPException(
            status_code=503, detail="Object storage is temporarily unavailable"
        ) from None

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    ctx.db.add(
        AuditLog(
            actor_staff_id=ctx.actor.id,
            action="storage_inventory_viewed",
            resource_type="storage_bucket",
            after_state={
                "bucket": bucket,
                "prefix": prefix,
                "limit": limit,
                "folders_returned": len(listing.folders),
                "objects_returned": len(listing.objects),
            },
        )
    )
    await ctx.db.flush()

    return {
        "bucket": bucket,
        "provider": "S3-compatible",
        "status": "online",
        "prefix": prefix,
        "folders": list(listing.folders),
        "objects": [
            {
                "key": item.key,
                "size_bytes": item.size_bytes,
                "last_modified": item.last_modified,
                "etag": item.etag,
            }
            for item in listing.objects
        ],
        "is_truncated": listing.is_truncated,
        "next_page_token": listing.next_token,
    }
