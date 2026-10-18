"""Unit coverage for the dependency-failure handlers (QA finding L-7, fixed).

The handlers are exercised live by ``qa-live/e2e_resilience.py`` (Redis and
Postgres are really killed there); these tests pin the contract - graceful JSON,
one uniform message for both dependencies, no driver wording - at unit speed.
"""

from __future__ import annotations

import json

from fastapi import Request
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.main import (
    DEPENDENCY_UNAVAILABLE,
    cache_failure,
    database_failure,
    unhandled_failure,
)


def make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/companies",
        "raw_path": b"/api/companies",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "client": ("203.0.113.9", 4242),
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope)


def db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))


async def test_database_handler_answers_graceful_json_503() -> None:
    response = await database_failure(make_request(), db_error())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert json.loads(response.body)["detail"] == DEPENDENCY_UNAVAILABLE
    assert "connection refused" not in response.body.decode()


async def test_cache_handler_answers_graceful_json_503() -> None:
    response = await cache_failure(make_request(), RedisConnectionError("Error 111 connecting"))
    assert response.status_code == 503
    assert json.loads(response.body)["detail"] == DEPENDENCY_UNAVAILABLE
    assert "Error 111" not in response.body.decode()


async def test_last_resort_handler_classifies_dependency_failures() -> None:
    # middleware-level dependency failures reach the catch-all handler, and a
    # raw asyncpg pool-checkout OSError counts as a dependency failure too
    for exc in (
        db_error(),
        RedisConnectionError("down"),
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError(),
    ):
        response = await unhandled_failure(make_request(), exc)
        assert response.status_code == 503
        assert json.loads(response.body)["detail"] == DEPENDENCY_UNAVAILABLE


async def test_last_resort_handler_hides_unexpected_errors() -> None:
    response = await unhandled_failure(make_request(), RuntimeError("secret internals"))
    assert response.status_code == 500
    assert json.loads(response.body)["detail"] == "Internal server error"
    assert "secret internals" not in response.body.decode()
