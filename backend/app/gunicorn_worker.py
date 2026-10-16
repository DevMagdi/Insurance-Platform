"""Uvicorn worker for Gunicorn with production-safe defaults.

* ``server_header=False`` - do not advertise the server implementation;
* ``proxy_headers=False`` - X-Forwarded-For is resolved by the application
  (``TRUSTED_PROXY_CIDRS``), never trusted blindly by the ASGI server;
* the lifespan is always run so every worker builds its own pools.
"""

from __future__ import annotations

from typing import Any, ClassVar

from uvicorn_worker import UvicornWorker


class HardenedUvicornWorker(UvicornWorker):  # type: ignore[misc]
    CONFIG_KWARGS: ClassVar[dict[str, Any]] = {
        "loop": "auto",
        "http": "auto",
        "lifespan": "on",
        "server_header": False,
        "date_header": True,
        "proxy_headers": False,
        "timeout_graceful_shutdown": 25,
    }
