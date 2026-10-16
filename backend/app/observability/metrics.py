"""Prometheus metrics. Labels are low-cardinality: never user IDs, IPs or raw paths.

Gunicorn runs several worker processes, so when ``PROMETHEUS_MULTIPROC_DIR`` is
set the exposition aggregates every worker through ``MultiProcessCollector``.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

if _multiproc_dir := os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    # Workers outside Gunicorn (arq, scripts) start with an empty tmpfs.
    os.makedirs(_multiproc_dir, exist_ok=True)

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests by route template", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by route template",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Requests currently being served",
    ["method"],
    multiprocess_mode="livesum",
)
RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by a rate limit", ["category"])
AUTH_FAILURES = Counter("auth_failures_total", "Failed authentication attempts", ["kind"])
LOCKOUTS = Counter("account_lockouts_total", "Progressive lockouts applied", ["kind"])
ACCESS_DENIED = Counter(
    "access_denied_total",
    "Authenticated requests denied (403) or hidden (404) by authorization/scope",
    ["status", "actor_kind"],
)
SUSPICIOUS_ACCESS = Counter(
    "suspicious_access_total", "Abuse-detector findings (possible IDOR/enumeration)", ["reason"]
)
CAPTCHA_FAILURES = Counter("captcha_failures_total", "Rejected CAPTCHA tokens", ["action"])
BACKPRESSURE = Counter(
    "queue_backpressure_rejections_total",
    "Requests refused because a queue is saturated",
    ["queue"],
)
QUEUE_DEPTH = Gauge(
    "queue_depth", "Last observed durable queue depth", ["queue"], multiprocess_mode="max"
)
CACHE_REQUESTS = Counter("cache_requests_total", "Scoped cache lookups", ["cache", "result"])
SENSITIVE_ACTIONS = Counter(
    "sensitive_actions_total", "Privileged data actions (exports, PII reveal, privacy)", ["action"]
)
REQUEST_BODY_REJECTED = Counter(
    "request_body_rejected_total", "Requests rejected for exceeding body limits", ["reason"]
)


def render_latest() -> tuple[bytes, str]:
    """Exposition for this process, or every Gunicorn worker in multiprocess mode."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
