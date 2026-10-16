"""Structured JSON logs carrying request ID, actor and route on every line.

Context lives in ``contextvars`` so concurrent requests on one event loop never
mix identities. Values that look like secrets/PII are redacted before output:
national IDs, mobiles, bearer tokens and JWTs must never reach a log sink.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)
actor_kind_var: ContextVar[str | None] = ContextVar("actor_kind", default=None)
client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)
route_var: ContextVar[str | None] = ContextVar("route", default=None)

_REDACTIONS = (
    (re.compile(r"\b[23]\d{13}\b"), "[national-id]"),  # Egyptian national ID
    (re.compile(r"(?:\+?20|0)1[0125]\d{8}\b"), "[mobile]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[jwt]"),
    (re.compile(r"(?i)(bearer|apikey)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)(password|secret|token|otp|code)=([^&\s]+)"), r"\1=[redacted]"),
)
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def redact(value: str) -> str:
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def bind_actor(actor_id: str, kind: str) -> None:
    actor_id_var.set(actor_id)
    actor_kind_var.set(kind)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, release: str | None) -> None:
        super().__init__()
        self.service = service
        self.release = release

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
            "service": self.service,
        }
        if self.release:
            payload["release"] = self.release
        for key, var in (
            ("request_id", request_id_var),
            ("user_id", actor_id_var),
            ("actor_kind", actor_kind_var),
            ("client_ip", client_ip_var),
            ("route", route_var),
        ):
            value = var.get()
            if value:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    *, level: str = "INFO", json_logs: bool = True, service: str = "api", release: str | None = None
) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter(service, release))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn/Gunicorn access logs are replaced by our structured access log.
    for noisy in ("uvicorn.access", "gunicorn.access"):
        logging.getLogger(noisy).handlers[:] = []
        logging.getLogger(noisy).propagate = False
    for name in ("uvicorn", "uvicorn.error", "gunicorn.error", "arq"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
