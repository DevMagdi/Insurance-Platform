"""Atomic Redis counters and single-use, hashed mobile OTP challenges."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from fastapi import HTTPException
from redis.asyncio import Redis


class RateLimiter:
    def __init__(self, redis: Redis, signing_key: str) -> None:
        self.redis = redis
        self.key = signing_key.encode()

    def key_for(self, category: str, identity: str) -> str:
        digest = hmac.new(self.key, identity.encode(), hashlib.sha256).hexdigest()
        return f"limit:{category}:{digest}"

    async def check(self, category: str, identity: str, limit: int, seconds: int) -> None:
        key = self.key_for(category, identity)
        count = await self.redis.incr(key)  # INCR is atomic across concurrent requests.
        await self.redis.expire(key, seconds, nx=True)
        if count > limit:
            raise HTTPException(status_code=429, detail="Too many attempts; try later")


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

    async def verify(self, mobile: str, code: str) -> None:
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
        # Only ONE concurrent successful verification can consume the code.
        consumed = await self.redis.getdel(key)
        if consumed != value:
            raise HTTPException(status_code=401, detail="Code already used")
        await self.redis.delete(attempt_key)
