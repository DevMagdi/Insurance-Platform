"""Progressive brute-force lockout for staff passwords, TOTP and customer OTP.

After ``threshold`` failures inside ``window`` seconds an identity is locked;
every further lockout within 24 hours doubles the duration (capped). The same
response is returned whether or not an account exists, so the lockout cannot be
used to enumerate staff e-mails. Successful authentication clears failures.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import HTTPException
from redis.asyncio import Redis

from app.observability import metrics

logger = logging.getLogger("app.security")


@dataclass(frozen=True)
class LockoutPolicy:
    threshold: int
    window: int
    base_lock: int
    max_lock: int = 86_400


POLICIES: dict[str, LockoutPolicy] = {
    "staff_login": LockoutPolicy(threshold=5, window=900, base_lock=300),
    "staff_login_ip": LockoutPolicy(threshold=30, window=900, base_lock=600),
    "staff_totp": LockoutPolicy(threshold=5, window=900, base_lock=300),
    "otp_verify": LockoutPolicy(threshold=8, window=3600, base_lock=900),
}


class AccountLocked(HTTPException):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            status_code=429,
            detail="Too many failed attempts; try again later",
            headers={"Retry-After": str(max(1, retry_after))},
        )


class LoginLockout:
    def __init__(self, redis: Redis, signing_key: str) -> None:
        self.redis = redis
        self.key = signing_key.encode()

    def _keys(self, kind: str, identity: str) -> tuple[str, str, str]:
        digest = hmac.new(self.key, f"{kind}:{identity}".encode(), hashlib.sha256).hexdigest()
        return (f"lockout:{kind}:{digest}", f"lockfail:{kind}:{digest}", f"lockn:{kind}:{digest}")

    async def ensure_allowed(self, kind: str, identity: str) -> None:
        lock, _, _ = self._keys(kind, identity)
        ttl = await self.redis.ttl(lock)
        if ttl and ttl > 0:
            raise AccountLocked(int(ttl))

    async def failure(self, kind: str, identity: str) -> int | None:
        """Record a failure; returns the lock duration when a lock was applied."""
        policy = POLICIES[kind]
        lock, failures, count = self._keys(kind, identity)
        metrics.AUTH_FAILURES.labels(kind=kind).inc()
        attempts = int(await self.redis.incr(failures))
        if attempts == 1:
            await self.redis.expire(failures, policy.window)
        if attempts < policy.threshold:
            return None
        previous = int(await self.redis.incr(count))
        await self.redis.expire(count, 86_400)
        duration = int(min(policy.max_lock, policy.base_lock * 2 ** (previous - 1)))
        await self.redis.set(lock, "1", ex=duration)
        await self.redis.delete(failures)
        metrics.LOCKOUTS.labels(kind=kind).inc()
        logger.warning(
            "security.lockout applied",
            extra={"event": "security.lockout", "kind": kind, "duration_seconds": duration},
        )
        return duration

    async def success(self, kind: str, identity: str) -> None:
        _, failures, _ = self._keys(kind, identity)
        await self.redis.delete(failures)
