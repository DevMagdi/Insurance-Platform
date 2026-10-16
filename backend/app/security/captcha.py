"""Cloudflare Turnstile verification for public forms (OTP, tracking, staff login).

The browser obtains a single-use token from the widget and sends it in the
``X-Captcha-Token`` header. Verification is server-side, bound to the expected
action and hostname, and fails closed when Cloudflare is unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request

from app.observability import metrics

logger = logging.getLogger("app.security")
CaptchaAction = Literal["otp_start", "guest_track", "staff_login", "invite_accept"]
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@dataclass(frozen=True)
class CaptchaVerifier:
    secret_key: str | None = None
    site_key: str | None = None
    expected_hostname: str | None = None
    timeout: float = 5.0

    @property
    def enabled(self) -> bool:
        return bool(self.secret_key and self.site_key)

    @classmethod
    def from_settings(
        cls, secret: str | None, site: str | None, public_app_url: str
    ) -> CaptchaVerifier:
        host = urlsplit(public_app_url).hostname
        # Cloudflare test keys report "example.com"; do not bind them to a hostname.
        bound = None if (secret or "").startswith(("1x0000", "2x0000", "3x0000")) else host
        return cls(secret, site, bound)

    def public_config(self) -> dict[str, str] | None:
        if not self.enabled or self.site_key is None:
            return None
        return {"provider": "turnstile", "site_key": self.site_key}

    async def verify(self, token: str | None, action: CaptchaAction, remote_ip: str) -> None:
        if not self.enabled:
            return
        if not token or len(token) > 2048:
            metrics.CAPTCHA_FAILURES.labels(action=action).inc()
            raise HTTPException(status_code=400, detail="CAPTCHA verification required")
        data = {"secret": self.secret_key or "", "response": token, "remoteip": remote_ip}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                response = await client.post(TURNSTILE_VERIFY_URL, data=data)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("captcha.verify unavailable", extra={"event": "captcha.unavailable"})
            raise HTTPException(
                status_code=503, detail="CAPTCHA verification unavailable"
            ) from None
        success = bool(body.get("success"))
        if success and body.get("action") not in (None, "", action):
            success = False
        if success and self.expected_hostname and body.get("hostname") != self.expected_hostname:
            success = False
        if not success:
            metrics.CAPTCHA_FAILURES.labels(action=action).inc()
            raise HTTPException(status_code=400, detail="CAPTCHA verification failed")


def captcha_guard(action: CaptchaAction):  # type: ignore[no-untyped-def]
    """FastAPI dependency factory; the verifier is installed by the app lifespan."""

    async def dependency(request: Request) -> None:
        verifier: CaptchaVerifier | None = getattr(request.app.state, "captcha", None)
        if verifier is None:
            return  # Unit tests without lifespan; production startup always installs one.
        from app.api.deps import client_ip

        await verifier.verify(request.headers.get("X-Captcha-Token"), action, client_ip(request))

    dependency.__name__ = f"captcha_{action}"
    dependency.captcha_action = action  # type: ignore[attr-defined]  # read by openapi_contract
    return dependency
