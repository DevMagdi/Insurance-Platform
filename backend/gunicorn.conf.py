"""Gunicorn process model for the API (one container = one Gunicorn master).

Tuning rationale (docs/SCALABILITY.md): the API is async and I/O bound, so a
small number of Uvicorn workers per container (default 2 x vCPU, capped) keeps
memory predictable; horizontal scaling is done by the HPA, not by huge pods.
Database connections are NOT pooled in-process (NullPool) - PgBouncer in
transaction mode multiplexes them - so workers can be added freely.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import tempfile

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
worker_class = "app.gunicorn_worker.HardenedUvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", min(8, max(2, multiprocessing.cpu_count() * 2))))
# Recycle workers to bound memory growth from fragmentation; jitter avoids herds.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "20000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "2000"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
# Must be shorter than terminationGracePeriodSeconds (k8s) minus the preStop sleep.
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "25"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "75"))  # > LB idle timeout
backlog = int(os.environ.get("GUNICORN_BACKLOG", "2048"))
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None  # noqa: S108
accesslog = None  # structured access logs come from RequestContextMiddleware
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
forwarded_allow_ips = ""  # the app resolves client IPs itself
proxy_allow_ips = ""
limit_request_line = 8190
limit_request_fields = 100
limit_request_field_size = 8190

_metrics_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")


def on_starting(server: object) -> None:
    # Multiprocess Prometheus: start every boot with an empty shared directory.
    if _metrics_dir:
        shutil.rmtree(_metrics_dir, ignore_errors=True)
        os.makedirs(_metrics_dir, exist_ok=True)


def child_exit(server: object, worker: object) -> None:
    if _metrics_dir:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)  # type: ignore[attr-defined]


if _metrics_dir is None:
    # Gauges/counters from all workers must be aggregated for /metrics.
    _metrics_dir = os.path.join(tempfile.gettempdir(), "prometheus-multiproc")
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = _metrics_dir
