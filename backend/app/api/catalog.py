"""Operational catalog: explicit grants, company/type scopes and audited DB guards."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.api.staff import access_error
from app.domain.schemas import CategorySpec, CompanySpec, SlaRuleSpec, TemplateSpec
from app.repositories.catalog import CatalogKind, CatalogRepository
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api/staff/admin", tags=["administration"])


async def save(
    ctx: RequestContext,
    kind: CatalogKind,
    item_id: UUID | None,
    item: CompanySpec | CategorySpec | SlaRuleSpec | TemplateSpec,
) -> dict[str, UUID]:
    try:
        result = await CatalogRepository().save(ctx.db, kind, item_id, item.model_dump_json())
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Catalog item unavailable")
    return {"id": result}


@router.get("/companies")
async def companies(
    ctx: RequestContext = Depends(require_permission("companies.manage")),
) -> list[dict[str, Any]]:
    result = await CatalogRepository().companies(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global insurer management required")
    return result


@router.post("/companies", status_code=201)
async def create_company(
    item: CompanySpec,
    ctx: RequestContext = Depends(require_permission("companies.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "company", None, item)


@router.put("/companies/{item_id}")
async def update_company(
    item_id: UUID,
    item: CompanySpec,
    ctx: RequestContext = Depends(require_permission("companies.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "company", item_id, item)


@router.get("/categories")
async def categories(
    ctx: RequestContext = Depends(require_permission("categories.manage")),
) -> list[dict[str, Any]]:
    return await CatalogRepository().categories(ctx.db, ctx.scope)


@router.post("/categories", status_code=201)
async def create_category(
    item: CategorySpec,
    ctx: RequestContext = Depends(require_permission("categories.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "category", None, item)


@router.put("/categories/{item_id}")
async def update_category(
    item_id: UUID,
    item: CategorySpec,
    ctx: RequestContext = Depends(require_permission("categories.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "category", item_id, item)


@router.get("/sla-rules")
async def sla_rules(
    ctx: RequestContext = Depends(require_permission("sla.manage")),
) -> list[dict[str, Any]]:
    return await CatalogRepository().slas(ctx.db, ctx.scope)


@router.post("/sla-rules", status_code=201)
async def create_sla_rule(
    item: SlaRuleSpec,
    ctx: RequestContext = Depends(require_permission("sla.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "sla_rule", None, item)


@router.put("/sla-rules/{item_id}")
async def update_sla_rule(
    item_id: UUID,
    item: SlaRuleSpec,
    ctx: RequestContext = Depends(require_permission("sla.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "sla_rule", item_id, item)


@router.get("/templates")
async def templates(
    ctx: RequestContext = Depends(require_permission("templates.manage")),
) -> list[dict[str, Any]]:
    return await CatalogRepository().templates(ctx.db, ctx.scope)


@router.post("/templates", status_code=201)
async def create_template(
    item: TemplateSpec,
    ctx: RequestContext = Depends(require_permission("templates.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "template", None, item)


@router.put("/templates/{item_id}")
async def update_template(
    item_id: UUID,
    item: TemplateSpec,
    ctx: RequestContext = Depends(require_permission("templates.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    return await save(ctx, "template", item_id, item)
