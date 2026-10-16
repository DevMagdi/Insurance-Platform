"""HTTP edge policy shared by the pure-ASGI middleware stack.

Middleware is constructed at import time, but settings are only loaded in the
application lifespan, so the active policy lives on ``app.state.http_policy``.
Tests that skip the lifespan get the conservative defaults below.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any

from app.security.proxy import DEFAULT_TRUSTED_PROXIES, ClientIPResolver

_DEFAULT_RESOLVER = ClientIPResolver.from_csv(DEFAULT_TRUSTED_PROXIES)


@dataclass(frozen=True)
class HttpPolicy:
    resolver: ClientIPResolver = _DEFAULT_RESOLVER
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    hsts_seconds: int | None = None
    max_json_body: int = 1_048_576
    max_upload_body: int = 10_485_760
    production: bool = False
    metrics_token: str | None = None
    abuse_denied_threshold: int = 40


DEFAULT_POLICY = HttpPolicy()


def policy_for(scope: MutableMapping[str, Any]) -> HttpPolicy:
    app = scope.get("app")
    state = getattr(app, "state", None)
    policy = getattr(state, "http_policy", None)
    return policy if isinstance(policy, HttpPolicy) else DEFAULT_POLICY


def route_template(scope: MutableMapping[str, Any]) -> str:
    """A bounded-cardinality route label (never the raw, ID-bearing path)."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "unmatched"
