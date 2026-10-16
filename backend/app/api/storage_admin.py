"""First-party, read-only storage inventory for authorized administrators.

The inventory describes what the platform itself stores in PostgreSQL: attachment
bytes, generated export artifacts and call recordings. It speaks only to
``app_security.storage_inventory``/``storage_totals`` (bounded metadata queries
behind the ``storage.manage`` capability and global scope) and never returns file
bytes, storage credentials or provider messages. Business files remain
downloadable only through their scoped, audited application routes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.domain.enums import OwnershipLevel
from app.models.tickets import AuditLog
from app.repositories.storage import (
    FOLDERS,
    KINDS,
    StorageInventoryRepository,
    decode_cursor,
)

router = APIRouter(prefix="/api/staff/storage", tags=["storage"])
logger = logging.getLogger(__name__)

STORE_NAME = "insurance-platform"
FORBIDDEN_DETAIL = "Global storage administration required"


def require_global_storage_scope(ctx: RequestContext) -> None:
    """Storage inventory is cross-company metadata and therefore needs global scope."""
    if (
        not ctx.scope.all_companies
        or ctx.scope.company_ids
        or ctx.scope.insurance_types
        or ctx.scope.ownership != OwnershipLevel.ALL
    ):
        raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)


def _kind_for_prefix(prefix: str) -> str | None:
    """Map a logical folder prefix (``attachments/``) to one inventory kind."""
    if not prefix:
        return None
    folder = prefix.split("/", 1)[0]
    for kind in KINDS:
        if f"{kind}s" == folder:
            return kind
    raise HTTPException(status_code=422, detail="Unknown storage prefix")


@router.get("")
async def browse_storage(
    request: Request,
    response: Response,
    prefix: str = Query(default="", max_length=1024),
    continuation_token: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: RequestContext = Depends(require_permission("storage.manage")),
) -> dict[str, Any]:
    """Return one bounded page of row metadata, never file bytes."""
    require_global_storage_scope(ctx)
    if "\x00" in prefix or len(prefix.encode("utf-8")) > 1024:
        raise HTTPException(status_code=422, detail="Invalid storage prefix")
    kind = _kind_for_prefix(prefix)
    after = None
    if continuation_token:
        decoded = decode_cursor(continuation_token)
        if decoded is None or decoded[0] != kind:
            raise HTTPException(status_code=422, detail="Invalid storage cursor")
        after = decoded[1]

    repository = StorageInventoryRepository()
    try:
        totals = await repository.totals(ctx.db)
        page = await repository.page(ctx.db, kind, after, limit) if kind is not None else None
    except DBAPIError as exc:
        # Never surface driver/SQL detail: it can describe deployment internals.
        logger.warning("Storage inventory query failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Storage inventory is temporarily unavailable"
        ) from None

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    objects = (
        [
            {
                "key": row.object_key,
                "kind": row.kind,
                "size_bytes": row.size_bytes,
                "last_modified": row.last_modified,
                "checksum_sha256": row.checksum_sha256,
                "details": row.details,
            }
            for row in page.rows
        ]
        if page is not None
        else []
    )
    ctx.db.add(
        AuditLog(
            actor_staff_id=ctx.actor.id,
            action="storage_inventory_viewed",
            resource_type="storage_store",
            after_state={
                "store": STORE_NAME,
                "prefix": prefix,
                "limit": limit,
                "objects_returned": len(objects),
            },
        )
    )
    await ctx.db.flush()

    return {
        "backend": "postgresql",
        "store": STORE_NAME,
        "status": "online",
        "prefix": prefix,
        "folders": list(FOLDERS) if kind is None else [],
        "objects": objects,
        "totals": totals,
        "is_truncated": bool(page.is_truncated) if page else False,
        "next_page_token": page.next_page_token if page else None,
    }
