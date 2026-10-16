"""Queue backpressure: refuse new async work while a durable queue is saturated.

The worker publishes queue depths (``app_security.queue_depths()``) to the Redis
hash ``ops:queue_depth`` every minute. Enqueueing endpoints for AI analysis,
voice calls, e-mail/WhatsApp sends and exports call ``ensure_capacity`` and
answer ``503 Retry-After`` instead of letting the backlog grow without bound.
Missing telemetry never blocks traffic (fail-open), because the queues are
durable tables and the limit is a protective, not a correctness, control.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import HTTPException, Request
from redis.asyncio import Redis

from app.observability import metrics

QueueName = Literal["integration", "notification", "insurer", "export"]
DEPTH_KEY = "ops:queue_depth"


class QueueBackpressure:
    def __init__(self, redis: Redis, threshold: int, retry_after: int = 60) -> None:
        self.redis = redis
        self.threshold = threshold
        self.retry_after = retry_after

    async def depth(self, queue: QueueName) -> int | None:
        try:
            raw = await self.redis.hget(DEPTH_KEY, queue)  # type: ignore[misc]
        except Exception:  # Redis trouble must not take writes down with it.
            return None
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def ensure_capacity(self, queue: QueueName) -> None:
        depth = await self.depth(queue)
        if depth is not None and depth >= self.threshold:
            metrics.BACKPRESSURE.labels(queue=queue).inc()
            raise HTTPException(
                status_code=503,
                detail="The system is busy processing earlier requests; retry shortly",
                headers={"Retry-After": str(self.retry_after)},
            )


def backpressure(queue: QueueName) -> Callable[[Request], Awaitable[None]]:
    """Route dependency; inert when the lifespan has not installed a controller."""

    async def dependency(request: Request) -> None:
        controller: QueueBackpressure | None = getattr(request.app.state, "backpressure", None)
        if controller is not None:
            await controller.ensure_capacity(queue)

    dependency.__name__ = f"backpressure_{queue}"
    return dependency
