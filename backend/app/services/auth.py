"""Mobile OTP, Argon2 staff sign-in, TOTP and rotating refresh sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Actor, purpose_transaction, scoped_transaction
from app.domain.schemas import CustomerVerifyRequest, StaffPersonalData
from app.repositories.auth import AuthRepository, StaffCredentials
from app.security.blobs import AVATAR, STAFF_PROFILE, BlobCipher
from app.security.crypto import NationalIdProtector, decode_key
from app.security.lockout import LoginLockout
from app.security.passwords import StaffPasswords, TotpProtector
from app.security.rate_limit import MobileOTP, RateLimiter
from app.security.tokens import TokenClaims, TokenManager
from app.services.providers import Delivery, ProviderUnavailable
from app.services.staff_mail import StaffMailService, invitation_url
from app.services.staff_profiles import StaffProfileService
from app.settings import Settings


@dataclass(frozen=True)
class SessionTokens:
    access: str
    refresh: str


@dataclass(frozen=True)
class StaffLoginResult:
    tokens: SessionTokens | None
    challenge: str | None
    next_step: str | None


class AuthService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        delivery: Delivery,
    ) -> None:
        self.factory = factory
        self.redis = redis
        self.settings = settings
        self.delivery = delivery
        self.repo = AuthRepository()
        self.staff_mail = StaffMailService(settings, delivery)
        self.staff_profiles = StaffProfileService(BlobCipher(settings.encryption_keyring()))
        self.tokens = TokenManager(settings)
        self.passwords = StaffPasswords()
        self.totp = TotpProtector(settings)
        self.protector = NationalIdProtector(
            settings.encryption_keyring(), decode_key(settings.app_blind_index_key)
        )
        self.otp = MobileOTP(redis, settings.jwt_signing_key, settings.otp_seconds)
        self.limits = RateLimiter(redis, settings.jwt_signing_key, settings.rate_limit_multiplier)
        self.lockout = LoginLockout(redis, settings.jwt_signing_key)

    async def start_customer_otp(self, mobile: str, ip: str) -> None:
        await self.limits.check("otp:mobile", mobile, 3, 600)
        await self.limits.check("otp:ip", ip, 20, 3600)
        code = await self.otp.start(mobile)
        try:
            await self.delivery.send_otp(mobile, code)
        except ProviderUnavailable as exc:
            await self.otp.cancel(mobile)
            raise HTTPException(status_code=503, detail="SMS delivery not configured") from exc
        except Exception:
            await self.otp.cancel(mobile)
            raise HTTPException(status_code=503, detail="OTP delivery unavailable") from None

    async def verify_customer(
        self,
        item: CustomerVerifyRequest,
        ip: str,
        user_agent: str,
    ) -> SessionTokens:
        await self.limits.check("otp:verify:ip", ip, 30, 3600)
        await self.lockout.ensure_allowed("otp_verify", item.mobile)
        try:
            # A successful code check may still need an identity step (new or
            # shared-mobile customer). Preserve it until login/registration completes.
            await self.otp.verify(item.mobile, item.code, consume=False)
        except HTTPException as exc:
            if exc.status_code in (401, 429):
                await self.lockout.failure("otp_verify", item.mobile)
            raise
        await self.lockout.success("otp_verify", item.mobile)
        blind = self.protector.blind_index(item.national_id) if item.national_id else None
        try:
            async with purpose_transaction(self.factory, "customer_auth") as db:
                matches = await self.repo.customers_by_mobile(db, item.mobile, blind)
                if len(matches) == 1:
                    await self.otp.verify(item.mobile, item.code)
                    customer_id, version = matches[0]
                elif len(matches) > 1:
                    raise HTTPException(
                        status_code=409, detail="Provide national ID to identify the customer"
                    )
                elif item.national_id is not None and item.full_name is not None:
                    await self.otp.verify(item.mobile, item.code)
                    customer_id = await self.repo.register_customer(
                        db,
                        full_name=item.full_name,
                        mobile=item.mobile,
                        ciphertext=self.protector.encrypt(item.national_id),
                        blind=self.protector.blind_index(item.national_id),
                        last4=item.national_id[-4:],
                        email=item.email,
                        whatsapp=item.whatsapp_available,
                    )
                    version = 0
                else:
                    raise HTTPException(
                        status_code=409, detail="Provide national ID and name to register"
                    )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Identity cannot be registered") from exc
        return await self.issue_session(Actor(customer_id, "customer"), version, ip, user_agent)

    async def staff_login(
        self,
        email: str,
        password: str,
        ip: str,
        user_agent: str,
    ) -> StaffLoginResult:
        email = email.lower().strip()
        await self.limits.check("staff:login:ip", ip, 25, 900)
        await self.limits.check("staff:login:email", email, 8, 900)
        # Locks apply identically to unknown e-mails, so they reveal nothing.
        await self.lockout.ensure_allowed("staff_login_ip", ip)
        await self.lockout.ensure_allowed("staff_login", email)
        async with purpose_transaction(self.factory, "staff_auth") as db:
            staff = await self.repo.staff_by_email(db, email)
        if (
            not await self.passwords.verify(staff.password_hash if staff else None, password)
            or staff is None
            or staff.status != "active"
        ):
            await self.lockout.failure("staff_login", email)
            await self.lockout.failure("staff_login_ip", ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        await self.lockout.success("staff_login", email)
        if staff.must_change_password:
            return StaffLoginResult(
                None, await self._challenge(staff, "password_change"), "password_change"
            )
        if staff.two_factor_enabled:
            return StaffLoginResult(
                None, await self._challenge(staff, "totp_verify"), "totp_verify"
            )
        if staff.two_factor_required or staff.global_2fa_required:
            return StaffLoginResult(None, await self._challenge(staff, "totp_setup"), "totp_setup")
        return StaffLoginResult(
            await self.issue_session(Actor(staff.id, "staff"), staff.token_version, ip, user_agent),
            None,
            None,
        )

    async def _challenge(self, staff: StaffCredentials, purpose: str) -> str:
        token = self.tokens.issue(
            Actor(staff.id, "staff"),
            uuid4(),
            staff.token_version,
            "challenge",
            timedelta(minutes=10),
            purpose=purpose,
        )
        claims = self.tokens.parse(token, "challenge")
        await self.redis.set(f"auth:challenge:{claims.jti}", purpose, ex=600)
        return token

    async def _check_challenge(self, token: str, purpose: str) -> TokenClaims:
        claims = self.tokens.parse(token, "challenge")
        if (
            claims.subject.scope != "staff"
            or claims.purpose != purpose
            or not await self.redis.exists(f"auth:challenge:{claims.jti}")
        ):
            raise HTTPException(status_code=401, detail="Invalid authentication challenge")
        await self.limits.check("challenge", str(claims.jti), 5, 600)
        return claims

    async def _consume_challenge(self, claims: TokenClaims, purpose: str) -> None:
        value = await self.redis.getdel(f"auth:challenge:{claims.jti}")
        if value is None or value.decode() != purpose:
            raise HTTPException(status_code=401, detail="Challenge was already used")

    async def _challenge_staff(self, claims: TokenClaims) -> StaffCredentials:
        async with purpose_transaction(self.factory, "staff_auth") as db:
            staff = await self.repo.staff_by_id(db, claims.subject.id)
        if staff is None or staff.status != "active" or staff.token_version != claims.token_version:
            raise HTTPException(status_code=401, detail="Authentication challenge expired")
        return staff

    async def verify_staff_totp(
        self,
        challenge: str,
        code: str,
        ip: str,
        user_agent: str,
    ) -> SessionTokens:
        claims = await self._check_challenge(challenge, "totp_verify")
        identity = str(claims.subject.id)
        await self.lockout.ensure_allowed("staff_totp", identity)
        staff = await self._challenge_staff(claims)
        if (
            not staff.two_factor_enabled
            or not staff.totp_secret_ciphertext
            or not self.totp.verify(self.totp.decrypt(staff.totp_secret_ciphertext), code)
        ):
            await self.lockout.failure("staff_totp", identity)
            raise HTTPException(status_code=401, detail="Invalid authentication code")
        await self.lockout.success("staff_totp", identity)
        await self._consume_challenge(claims, "totp_verify")
        return await self.issue_session(claims.subject, claims.token_version, ip, user_agent)

    async def change_forced_password(self, challenge: str, password: str) -> None:
        claims = await self._check_challenge(challenge, "password_change")
        staff = await self._challenge_staff(claims)
        if not staff.must_change_password:
            raise HTTPException(status_code=409, detail="Password change challenge invalid")
        hashed = await self.passwords.hash(password)
        await self._consume_challenge(claims, "password_change")
        async with scoped_transaction(self.factory, claims.subject) as db:
            if await self.repo.current_version(
                db
            ) != claims.token_version or not await self.repo.change_password(db, hashed):
                raise HTTPException(status_code=401, detail="Authentication challenge expired")

    async def enroll_start(
        self, actor: Actor, sid: UUID, version: int, challenge: str | None = None
    ) -> str:
        if challenge is not None:
            claims = await self._check_challenge(challenge, "totp_setup")
            if (
                claims.subject != actor
                or claims.session_id != sid
                or claims.token_version != version
            ):
                raise HTTPException(status_code=401, detail="Invalid 2FA challenge")
        async with purpose_transaction(self.factory, "staff_auth") as db:
            staff = await self.repo.staff_by_id(db, actor.id)
        if (
            staff is None
            or staff.status != "active"
            or staff.token_version != version
            or staff.two_factor_enabled
        ):
            raise HTTPException(status_code=409, detail="2FA enrollment unavailable")
        secret = self.totp.new_secret()
        await self.redis.set(f"auth:enroll:{actor.id}:{sid}", self.totp.encrypt(secret), ex=600)
        return self.totp.uri(secret, staff.email)

    async def enroll_confirm(
        self, actor: Actor, sid: UUID, version: int, code: str, challenge: str | None = None
    ) -> None:
        if challenge is not None:
            claims = await self._check_challenge(challenge, "totp_setup")
            if (
                claims.subject != actor
                or claims.session_id != sid
                or claims.token_version != version
            ):
                raise HTTPException(status_code=401, detail="Invalid 2FA challenge")
        await self.limits.check("totp:confirm", f"{actor.id}:{sid}", 5, 600)
        key = f"auth:enroll:{actor.id}:{sid}"
        ciphertext = await self.redis.get(key)
        if not ciphertext or not self.totp.verify(self.totp.decrypt(ciphertext), code):
            raise HTTPException(status_code=401, detail="Invalid 2FA code")
        if await self.redis.getdel(key) != ciphertext:
            raise HTTPException(status_code=401, detail="Enrollment already completed")
        if challenge is not None:
            await self._consume_challenge(claims, "totp_setup")
        async with scoped_transaction(self.factory, actor) as db:
            if await self.repo.current_version(db) != version or not await self.repo.set_totp(
                db, ciphertext
            ):
                raise HTTPException(status_code=401, detail="Authentication expired")

    async def issue_session(
        self,
        actor: Actor,
        version: int,
        ip: str,
        user_agent: str,
    ) -> SessionTokens:
        sid, family = uuid4(), uuid4()
        access = self.tokens.issue(
            actor, sid, version, "access", timedelta(minutes=self.settings.access_token_minutes)
        )
        refresh = self.tokens.issue(
            actor, sid, version, "refresh", timedelta(days=self.settings.refresh_token_days)
        )
        async with scoped_transaction(self.factory, actor) as db:
            if await self.repo.current_version(db) != version:
                raise HTTPException(status_code=401, detail="Account access changed; sign in again")
            await self.repo.create_session(
                db,
                actor=actor,
                sid=sid,
                family_id=family,
                digest=self.tokens.digest(refresh),
                version=version,
                expires_at=datetime.now(UTC) + timedelta(days=self.settings.refresh_token_days),
                ip=ip,
                user_agent=user_agent[:256],
            )
            if actor.scope == "staff":
                await self.repo.record_login(db)
        return SessionTokens(access, refresh)

    async def refresh(
        self, refresh_token: str | None, ip: str, user_agent: str
    ) -> SessionTokens | None:
        claims = self.tokens.parse(refresh_token, "refresh")
        sid = uuid4()
        access = self.tokens.issue(
            claims.subject,
            sid,
            claims.token_version,
            "access",
            timedelta(minutes=self.settings.access_token_minutes),
        )
        refresh = self.tokens.issue(
            claims.subject,
            sid,
            claims.token_version,
            "refresh",
            timedelta(days=self.settings.refresh_token_days),
        )
        async with scoped_transaction(self.factory, claims.subject) as db:
            old = await self.repo.get_session(db, claims.session_id, lock=True)
            if old is None:
                return None
            version = await self.repo.current_version(db)
            if (
                old.revoked_at is not None
                or old.expires_at <= datetime.now(UTC)
                or version != claims.token_version
                or old.token_version != version
                or not hmac.compare_digest(
                    old.refresh_digest, self.tokens.digest(refresh_token or "")
                )
            ):
                await self.repo.revoke_family(db, old.family_id)
                return None
            old.revoked_at = datetime.now(UTC)
            old.last_used_at = datetime.now(UTC)
            await self.repo.create_session(
                db,
                actor=claims.subject,
                sid=sid,
                family_id=old.family_id,
                rotated_from=old.id,
                digest=self.tokens.digest(refresh),
                version=claims.token_version,
                expires_at=datetime.now(UTC) + timedelta(days=self.settings.refresh_token_days),
                ip=ip,
                user_agent=user_agent[:256],
            )
        return SessionTokens(access, refresh)

    async def create_invitation(
        self,
        db: AsyncSession,
        email: str,
        name: str,
        department_id: UUID,
    ) -> UUID:
        email = email.lower().strip()
        token = secrets.token_urlsafe(32)
        staff_id = await self.repo.invite(
            db,
            email=email,
            name=name,
            digest=hashlib.sha256(token.encode()).digest(),
            expires_at=datetime.now(UTC) + timedelta(hours=48),
            department_id=department_id,
        )
        try:
            url = invitation_url(self.settings, token)
            await self.staff_mail.send(db, email, url)
        except Exception:
            raise HTTPException(status_code=503, detail="Invitation delivery unavailable") from None
        return staff_id

    async def reset_staff_password(self, db: AsyncSession, user_id: UUID) -> None:
        """Invalidate the old password/sessions; only deliver a one-use reset token."""
        from app.repositories.access import AccessRepository

        token = secrets.token_urlsafe(32)
        email = await AccessRepository().issue_reset(
            db,
            user_id,
            hashlib.sha256(token.encode()).digest(),
            datetime.now(UTC) + timedelta(hours=48),
        )
        if email is None:
            raise HTTPException(status_code=404, detail="Active staff user unavailable")
        try:
            url = invitation_url(self.settings, token)
            await self.staff_mail.send(db, email, url, reset=True)
        except Exception:
            # The request transaction rolls back the credential/session change.
            raise HTTPException(
                status_code=503, detail="Password reset delivery unavailable"
            ) from None

    async def inspect_invitation(self, token: str) -> dict[str, Any]:
        async with purpose_transaction(self.factory, "invitation") as db:
            details = await self.repo.invitation_details(
                db, hashlib.sha256(token.encode()).digest()
            )
        if details is None:
            raise HTTPException(status_code=400, detail="Invalid or expired invitation")
        return {key: value for key, value in details.items() if key != "id"}

    async def resend_invitation(self, db: AsyncSession, user_id: UUID) -> None:
        token = secrets.token_urlsafe(32)
        email = await self.repo.reissue_invite(
            db,
            user_id,
            hashlib.sha256(token.encode()).digest(),
            datetime.now(UTC) + timedelta(hours=48),
        )
        if email is None:
            raise HTTPException(status_code=404, detail="Pending invitation unavailable")
        try:
            await self.staff_mail.send(db, email, invitation_url(self.settings, token))
        except Exception:
            raise HTTPException(status_code=503, detail="Invitation delivery unavailable") from None

    async def accept_invitation(
        self,
        token: str,
        password: str,
        profile: StaffPersonalData | None = None,
        avatar_base64: str | None = None,
    ) -> None:
        digest = hashlib.sha256(token.encode()).digest()
        hashed = await self.passwords.hash(password)
        try:
            async with purpose_transaction(self.factory, "invitation") as db:
                details = await self.repo.invitation_details(db, digest)
                if details is None:
                    raise HTTPException(status_code=400, detail="Invalid or expired invitation")
                user_id = UUID(str(details["id"]))
                personal = blind = avatar = None
                name = last4 = None
                if details["kind"] == "invitation":
                    if profile is None or not avatar_base64:
                        raise HTTPException(
                            status_code=422,
                            detail="Complete your personal details and upload a profile photo",
                        )
                    clean = await self.staff_profiles.normalized_avatar(avatar_base64)
                    personal = self.staff_profiles.blobs.encrypt(
                        profile.model_dump_json().encode(),
                        purpose=STAFF_PROFILE,
                        resource_id=user_id,
                    )
                    avatar = self.staff_profiles.blobs.encrypt(
                        clean, purpose=AVATAR, resource_id=user_id
                    )
                    blind = self.protector.blind_index(profile.national_id)
                    name, last4 = profile.full_name, profile.national_id[-4:]
                elif profile is not None or avatar_base64 is not None:
                    raise HTTPException(
                        status_code=422, detail="Password reset does not change personal details"
                    )
                staff_id = await self.repo.complete_invite(
                    db,
                    digest=digest,
                    user_id=user_id,
                    password_hash=hashed,
                    name=name,
                    personal=personal,
                    blind=blind,
                    last4=last4,
                    avatar=avatar,
                )
                if staff_id is None:
                    raise HTTPException(status_code=400, detail="Invalid or expired invitation")
        except IntegrityError:
            raise HTTPException(
                status_code=409, detail="Profile identity is already registered"
            ) from None
