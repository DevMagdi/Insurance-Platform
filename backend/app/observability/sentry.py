"""Optional Sentry error tracking with PII scrubbing (PDPL: no personal data leaves)."""

from __future__ import annotations

from typing import Any

from app.observability.logging import redact

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-upload-token",
    "x-download-token",
    "x-captcha-token",
    "national_id",
    "mobile",
    "password",
    "code",
    "token",
    "challenge",
    "email",
}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if str(key).lower() in _SENSITIVE_KEYS else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        request.pop("data", None)  # request bodies may contain national IDs
        if "headers" in request:
            request["headers"] = _scrub(request["headers"])
        if "query_string" in request:
            request["query_string"] = "[redacted]"
    user = event.get("user")
    if isinstance(user, dict):  # keep only the opaque UUID
        event["user"] = {"id": user.get("id")} if user.get("id") else {}
    for key in ("message", "logentry", "exception", "extra", "contexts", "breadcrumbs"):
        if key in event:
            event[key] = _scrub(event[key])
    return event


def init_sentry(dsn: str | None, environment: str, release: str | None, traces: float) -> bool:
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        send_default_pii=False,
        traces_sample_rate=traces,
        before_send=before_send,  # type: ignore[arg-type]
        max_request_body_size="never",
    )
    return True
