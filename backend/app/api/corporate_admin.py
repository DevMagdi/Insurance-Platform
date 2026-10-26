"""Staff administration of corporate clients.

The counterpart to ``app/api/corporate.py``: that module is what a contracted
company sees, this one is how brokerage staff set it up — register the
company, record the contract it signed (value, payment frequency, renewal
date), issue its portal login, and attach its employees.

Separate from the portal router on purpose. These routes run under a *staff*
session and the ``corporate.manage`` capability, so the row-level policies
that confine a corporate reader to one client do not apply here; the
authorization is the permission gate plus the audit trail each write leaves
behind.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, authenticated_context, require_permission
from app.domain.schemas import (
    CorporateClientUpsert,
    CorporateContractUpsert,
    CorporateMemberLink,
    CorporateUserCreate,
)
from app.models.crm_module import CrmLead
from app.models.tickets import AuditLog
from app.repositories.crm_module import CrmModuleRepository
from app.security.passwords import StaffPasswords
from app.security.tokens import csrf_required
from app.services.corporate import CorporateService

router = APIRouter(prefix="/api/staff/corporate", tags=["corporate-admin"])
logger = logging.getLogger(__name__)

# The gate yields the request context itself, so each handler takes it as
# its dependency rather than listing a parameterless one.
VIEW = Depends(require_permission("corporate.view"))
MANAGE = Depends(require_permission("corporate.manage"))
crm_repo = CrmModuleRepository()


def _service() -> CorporateService:
    return CorporateService()


async def _sales_directory_context(
    ctx: RequestContext = Depends(authenticated_context),
) -> RequestContext:
    if ctx.actor.scope != "staff" or not {"crm.view", "corporate.view"}.intersection(
        ctx.permissions
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    return ctx


SALES_DIRECTORY = Depends(_sales_directory_context)


async def _audit(
    ctx: RequestContext, action: str, resource_id: UUID, after: dict[str, Any]
) -> None:
    """Record who changed a corporate record, and to what.

    A nested transaction: a failed statement aborts the surrounding one, and
    an audit row that cannot be written must not take the request down with
    it.
    """
    async with ctx.db.begin_nested():
        ctx.db.add(
            AuditLog(
                actor_staff_id=ctx.actor.id,
                action=action,
                resource_type="corporate_client",
                resource_id=resource_id,
                after_state=after,
            )
        )
    await ctx.db.flush()


def _conflict(exc: DBAPIError, detail: str) -> HTTPException:
    """Turn a constraint violation into an answer the operator can act on."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate == "23505":  # unique_violation
        return HTTPException(status_code=409, detail=detail)
    if sqlstate in {"23503", "23514", "22023", "22P02"}:
        message = getattr(getattr(orig, "diag", None), "message_primary", None)
        return HTTPException(status_code=422, detail=message or detail)
    raise exc


@router.get("")
async def list_corporate_clients(ctx: RequestContext = VIEW) -> dict[str, Any]:
    """Every corporate client with its contracts and portal users."""
    return await _service().directory(ctx.db)


def _is_salesperson(ctx: RequestContext) -> bool:
    return "crm.manage" in ctx.permissions and "crm.convert" not in ctx.permissions


def _sales_client(lead: CrmLead) -> dict[str, Any]:
    """Safe list projection built only from the seller's converted lead."""
    client_id = lead.converted_corporate_client_id
    name = lead.company_name or lead.full_name
    return {
        "id": str(client_id),
        "name_ar": name,
        "name_en": name,
        "slug": None,
        "contact_name": lead.full_name,
        "contact_mobile": lead.phone,
        "contact_email": lead.email,
        "converted_at": lead.converted_at.isoformat() if lead.converted_at else None,
        "lead_id": str(lead.id),
        "lead_no": lead.lead_no,
        "origin": "converted_lead",
    }


@router.get("/sales-directory")
async def sales_directory(ctx: RequestContext = SALES_DIRECTORY) -> dict[str, Any]:
    """Corporate-view users see the full directory; sellers see owned conversions.

    Sales-role staff (``crm.manage`` without ``crm.convert``) are always
    self-scoped; other staff with ``corporate.view`` receive the full
    directory. CRM viewers otherwise see only conversions whose source lead
    is currently assigned to them. No second ownership field is inferred.
    """
    if "corporate.view" in ctx.permissions and not _is_salesperson(ctx):
        return await _service().directory(ctx.db)

    leads = await crm_repo.converted_leads(ctx.db, assigned_to=ctx.actor.id)
    unique: dict[UUID, dict[str, Any]] = {}
    for lead in leads:
        if lead.converted_corporate_client_id is not None:
            unique.setdefault(lead.converted_corporate_client_id, _sales_client(lead))
    items = list(unique.values())
    return {"items": items, "total": len(items), "scope": "assigned-converted-leads"}


@router.put("/clients")
async def upsert_corporate_client(
    item: CorporateClientUpsert,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Register a company, or amend one already registered."""
    try:
        result = await _service().save_client(ctx.db, item)
    except DBAPIError as exc:
        raise _conflict(exc, "A different company already uses that slug") from exc
    await _audit(
        ctx,
        "corporate_client_saved" if item.id is None else "corporate_client_updated",
        result["id"],
        {"slug": item.slug, "name_ar": item.name_ar, "is_active": item.is_active},
    )
    return result


@router.put("/contracts")
async def upsert_corporate_contract(
    item: CorporateContractUpsert,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Record or amend the contract a company signed with the brokerage."""
    if item.renews_on < item.starts_on:
        raise HTTPException(status_code=422, detail="Renewal date precedes the start date")
    try:
        result = await _service().save_contract(ctx.db, item)
    except DBAPIError as exc:
        raise _conflict(exc, "That contract number already exists for this company") from exc
    await _audit(
        ctx,
        "corporate_contract_saved",
        item.corporate_client_id,
        {
            "contract_no": item.contract_no,
            "contract_value": str(item.contract_value),
            "currency": item.currency,
            "payment_frequency": item.payment_frequency.value,
            "renews_on": item.renews_on.isoformat(),
            "status": item.status.value,
        },
    )
    return result


@router.post("/users", status_code=201)
async def create_corporate_user(
    item: CorporateUserCreate,
    request: Request,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Issue the username and password a company signs in with."""
    try:
        StaffPasswords.validate(item.password)
    except ValueError as exc:
        # The strength rule raises; an operator typing a weak password is a
        # bad request, not a server fault.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    passwords: StaffPasswords = request.app.state.auth.passwords
    try:
        result = await _service().create_user(ctx.db, item, passwords)
    except DBAPIError as exc:
        raise _conflict(exc, "That username is already taken") from exc
    # The password itself is never written to the audit trail.
    await _audit(
        ctx,
        "corporate_user_created",
        item.corporate_client_id,
        {"username": item.username, "full_name": item.full_name},
    )
    return result


@router.post("/members")
async def link_corporate_member(
    item: CorporateMemberLink,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Attach an existing customer to a company as one of its employees."""
    result = await _service().link_member(
        ctx.db, item.customer_id, item.corporate_client_id, item.employee_code
    )
    await _audit(
        ctx,
        "corporate_member_linked",
        item.corporate_client_id,
        {"customer_id": str(item.customer_id), "employee_code": item.employee_code},
    )
    return result
