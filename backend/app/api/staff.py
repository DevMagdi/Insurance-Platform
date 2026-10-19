"""Staff resources: every endpoint declares an explicit resource.action permission."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.domain.enums import (
    CommunicationChannel,
    ComplaintPriority,
    ComplaintStatus,
    InsuranceType,
    MessageVisibility,
)
from app.domain.schemas import (
    AccessSpec,
    ComplaintFilter,
    DepartmentAssignment,
    DepartmentSpec,
    ReplyRequest,
    RevealReason,
    RoleClone,
    RoleSpec,
    StaffInviteRequest,
    StaffProfileEdit,
    StatusChangeRequest,
    TotpPolicyRequest,
)
from app.repositories.access import AccessRepository
from app.repositories.complaints import ComplaintRepository
from app.services.realtime import stream_staff_complaint
from app.repositories.staff_profiles import StaffProfileRepository
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api/staff", tags=["staff"])


def access_error(exc: DBAPIError) -> HTTPException:
    sqlstate = getattr(exc.orig, "sqlstate", "")
    if sqlstate == "23514":
        return HTTPException(status_code=409, detail="Cannot remove the last Super Admin")
    if sqlstate == "22023":
        return HTTPException(status_code=422, detail="Invalid access specification")
    if sqlstate == "23505":
        return HTTPException(status_code=409, detail="Name or email is already in use")
    return HTTPException(status_code=403, detail="Access change forbidden")


@router.get("/users")
async def users(
    ctx: RequestContext = Depends(require_permission("users.view")),
) -> list[dict[str, Any]]:
    return await AccessRepository().staff_directory(ctx.db)


@router.post("/users/invitations", status_code=202)
async def invite_user(
    item: StaffInviteRequest,
    request: Request,
    ctx: RequestContext = Depends(require_permission("users.create")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    try:
        staff_id = await request.app.state.auth.create_invitation(
            ctx.db, item.email, item.full_name, item.department_id
        )
    except DBAPIError as exc:
        raise access_error(exc) from exc
    return {"id": staff_id, "message": "Invitation accepted by the configured email provider"}


@router.get("/users/{user_id}/access")
async def user_access(
    user_id: UUID, ctx: RequestContext = Depends(require_permission("users.view"))
) -> dict[str, Any]:
    result = await AccessRepository().snapshot(ctx.db, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Staff user unavailable")
    return result


@router.put("/users/{user_id}/access")
async def replace_access(
    user_id: UUID,
    item: AccessSpec,
    request: Request,
    ctx: RequestContext = Depends(require_permission("users.manage_access")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot change your own access")
    try:
        version = await AccessRepository().replace(ctx.db, user_id, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    # A versioned cache key cannot authorize another version even under races.
    await request.app.state.redis.delete(f"access:{user_id}:{version - 1}")
    return {"id": user_id, "token_version": version}


@router.post("/users/{user_id}/deactivate")
async def deactivate(
    user_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("users.deactivate")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot deactivate yourself")
    try:
        changed = await AccessRepository().deactivate(ctx.db, user_id)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="Staff user unavailable")
    # Disabled users fail DB status/session checks regardless of cache state.
    return {"deactivated": True}


@router.patch("/users/{user_id}/2fa-policy")
async def staff_totp_policy(
    user_id: UUID,
    item: TotpPolicyRequest,
    request: Request,
    ctx: RequestContext = Depends(require_permission("users.manage_access")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot edit your own 2FA policy")
    try:
        version = await AccessRepository().require_totp(ctx.db, user_id, item.required)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if version is None:
        raise HTTPException(status_code=404, detail="Staff user unavailable")
    await request.app.state.redis.delete(f"access:{user_id}:{version - 1}")
    return {"id": user_id, "two_factor_required": item.required}


@router.patch("/settings/staff-2fa")
async def global_totp_policy(
    item: TotpPolicyRequest,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    try:
        await AccessRepository().require_global_totp(ctx.db, item.required)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    return {"staff_2fa_required": item.required}


@router.get("/users/{user_id}/access/explain")
async def explain_user_access(
    user_id: UUID, ctx: RequestContext = Depends(require_permission("users.view"))
) -> dict[str, Any]:
    result = await AccessRepository().explain(ctx.db, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Staff user unavailable")
    return result


@router.patch("/users/{user_id}")
async def edit_user(
    user_id: UUID,
    item: StaffProfileEdit,
    ctx: RequestContext = Depends(require_permission("users.edit")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot edit your own admin profile")
    try:
        changed = await AccessRepository().edit_profile(ctx.db, user_id, item.email, item.full_name)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="Staff user unavailable")
    return {"id": user_id, "updated": True}


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: UUID,
    ctx: RequestContext = Depends(require_permission("users.edit")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot reactivate yourself")
    changed = await AccessRepository().reactivate(ctx.db, user_id)
    if not changed:
        raise HTTPException(status_code=404, detail="Disabled staff user unavailable")
    return {"reactivated": True}


@router.post("/users/{user_id}/force-logout")
async def force_logout_user(
    user_id: UUID,
    ctx: RequestContext = Depends(require_permission("users.manage_access")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot force-logout yourself")
    changed = await AccessRepository().force_logout(ctx.db, user_id)
    if not changed:
        raise HTTPException(status_code=404, detail="Staff user unavailable")
    return {"sessions_revoked": True}


@router.post("/users/{user_id}/password-reset", status_code=202)
async def reset_staff_password(
    user_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("users.edit")),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    if user_id == ctx.actor.id:
        raise HTTPException(status_code=403, detail="Cannot reset your own admin password")
    await request.app.state.auth.limits.check("admin:reset", str(ctx.actor.id), 10, 3600)
    try:
        await request.app.state.auth.reset_staff_password(ctx.db, user_id)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    return {"message": "One-use password reset link sent"}


@router.get("/roles")
async def roles_catalog(
    ctx: RequestContext = Depends(require_permission("roles.manage")),
) -> list[dict[str, Any]]:
    result = await AccessRepository().roles(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global role management required")
    return result


@router.post("/roles", status_code=201)
async def create_role(
    item: RoleSpec,
    ctx: RequestContext = Depends(require_permission("roles.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        role_id = await AccessRepository().save_role(
            ctx.db, None, item.name, item.description, item.permissions
        )
    except DBAPIError as exc:
        raise access_error(exc) from exc
    return {"id": role_id}


@router.put("/roles/{role_id}")
async def update_role(
    role_id: UUID,
    item: RoleSpec,
    ctx: RequestContext = Depends(require_permission("roles.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        changed = await AccessRepository().save_role(
            ctx.db, role_id, item.name, item.description, item.permissions
        )
    except DBAPIError as exc:
        raise access_error(exc) from exc
    return {"id": changed}


@router.post("/roles/{role_id}/clone", status_code=201)
async def clone_role(
    role_id: UUID,
    item: RoleClone,
    ctx: RequestContext = Depends(require_permission("roles.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        changed = await AccessRepository().clone_role(ctx.db, role_id, item.name)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if changed is None:
        raise HTTPException(status_code=404, detail="Source role unavailable")
    return {"id": changed}


@router.get("/complaints/filter-options")
async def filter_options(
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    return {
        "companies": await ComplaintRepository().filter_options(ctx.db, ctx.scope),
        "agents": await ComplaintRepository().agent_options(ctx.db),
        "statuses": [status.value for status in ComplaintStatus],
        "priorities": [priority.value for priority in ComplaintPriority],
        "channels": [channel.value for channel in CommunicationChannel],
        "insurance_types": [
            kind.value
            for kind in InsuranceType
            if not ctx.scope.insurance_types or kind in ctx.scope.insurance_types
        ],
    }


@router.get("/complaints")
async def complaints(
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    return await ctx.complaints.list(ctx.db, ctx.scope, filters)


@router.get("/complaints/counters")
async def complaint_counters(
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, int]:
    return await ComplaintRepository().counters(ctx.db, ctx.scope, filters)


@router.get("/complaints/export")
async def export_complaints(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("complaints.export")),
) -> Response:
    await request.app.state.auth.limits.check("complaints:export", str(ctx.actor.id), 5, 300)
    data = await ctx.complaints.export(ctx.db, ctx.scope, filters)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="complaints.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/complaints/{complaint_id}")
async def complaint_detail(
    complaint_id: UUID, ctx: RequestContext = Depends(require_permission("complaints.view"))
) -> dict[str, Any]:
    return await ctx.complaints.detail(ctx.db, ctx.scope, complaint_id)


@router.get("/complaints/{complaint_id}/sla")
async def complaint_sla(
    complaint_id: UUID, ctx: RequestContext = Depends(require_permission("complaints.view"))
) -> dict[str, Any]:
    """Deadlines, first response and at-risk/breached state for one ticket."""
    state = await ComplaintRepository().sla_state(ctx.db, ctx.scope, complaint_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Complaint unavailable")
    return state


@router.get("/complaints/{complaint_id}/timeline")
async def complaint_timeline(
    complaint_id: UUID,
    page: int = 1,
    page_size: int = 50,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    if not 1 <= page <= 100 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="Invalid timeline page")
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    return await ComplaintRepository().timeline(ctx.db, ctx.scope, parent, page, page_size)


@router.post("/complaints/{complaint_id}/reply")
async def staff_reply(
    complaint_id: UUID,
    item: ReplyRequest,
    ctx: RequestContext = Depends(require_permission("complaints.reply_public")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await ctx.complaints.reply(ctx.db, ctx.scope, complaint_id, item.body)


@router.post("/complaints/{complaint_id}/notes")
async def internal_note(
    complaint_id: UUID,
    item: ReplyRequest,
    ctx: RequestContext = Depends(require_permission("complaints.add_internal_note")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await ctx.complaints.reply(
        ctx.db, ctx.scope, complaint_id, item.body, MessageVisibility.INTERNAL
    )


@router.patch("/complaints/{complaint_id}/status")
async def staff_status(
    complaint_id: UUID,
    item: StatusChangeRequest,
    ctx: RequestContext = Depends(require_permission("complaints.change_status")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await ctx.complaints.change_status(ctx.db, ctx.scope, complaint_id, item.status)


@router.post("/complaints/{complaint_id}/national-id:reveal")
async def reveal_national_id(
    complaint_id: UUID,
    item: RevealReason,
    response: Response,
    ctx: RequestContext = Depends(require_permission("complaints.view_full_national_id")),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    value = await ctx.complaints.reveal_national_id(ctx.db, ctx.scope, complaint_id, item.reason)
    response.headers["Cache-Control"] = "no-store"
    return {"national_id": value}


@router.get("/complaints/{complaint_id}/stream")
async def staff_complaint_stream(complaint_id: UUID, request: Request) -> StreamingResponse:
    """Live SSE for the open staff ticket; auth/scope rechecked every poll."""
    auth = request.app.state.auth
    claims = auth.tokens.parse(request.cookies.get("access_token"), "access")
    if claims.subject.scope != "staff":
        raise HTTPException(status_code=403, detail="Staff account required")
    generator = stream_staff_complaint(
        factory=request.app.state.factory,
        redis=request.app.state.redis,
        complaints=request.app.state.complaints,
        claims=claims,
        complaint_id=complaint_id,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/complaints/{complaint_id}/attachments")
async def complaint_attachments(
    complaint_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.view_attachments")),
) -> list[dict[str, Any]]:
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    records = await ComplaintRepository().attachments(ctx.db, ctx.scope, parent)
    return [
        {
            "id": item.id,
            "filename": item.original_filename,
            "mime_type": item.mime_type,
            "size_bytes": item.size_bytes,
            "scan_status": item.scan_status,
        }
        for item in records
    ]


@router.get("/notifications")
async def staff_notifications(
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> list[dict[str, Any]]:
    records = await ComplaintRepository().notifications(ctx.db, ctx.scope)
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "payload": item.payload,
            "created_at": item.created_at,
            "read_at": item.read_at,
        }
        for item in records
    ]


@router.get("/departments")
async def departments(
    ctx: RequestContext = Depends(require_permission("users.view")),
) -> list[dict[str, Any]]:
    result = await StaffProfileRepository().departments(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global staff administration required")
    return result


def department_error(exc: DBAPIError) -> HTTPException:
    if getattr(exc.orig, "sqlstate", "") == "23505":
        return HTTPException(status_code=409, detail="Department name already exists")
    return access_error(exc)


@router.post("/departments", status_code=201)
async def create_department(
    item: DepartmentSpec,
    ctx: RequestContext = Depends(require_permission("users.edit")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    try:
        department_id = await StaffProfileRepository().create_department(
            ctx.db, item.name, item.description, item.is_active
        )
    except DBAPIError as exc:
        raise department_error(exc) from exc
    if department_id is None:
        raise HTTPException(status_code=403, detail="Global staff administration required")
    return {"id": department_id, **item.model_dump(), "employee_count": 0}


@router.put("/departments/{department_id}")
async def update_department(
    department_id: UUID,
    item: DepartmentSpec,
    ctx: RequestContext = Depends(require_permission("users.edit")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    try:
        updated = await StaffProfileRepository().update_department(
            ctx.db, department_id, item.name, item.description, item.is_active
        )
    except DBAPIError as exc:
        raise department_error(exc) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Department unavailable")
    return {"id": department_id, **item.model_dump()}


@router.patch("/users/{user_id}/department")
async def assign_department(
    user_id: UUID,
    item: DepartmentAssignment,
    ctx: RequestContext = Depends(require_permission("users.edit")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    changed = await StaffProfileRepository().assign_department(ctx.db, user_id, item.department_id)
    if not changed:
        raise HTTPException(status_code=404, detail="Staff user or department unavailable")
    return {"id": user_id, "department_id": item.department_id}


@router.get("/users/{user_id}/profile")
async def staff_profile(
    user_id: UUID, request: Request, ctx: RequestContext = Depends(require_permission("users.view"))
) -> dict[str, Any]:
    result: dict[str, Any] = await request.app.state.auth.staff_profiles.profile(ctx.db, user_id)
    return result


@router.get("/users/{user_id}/avatar", response_class=Response)
async def staff_avatar(
    user_id: UUID, request: Request, ctx: RequestContext = Depends(require_permission("users.view"))
) -> Response:
    content = await request.app.state.auth.staff_profiles.avatar(ctx.db, user_id)
    return Response(
        content, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
    )


@router.post("/users/{user_id}/resend-invitation", status_code=202)
async def resend_invitation(
    user_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("users.create")),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    await request.app.state.auth.resend_invitation(ctx.db, user_id)
    return {"message": "Invitation accepted by the configured email provider"}
