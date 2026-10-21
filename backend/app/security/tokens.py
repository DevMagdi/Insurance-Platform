"""Audience-bound JWTs, one-way refresh digests, and CSRF-protected cookies."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from fastapi import HTTPException, Request, Response

from app.db import Actor, ActorScope
from app.settings import Settings

TokenType = Literal["access", "refresh", "challenge", "upload", "download"]
_ISSUER = "insurance-platform"
_AUDIENCE = "insurance-platform-api"


@dataclass(frozen=True)
class TokenClaims:
    subject: Actor
    session_id: UUID
    token_version: int
    token_type: TokenType
    expires_at: datetime
    jti: UUID
    purpose: str | None = None
    resource_id: UUID | None = None


class TokenManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keyring = settings.jwt_keyring()

    def issue(
        self,
        actor: Actor,
        session_id: UUID,
        token_version: int,
        token_type: TokenType,
        lifetime: timedelta,
        *,
        purpose: str | None = None,
        resource_id: UUID | None = None,
    ) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": str(actor.id),
            "sid": str(session_id),
            "kind": actor.scope,
            "tv": token_version,
            "typ": token_type,
            "jti": str(uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + lifetime,
        }
        if purpose is not None:
            claims["purpose"] = purpose
        if resource_id is not None:
            claims["rid"] = str(resource_id)
        return str(
            jwt.encode(
                claims,
                self.keyring.private_key,
                algorithm="EdDSA",
                headers={"kid": self.keyring.active_key_id},
            )
        )

    def parse(self, token: str | None, expected_type: TokenType) -> TokenClaims:
        if not token:
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if not isinstance(key_id, str):
                raise ValueError("Missing JWT key ID")
            verification_key = self.keyring.verification_keys.get(key_id)
            if verification_key is None:
                raise ValueError("Unknown JWT key ID")
            payload = jwt.decode(
                token,
                verification_key,
                algorithms=["EdDSA"],
                audience=_AUDIENCE,
                issuer=_ISSUER,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "sid",
                        "kind",
                        "tv",
                        "typ",
                        "jti",
                        "iat",
                        "nbf",
                        "exp",
                    ]
                },
            )
            if payload["typ"] != expected_type or payload["kind"] not in (
                "staff",
                "customer",
                "corporate",
            ):
                raise ValueError("Wrong token kind")
            if type(payload["tv"]) is not int or payload["tv"] < 0:
                raise ValueError("Invalid token version")
            scope: ActorScope = payload["kind"]
            return TokenClaims(
                subject=Actor(UUID(payload["sub"]), scope),
                session_id=UUID(payload["sid"]),
                token_version=payload["tv"],
                token_type=expected_type,
                expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
                jti=UUID(payload["jti"]),
                purpose=payload.get("purpose"),
                resource_id=UUID(payload["rid"]) if "rid" in payload else None,
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    @staticmethod
    def digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("ascii")).digest()


def csrf_required(request: Request) -> None:
    sent = request.headers.get("X-CSRF-Token", "")
    cookie = request.cookies.get("csrf_token", "")
    if not sent or not cookie or not hmac.compare_digest(sent, cookie):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def set_auth_cookies(response: Response, access: str, refresh: str, settings: Settings) -> None:
    response.set_cookie(
        "access_token",
        access,
        max_age=settings.access_token_minutes * 60,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/api",
    )
    response.set_cookie(
        "refresh_token",
        refresh,
        max_age=settings.refresh_token_days * 86400,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/api/auth",
    )
    # The SPA is served at `/`, so the readable double-submit token must also be
    # scoped to `/` for document.cookie to expose it to browser API clients.
    # Expire the legacy `/api` cookie to avoid duplicate names during rollout.
    response.delete_cookie("csrf_token", path="/api", secure=settings.cookie_secure)
    response.set_cookie(
        "csrf_token",
        secrets.token_urlsafe(32),
        max_age=settings.refresh_token_days * 86400,
        secure=settings.cookie_secure,
        httponly=False,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie("access_token", path="/api", secure=settings.cookie_secure)
    response.delete_cookie("refresh_token", path="/api/auth", secure=settings.cookie_secure)
    # Clear both the legacy API-scoped token and the new SPA-readable root token.
    response.delete_cookie("csrf_token", path="/api", secure=settings.cookie_secure)
    response.delete_cookie("csrf_token", path="/", secure=settings.cookie_secure)
