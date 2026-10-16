"""Public ticket API authenticated by revocable, company-scoped API keys."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response

from app.api.deps import client_ip
from app.db import purpose_transaction
from app.repositories.integrations import IntegrationRepository

router = APIRouter(prefix="/api/public/v1", tags=["public-api"])


@router.get("/complaints/{ticket_no}")
async def api_key_complaint(
    request: Request,
    response: Response,
    ticket_no: str = Path(pattern=r"^CMP-[0-9]{4}-[0-9]{6,}$"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("ApiKey "):
        raise HTTPException(status_code=401, detail="API key required")
    token = authorization[7:]
    try:
        prefix, secret = token.split(".", maxsplit=1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid API key") from exc
    if not re.fullmatch(r"ipk_[A-Za-z0-9_-]{12,24}", prefix) or not re.fullmatch(
        r"[A-Za-z0-9_-]{40,64}", secret
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")
    auth = getattr(request.app.state, "auth", None)
    limiter = getattr(auth, "limits", None)
    if limiter is not None:
        await limiter.check("public-api:key", prefix, 120, 60)
        await limiter.check("public-api:ip", client_ip(request), 600, 60)
    key_hash = hashlib.sha256(secret.encode("ascii")).digest()
    repository = IntegrationRepository()
    async with purpose_transaction(request.app.state.factory, "guest") as db:
        identity = await repository.api_key_identity(db, prefix, key_hash)
        if identity is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        if "complaints.view" not in identity["permission_codes"]:
            raise HTTPException(status_code=403, detail="API key lacks complaints.view")
        ticket = await repository.api_key_ticket(db, prefix, key_hash, ticket_no)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Complaint not found in this API key scope")
    response.headers["Cache-Control"] = "no-store"
    return {
        "id": ticket["id"],
        "ticket_no": ticket["ticket_no"],
        "company_id": ticket["company_id"],
        "insurance_type": ticket["insurance_type"],
        "status": ticket["status"],
        "priority": ticket["priority"],
        "subject": ticket["subject"],
        "created_at": ticket["created_at"],
        "updated_at": ticket["updated_at"],
    }
