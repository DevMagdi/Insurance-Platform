"""Customer/staff authentication and self-service session routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import (
    RequestContext,
    authenticated_context,
    client_ip,
    require_staff,
    user_agent,
)
from app.domain.schemas import (
    ChallengeRequest,
    ChangePasswordRequest,
    CustomerVerifyRequest,
    InvitationTokenRequest,
    InviteAcceptRequest,
    MobileOTPRequest,
    StaffLoginRequest,
    TotpConfirmRequest,
    TotpVerifyRequest,
)
from app.models.identity import Customer
from app.repositories.auth import AuthRepository
from app.repositories.complaints import ComplaintRepository
from app.repositories.workflow import StaffWorkflowRepository
from app.security.captcha import captcha_guard
from app.security.tokens import clear_auth_cookies, csrf_required, set_auth_cookies

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/auth/csrf", include_in_schema=False)
async def migrate_csrf_cookie(request: Request, response: Response) -> dict[str, bool]:
    """Expose a legacy API-scoped CSRF cookie to the SPA without invalidating sessions."""
    legacy_token = request.cookies.get("csrf_token")
    if legacy_token:
        settings = request.app.state.auth.settings
        response.set_cookie(
            "csrf_token",
            legacy_token,
            max_age=settings.refresh_token_days * 86400,
            secure=settings.cookie_secure,
            httponly=False,
            samesite="lax",
            path="/",
        )
        response.delete_cookie("csrf_token", path="/api", secure=settings.cookie_secure)
    response.headers["Cache-Control"] = "no-store"
    return {"available": bool(legacy_token)}


@router.post(
    "/auth/customer/otp/start",
    status_code=202,
    dependencies=[Depends(captcha_guard("otp_start"))],
)
async def start_otp(item: MobileOTPRequest, request: Request) -> dict[str, str]:
    await request.app.state.auth.start_customer_otp(item.mobile, client_ip(request))
    return {"message": "If delivery is available, a one-time code has been sent"}


@router.post("/auth/customer/otp/verify")
async def verify_otp(
    item: CustomerVerifyRequest, request: Request, response: Response
) -> dict[str, str]:
    auth = request.app.state.auth
    tokens = await auth.verify_customer(item, client_ip(request), user_agent(request))
    set_auth_cookies(response, tokens.access, tokens.refresh, auth.settings)
    return {"authenticated_as": "customer"}


@router.post("/auth/staff/login", dependencies=[Depends(captcha_guard("staff_login"))])
async def staff_login(
    item: StaffLoginRequest, request: Request, response: Response
) -> dict[str, str | None]:
    auth = request.app.state.auth
    result = await auth.staff_login(
        item.email, item.password, client_ip(request), user_agent(request)
    )
    if result.tokens is not None:
        set_auth_cookies(response, result.tokens.access, result.tokens.refresh, auth.settings)
    else:
        # A pending MFA/password challenge must not inherit any previous identity.
        clear_auth_cookies(response, auth.settings)
    return {"next_step": result.next_step or "authenticated", "challenge": result.challenge}


@router.post("/auth/staff/invitation/inspect")
async def inspect_invitation(item: InvitationTokenRequest, request: Request) -> dict[str, Any]:
    auth = request.app.state.auth
    await auth.limits.check("invite:inspect", client_ip(request), 30, 3600)
    result: dict[str, Any] = await auth.inspect_invitation(item.token)
    return result


@router.post(
    "/auth/staff/invitation/accept", dependencies=[Depends(captcha_guard("invite_accept"))]
)
async def accept_invitation(item: InviteAcceptRequest, request: Request) -> dict[str, str]:
    auth = request.app.state.auth
    await auth.limits.check("invite:accept", client_ip(request), 10, 3600)
    try:
        await auth.accept_invitation(item.token, item.password, item.profile, item.avatar_base64)
    except ValueError:
        # Validation failures are reported with a stable client-safe message;
        # never reflect implementation or provider exception text to callers.
        raise HTTPException(status_code=422, detail="Invitation or password is invalid") from None
    return {"next_step": "sign_in_with_password"}


@router.post("/auth/staff/password/change")
async def change_password(item: ChangePasswordRequest, request: Request) -> dict[str, str]:
    try:
        await request.app.state.auth.change_forced_password(item.challenge, item.password)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Password does not meet security requirements"
        ) from None
    return {"next_step": "sign_in_with_new_password"}


@router.post("/auth/staff/2fa/verify")
async def verify_totp(
    item: TotpVerifyRequest, request: Request, response: Response
) -> dict[str, str]:
    auth = request.app.state.auth
    tokens = await auth.verify_staff_totp(
        item.challenge, item.code, client_ip(request), user_agent(request)
    )
    set_auth_cookies(response, tokens.access, tokens.refresh, auth.settings)
    return {"authenticated_as": "staff"}


@router.post("/auth/staff/2fa/enroll/start")
async def setup_required_totp(item: ChallengeRequest, request: Request) -> dict[str, str]:
    auth = request.app.state.auth
    claims = auth.tokens.parse(item.challenge, "challenge")
    uri = await auth.enroll_start(
        claims.subject, claims.session_id, claims.token_version, item.challenge
    )
    return {"provisioning_uri": uri}


@router.post("/auth/staff/2fa/enroll/confirm")
async def confirm_required_totp(item: TotpVerifyRequest, request: Request) -> dict[str, str]:
    auth = request.app.state.auth
    claims = auth.tokens.parse(item.challenge, "challenge")
    await auth.enroll_confirm(
        claims.subject, claims.session_id, claims.token_version, item.code, item.challenge
    )
    return {"next_step": "sign_in_with_2fa"}


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response) -> Any:
    csrf_required(request)
    auth = request.app.state.auth
    tokens = await auth.refresh(
        request.cookies.get("refresh_token"), client_ip(request), user_agent(request)
    )
    if tokens is None:
        invalid = JSONResponse({"detail": "Session expired; sign in again"}, status_code=401)
        clear_auth_cookies(invalid, auth.settings)
        return invalid
    set_auth_cookies(response, tokens.access, tokens.refresh, auth.settings)
    return {"refreshed": True}


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(authenticated_context),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    session = await AuthRepository().get_session(ctx.db, ctx.session_id, lock=True)
    if session is not None:
        from datetime import UTC, datetime

        session.revoked_at = datetime.now(UTC)
    clear_auth_cookies(response, request.app.state.auth.settings)
    return {"logged_out": True}


@router.get("/me")
async def me(ctx: RequestContext = Depends(authenticated_context)) -> dict[str, Any]:
    if ctx.actor.scope == "staff":
        return {"kind": "staff", "access": ctx.snapshot}
    row = (
        await ctx.db.execute(
            select(
                Customer.id,
                Customer.full_name,
                Customer.mobile,
                Customer.national_id_last4,
                Customer.email,
            ).where(Customer.id == ctx.actor.id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Customer unavailable")
    return {
        "kind": "customer",
        "id": row.id,
        "full_name": row.full_name,
        "mobile": row.mobile,
        "email": row.email,
        "national_id_masked": f"**********{row.national_id_last4}"
        if row.national_id_last4
        else "**************",
    }


@router.get("/me/notifications")
async def my_notifications(
    ctx: RequestContext = Depends(require_staff),
) -> list[dict[str, Any]]:
    """Includes access-change notices even when complaint permission was revoked."""
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


@router.patch("/me/notifications/{notification_id}/read")
async def read_my_notification(
    notification_id: UUID,
    ctx: RequestContext = Depends(require_staff),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if not await StaffWorkflowRepository().mark_read(ctx.db, ctx.scope, notification_id):
        raise HTTPException(status_code=404, detail="Notification unavailable")
    return {"read": True}


@router.get("/me/sessions")
async def sessions(ctx: RequestContext = Depends(authenticated_context)) -> list[dict[str, Any]]:
    records = await AuthRepository().list_sessions(ctx.db, ctx.actor)
    return [
        {
            "id": row.id,
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "expires_at": row.expires_at,
            "ip_address": row.ip_address,
            "user_agent": row.user_agent,
            "current": row.id == ctx.session_id,
        }
        for row in records
    ]


@router.delete("/me/sessions/{session_id}")
async def revoke_session(
    session_id: UUID,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(authenticated_context),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    from datetime import UTC, datetime

    record = await AuthRepository().get_session(ctx.db, session_id, lock=True)
    # RLS already limits sessions to their owner; the explicit check is defence in depth.
    if (
        record is None
        or record.subject_id != ctx.actor.id
        or record.subject_kind != ctx.actor.scope
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    record.revoked_at = datetime.now(UTC)
    if session_id == ctx.session_id:
        clear_auth_cookies(response, request.app.state.auth.settings)
    return {"revoked": True}


@router.post("/me/sessions/revoke-all")
async def revoke_all(
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(authenticated_context),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    await AuthRepository().revoke_all(ctx.db, ctx.actor)
    clear_auth_cookies(response, request.app.state.auth.settings)
    return {"revoked": True}


@router.post("/me/2fa/enroll/start")
async def voluntary_totp_start(
    request: Request, ctx: RequestContext = Depends(require_staff), _: None = Depends(csrf_required)
) -> dict[str, str]:
    uri = await request.app.state.auth.enroll_start(ctx.actor, ctx.session_id, ctx.token_version)
    return {"provisioning_uri": uri}


@router.post("/me/2fa/enroll/confirm")
async def voluntary_totp_confirm(
    item: TotpConfirmRequest,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_staff),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    await request.app.state.auth.enroll_confirm(
        ctx.actor, ctx.session_id, ctx.token_version, item.code
    )
    clear_auth_cookies(response, request.app.state.auth.settings)
    return {"next_step": "sign_in_with_2fa"}
