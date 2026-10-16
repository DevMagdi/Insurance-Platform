"""Redis sliding-window rate limits and single-use, hashed mobile OTP challenges.

Two atomic Lua algorithms share one interface:

* **sliding log** (exact) for strict, low-volume limits such as login, OTP and
  complaint submission: a sorted set of request timestamps per identity;
* **sliding window counter** (O(1) memory) for high-volume per-IP / per-user /
  per-endpoint ceilings: the previous fixed window is weighted by its overlap.

Identities are HMAC-hashed before they become Redis keys, so mobiles, e-mails,
IPs and API-key prefixes never appear in Redis in clear text.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException
from redis.asyncio import Redis

from app.observability import metrics

# KEYS[1]=log key; ARGV: now_ms, window_ms, limit, member
_SLIDING_LOG = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = window
  if oldest[2] then retry = window - (now - tonumber(oldest[2])) end
  if retry < 1 then retry = 1 end
  return {0, count, retry}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('PEXPIRE', key, window)
return {1, count + 1, 0}
"""

# KEYS[1]=current bucket, KEYS[2]=previous bucket; ARGV: elapsed_ms, window_ms, limit
_SLIDING_COUNTER = """
local elapsed = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local previous = tonumber(redis.call('GET', KEYS[2]) or '0')
local weighted = previous * ((window - elapsed) / window) + current
if weighted >= limit then
  return {0, math.floor(weighted), window - elapsed}
end
current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('PEXPIRE', KEYS[1], window * 2) end
return {1, math.floor(previous * ((window - elapsed) / window) + current), 0}
"""

_LOG_THRESHOLD = 200  # exact log up to this many entries; counter above it


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds

    def headers(self) -> dict[str, str]:
        values = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            values["Retry-After"] = str(max(1, self.retry_after))
        return values


class RateLimitExceeded(HTTPException):
    def __init__(self, decision: LimitDecision, detail: str = "Too many attempts; try later"):
        super().__init__(status_code=429, detail=detail, headers=decision.headers())
        self.decision = decision


class RateLimiter:
    """Same ``check`` signature as earlier phases, now a true sliding window."""

    def __init__(self, redis: Redis, signing_key: str, multiplier: float = 1.0) -> None:
        self.redis = redis
        self.key = signing_key.encode()
        self.multiplier = multiplier
        self._log = redis.register_script(_SLIDING_LOG)
        self._counter = redis.register_script(_SLIDING_COUNTER)

    def key_for(self, category: str, identity: str) -> str:
        digest = hmac.new(self.key, identity.encode(), hashlib.sha256).hexdigest()
        return f"limit:{category}:{digest}"

    async def hit(self, category: str, identity: str, limit: int, seconds: int) -> LimitDecision:
        effective = max(1, math.ceil(limit * self.multiplier))
        window_ms = seconds * 1000
        now_ms = int(time.time() * 1000)
        key = self.key_for(category, identity)
        if effective <= _LOG_THRESHOLD:
            allowed, count, retry_ms = await self._log(
                keys=[key],
                args=[now_ms, window_ms, effective, f"{now_ms}:{secrets.token_hex(4)}"],
            )
        else:
            bucket = now_ms // window_ms
            allowed, count, retry_ms = await self._counter(
                keys=[f"{{{key}}}:{bucket}", f"{{{key}}}:{bucket - 1}"],  # cluster hash tag
                args=[now_ms - bucket * window_ms, window_ms, effective],
            )
        decision = LimitDecision(
            bool(int(allowed)),
            effective,
            effective - int(count),
            math.ceil(int(retry_ms) / 1000),
        )
        if not decision.allowed:
            metrics.RATE_LIMITED.labels(category=category).inc()
        return decision

    async def check(self, category: str, identity: str, limit: int, seconds: int) -> None:
        decision = await self.hit(category, identity, limit, seconds)
        if not decision.allowed:
            raise RateLimitExceeded(decision)


class MobileOTP:
    def __init__(self, redis: Redis, signing_key: str, ttl: int) -> None:
        self.redis = redis
        self.secret = signing_key.encode()
        self.ttl = ttl

    def _key(self, mobile: str) -> str:
        digest = hmac.new(self.secret, mobile.encode(), hashlib.sha256).hexdigest()
        return f"auth:otp:{digest}"

    def _digest(self, mobile: str, code: str, salt: str) -> str:
        return hmac.new(self.secret, f"{salt}:{mobile}:{code}".encode(), hashlib.sha256).hexdigest()

    async def start(self, mobile: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        value = json.dumps({"salt": salt, "digest": self._digest(mobile, code, salt)})
        if not await self.redis.set(self._key(mobile), value, ex=self.ttl, nx=True):
            raise HTTPException(status_code=429, detail="Wait for the current code to expire")
        return code

    async def cancel(self, mobile: str) -> None:
        await self.redis.delete(self._key(mobile))

    async def verify(self, mobile: str, code: str, *, consume: bool = True) -> None:
        """Validate a code, optionally preserving it for a required identity step.

        Customer registration first validates the OTP to prove control of the
        mobile, then asks for name/national ID. A valid preflight must not consume
        the single-use code: only a completed sign-in or registration does that.
        """
        key = self._key(mobile)
        attempt_key = key + ":attempts"
        attempts = await self.redis.incr(attempt_key)
        await self.redis.expire(attempt_key, self.ttl, nx=True)
        if attempts > 5:
            await self.redis.delete(key)
            raise HTTPException(status_code=429, detail="Too many OTP attempts")
        value = await self.redis.get(key)
        if not value:
            raise HTTPException(status_code=401, detail="Invalid or expired code")
        saved = json.loads(value)
        if not hmac.compare_digest(self._digest(mobile, code, saved["salt"]), saved["digest"]):
            raise HTTPException(status_code=401, detail="Invalid or expired code")
        if not consume:
            # A caller that knows the code has passed verification; don't penalize
            # the second (consuming) request in the registration flow.
            await self.redis.delete(attempt_key)
            return
        # Only ONE concurrent successful verification can consume the code.
        consumed = await self.redis.getdel(key)
        if consumed != value:
            raise HTTPException(status_code=401, detail="Code already used")
        await self.redis.delete(attempt_key)
