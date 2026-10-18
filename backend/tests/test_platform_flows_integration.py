"""Live end-to-end platform flows against real PostgreSQL and real Redis.

This suite is the harsh, black-box counterpart to the unit tests: it drives the
*production* ASGI stack (all middleware, real FastAPI dependencies, real
PostgreSQL RLS, real Redis rate limiter) over HTTP and walks the platform the
way a real user does -- staff sign-in, customer OTP sign-in, complaints,
tickets, departments, teams, roles, permissions, branding, integrations,
reports, exports, privacy and audit.

The central assertion is the 422 guard: every request a human can make from
the shipped web/mobile clients must not come back ``422 Unprocessable
Entity``. A 422 means the client and the server disagree about the contract,
which is exactly the class of defect that used to reach production.

Requires disposable TEST_DATABASE_ADMIN_URL and TEST_DIRECT_DATABASE_URL.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import fakeredis.aioredis
import psycopg
import pytest
from httpx import ASGITransport, AsyncClient, Response
from PIL import Image

from app.domain.permissions import PERMISSIONS, permission_parts
from app.main import app, install_state
from app.settings import Settings

pytestmark = pytest.mark.integration

BASE = "https://testserver"
PASSWORD = "Cedar-River-Garden-49"
# The analytics windows are mandatory; a day either side keeps every request
# inside the 366-day ceiling the API enforces.
NOW = datetime.now(UTC)
WINDOW = {
    "date_from": (NOW - timedelta(days=29)).isoformat(),
    "date_to": NOW.isoformat(),
}


def _admin_dsn() -> str:
    url = os.getenv("TEST_DATABASE_ADMIN_URL", "")
    if not url:
        pytest.skip("Set TEST_DATABASE_ADMIN_URL for real PostgreSQL tests")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _runtime_dsn() -> str:
    url = os.getenv("TEST_DIRECT_DATABASE_URL", "")
    if not url:
        pytest.skip("Set TEST_DIRECT_DATABASE_URL for real PostgreSQL tests")
    return url


def _settings(runtime: str) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        DATABASE_URL=runtime,
        TURNSTILE_SITE_KEY=None,
        TURNSTILE_SECRET_KEY=None,
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
        PUBLIC_APP_URL=BASE,
        COOKIE_SECURE="false",
        RATE_LIMIT_MULTIPLIER="1000",
        ABUSE_DENIED_THRESHOLD="1000",
    )


class CapturingDelivery:
    """Stands in for SMTP so OTP codes and invitation mails are observable.

    The method set mirrors the production ``Delivery`` protocol exactly so a
    missing or renamed method surfaces here instead of at runtime.
    """

    def __init__(self) -> None:
        self.otp_codes: dict[str, str] = {}
        self.messages: list[dict[str, Any]] = []

    async def send_otp(self, email: str, code: str) -> None:
        self.otp_codes[email.lower()] = code
        self.messages.append({"to": email, "subject": "one-time code", "body": code})

    async def send_invitation(self, email: str, url: str) -> None:
        self.messages.append({"to": email, "subject": "invitation", "body": url})

    async def send_password_reset(self, email: str, url: str) -> None:
        self.messages.append({"to": email, "subject": "password reset", "body": url})

    async def send_notice(
        self,
        mobile: str,
        email: str | None,
        kind: str,
        payload: dict[str, Any],
        preferences: dict[str, Any] | None = None,
    ) -> bool:
        self.messages.append({"to": email or mobile, "subject": kind, "body": str(payload)})
        return True

    async def send_insurer(self, email: str, ticket: str, reason: str) -> bool:
        self.messages.append({"to": email, "subject": "insurer notice", "body": ticket})
        return True

    async def send_report(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_name: str,
        content_type: str,
        data: bytes,
    ) -> bool:
        self.messages.append({"to": ",".join(recipients), "subject": subject, "body": body})
        return True

    async def close(self) -> None:
        return None


class CapturingStaffMail:
    """Workplace-mail stand-in with the exact ``StaffMailService.send`` signature.

    The production service prefers an email provider configured in the
    database. A shared test database may carry one left by another suite (an
    unreachable SMTP host), which would turn every invitation into a 503 that
    has nothing to do with the flow under test. This suite captures the mail
    regardless of the configured providers.
    """

    def __init__(self, delivery: CapturingDelivery) -> None:
        self.fallback = delivery

    async def send(self, db: Any, email: str, url: str, *, reset: bool = False) -> None:
        if reset:
            await self.fallback.send_password_reset(email, url)
        else:
            await self.fallback.send_invitation(email, url)


@dataclass
class World:
    admin_dsn: str
    runtime: str
    delivery: CapturingDelivery
    admin_id: UUID
    admin_email: str
    admin_hash: str
    company_id: UUID
    customer_mobile: str
    customer_email: str
    national_id: str
    invitee_national_id: str = ""
    department_id: UUID = field(default_factory=uuid4)
    team_id: UUID = field(default_factory=uuid4)
    role_id: UUID = field(default_factory=uuid4)
    admin_role_id: UUID = field(default_factory=uuid4)
    agent_id: UUID = field(default_factory=uuid4)
    previous_super_admin: UUID | None = None
    tag: str = field(default_factory=lambda: uuid4().hex[:10])


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 24), "teal").save(output, format="PNG")
    return output.getvalue()


def _seed(world: World) -> None:
    """Create the world with raw SQL on the administrative connection."""
    with psycopg.connect(world.admin_dsn, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO departments(id, name, description, is_active) VALUES (%s, %s, %s, true)",
            (world.department_id, f"قسم الاختبار {world.tag}", "قسم مخصص لاختبار التدفقات"),
        )
        for staff_id, email, name in (
            (world.admin_id, world.admin_email, "Flow Admin"),
            (world.agent_id, f"flow-agent-{world.tag}@example.test", "Flow Agent"),
        ):
            cur.execute(
                "INSERT INTO staff_users(id, email, full_name, password_hash, status, "
                "must_change_password, department_id) "
                "VALUES (%s, %s, %s, %s, 'active', false, %s)",
                (staff_id, email, name, world.admin_hash, world.department_id),
            )
            cur.execute(
                "INSERT INTO user_ownership_level(user_id, level) VALUES (%s, 'all')",
                (staff_id,),
            )
        # The suite is self-contained: a disposable database only has the
        # migrations, so the permission catalogue and an administrator role
        # are created here instead of relying on `bootstrap_admin`/`seed`.
        cur.executemany(
            "INSERT INTO permissions(code, resource, action) VALUES (%s, %s, %s) "
            "ON CONFLICT (code) DO NOTHING",
            [(code, *permission_parts(code)) for code in sorted(PERMISSIONS)],
        )
        cur.execute(
            "INSERT INTO roles(id, name, description) VALUES (%s, %s, %s)",
            (world.admin_role_id, f"flow-admin-{world.tag}", "Flow administrator role"),
        )
        cur.execute(
            "INSERT INTO role_permissions(role_id, permission_code) "
            "SELECT %s, code FROM permissions",
            (world.admin_role_id,),
        )
        cur.execute(
            "INSERT INTO user_roles(user_id, role_id) VALUES (%s, %s)",
            (world.admin_id, world.admin_role_id),
        )
        cur.execute(
            "INSERT INTO roles(id, name, description) VALUES (%s, %s, %s)",
            (world.role_id, f"flow-agent-{world.tag}", "Flow agent role"),
        )
        cur.execute(
            "INSERT INTO role_permissions(role_id, permission_code) "
            "SELECT %s, code FROM permissions",
            (world.role_id,),
        )
        cur.execute(
            "INSERT INTO user_roles(user_id, role_id) VALUES (%s, %s)",
            (world.agent_id, world.role_id),
        )
        cur.execute(
            "INSERT INTO insurance_companies(id, slug, name_en, name_ar, supported_types) "
            "VALUES (%s, %s, %s, %s, ARRAY['health','general','both']::insurance_type[])",
            (world.company_id, f"flow-{world.tag}", "Flow Insurer", "شركة الاختبار"),
        )
        cur.execute(
            "INSERT INTO user_company_scope(user_id, company_id, all_companies) "
            "VALUES (%s, NULL, true)",
            (world.admin_id,),
        )
        cur.execute(
            "INSERT INTO user_company_scope(user_id, company_id, all_companies) "
            "VALUES (%s, %s, false)",
            (world.agent_id, world.company_id),
        )
        cur.execute(
            "INSERT INTO teams(id, name, lead_staff_id) VALUES (%s, %s, %s)",
            (world.team_id, f"فريق الاختبار {world.tag}", world.admin_id),
        )
        cur.execute(
            "INSERT INTO team_members(team_id, user_id) VALUES (%s, %s)",
            (world.team_id, world.agent_id),
        )
        # Platform-wide administration is guarded by a single super-admin row.
        cur.execute("SELECT staff_user_id FROM platform_super_admin WHERE singleton")
        row = cur.fetchone()
        world.previous_super_admin = row[0] if row else None
        cur.execute(
            "INSERT INTO platform_super_admin(singleton, staff_user_id) VALUES (true, %s) "
            "ON CONFLICT (singleton) DO UPDATE SET staff_user_id = EXCLUDED.staff_user_id",
            (world.admin_id,),
        )


def _cleanup(world: World) -> None:
    """Best-effort teardown; a failure here must never mask a test failure.

    The whole sweep runs in one transaction with a savepoint per statement: a
    single unexpected foreign key rolls back that statement only, so the
    database never fills up with test rows.
    """
    _restore_super_admin(world)
    try:
        with psycopg.connect(world.admin_dsn, prepare_threshold=None) as conn, conn.cursor() as cur:
            for statement, parameters in _cleanup_statements(cur, world):
                _step(cur, statement, parameters)
            conn.commit()
    except Exception as error:  # pragma: no cover - teardown is best effort
        print(f"teardown sweep failed: {error}", file=sys.stderr)


def _step(cur: psycopg.Cursor[Any], statement: str, parameters: tuple[Any, ...]) -> None:
    """Run one cleanup statement; a failure only rolls back that statement."""
    try:
        cur.execute("SAVEPOINT cleanup_step")
        cur.execute(statement, parameters)
        cur.execute("RELEASE SAVEPOINT cleanup_step")
    except Exception as error:  # pragma: no cover - teardown is best effort
        cur.execute("ROLLBACK TO SAVEPOINT cleanup_step")
        print(f"teardown step failed ({statement}): {error}", file=sys.stderr)


def _cleanup_statements(
    cur: psycopg.Cursor[Any], world: World
) -> list[tuple[str, tuple[Any, ...]]]:
    """The ordered list of statements that remove everything the test created.

    The rows that *reference* the test rows go first (complaints, audit logs,
    events, ...), then the test rows themselves: the schema mixes CASCADE and
    RESTRICT foreign keys, so the order is what makes the sweep succeed.
    """
    staff_ids = [world.admin_id, world.agent_id]
    cur.execute("SELECT id FROM customers WHERE mobile = %s", (world.customer_mobile,))
    customer_ids = [row[0] for row in cur.fetchall()]
    owners: list[tuple[str, list[Any]]] = [
        ("staff_users", staff_ids),
        ("insurance_companies", [world.company_id]),
        ("customers", customer_ids),
        ("departments", [world.department_id]),
        ("teams", [world.team_id]),
        ("roles", [world.role_id, world.admin_role_id]),
    ]
    statements: list[tuple[str, tuple[Any, ...]]] = []
    for owner, values in owners:
        if not values:
            continue
        cur.execute(
            """
            SELECT c.conrelid::regclass::text AS child, a.attname AS column_name,
                   EXISTS (
                       SELECT 1 FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = c.conrelid::regclass::text
                         AND column_name = 'singleton'
                   ) AS is_singleton
            FROM pg_constraint c
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            JOIN pg_class cls ON cls.oid = c.conrelid
            WHERE c.contype = 'f'
              AND c.confrelid = %s::regclass
              AND NOT cls.relispartition
              AND c.conrelid <> 'platform_super_admin'::regclass
            """,
            (owner,),
        )
        for child, column_name, is_singleton in cur.fetchall():
            if is_singleton:
                # Platform-wide singletons (security_settings, ...) are not
                # owned by the test: clear the reference instead of deleting
                # the row, or the whole platform loses its settings.
                statements.append(
                    (
                        f'UPDATE "{child}" SET "{column_name}" = NULL '
                        f'WHERE "{column_name}" = ANY(%s)',
                        (values,),
                    )
                )
            else:
                statements.append(
                    (f'DELETE FROM "{child}" WHERE "{column_name}" = ANY(%s)', (values,))
                )
    # Everything the flows create carries the unique tag of the test
    # (departments, roles, invitations, companies). Sweeping by tag keeps the
    # database pristine between runs.
    pattern = f"%{world.tag}%"
    statements.extend(
        [
            (
                "DELETE FROM staff_invitations WHERE staff_user_id IN "
                "(SELECT id FROM staff_users WHERE email LIKE %s)",
                (pattern,),
            ),
            (
                "DELETE FROM user_roles WHERE user_id IN "
                "(SELECT id FROM staff_users WHERE email LIKE %s)",
                (pattern,),
            ),
            (
                "DELETE FROM user_company_scope WHERE user_id IN "
                "(SELECT id FROM staff_users WHERE email LIKE %s)",
                (pattern,),
            ),
            (
                "DELETE FROM user_ownership_level WHERE user_id IN "
                "(SELECT id FROM staff_users WHERE email LIKE %s)",
                (pattern,),
            ),
            (
                "DELETE FROM auth_sessions WHERE subject_id IN "
                "(SELECT id FROM staff_users WHERE email LIKE %s)",
                (pattern,),
            ),
            ("DELETE FROM staff_users WHERE email LIKE %s", (pattern,)),
            (
                "DELETE FROM role_permissions WHERE role_id IN "
                "(SELECT id FROM roles WHERE name LIKE %s)",
                (pattern,),
            ),
            (
                "DELETE FROM user_roles WHERE role_id IN (SELECT id FROM roles WHERE name LIKE %s)",
                (pattern,),
            ),
            ("DELETE FROM roles WHERE name LIKE %s", (pattern,)),
            ("DELETE FROM departments WHERE name LIKE %s", (pattern,)),
            ("DELETE FROM insurance_companies WHERE slug LIKE %s", (pattern,)),
        ]
    )
    statements.extend(
        [
            ("DELETE FROM team_members WHERE team_id = %s", (world.team_id,)),
            ("DELETE FROM teams WHERE id = %s", (world.team_id,)),
            ("DELETE FROM role_permissions WHERE role_id = %s", (world.role_id,)),
            ("DELETE FROM user_roles WHERE role_id = %s", (world.role_id,)),
            ("DELETE FROM roles WHERE id = %s", (world.role_id,)),
            ("DELETE FROM departments WHERE id = %s", (world.department_id,)),
            ("DELETE FROM insurance_companies WHERE id = %s", (world.company_id,)),
            ("DELETE FROM customers WHERE mobile = %s", (world.customer_mobile,)),
            ("DELETE FROM staff_users WHERE id = ANY(%s)", (staff_ids,)),
        ]
    )
    return statements


def _restore_super_admin(world: World) -> None:
    """Give the platform back to its real owner.

    The row references ``staff_users(id) ON DELETE RESTRICT``, so it must be
    restored before the test admin is deleted -- and in its own transaction,
    so a later failure cannot roll it back and leave the platform owned by a
    test user.
    """
    try:
        with psycopg.connect(world.admin_dsn, prepare_threshold=None) as conn, conn.cursor() as cur:
            if world.previous_super_admin is None:
                cur.execute("DELETE FROM platform_super_admin WHERE singleton")
            else:
                cur.execute(
                    "UPDATE platform_super_admin SET staff_user_id = %s WHERE singleton",
                    (world.previous_super_admin,),
                )
            conn.commit()
    except Exception as error:  # pragma: no cover - teardown is best effort
        print(f"teardown could not restore the platform super admin: {error}", file=sys.stderr)


class Api:
    """HTTP helper that turns an unexpected 422 into an immediate failure."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.unexpected: list[str] = []
        self.calls = 0

    def _sync_csrf(self) -> None:
        token = self.client.cookies.get("csrf_token")
        if token:
            self.client.headers["X-CSRF-Token"] = token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        expect: int | tuple[int, ...] = (200, 201, 202, 204),
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        self._sync_csrf()
        response = await self.client.request(
            method, path, json=json, params=params, content=content, headers=headers
        )
        self.calls += 1
        if response.status_code == 422:
            self.unexpected.append(f"{method} {path} -> 422 {response.text[:400]}")
        if isinstance(expect, int):
            expect = (expect,)
        if response.status_code not in expect:
            pytest.fail(
                f"{method} {path} -> {response.status_code} (expected {expect})\n"
                f"{response.text[:800]}"
            )
        return response

    async def get(self, path: str, **kw: Any) -> Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Response:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> Response:
        return await self.request("PUT", path, **kw)

    async def patch(self, path: str, **kw: Any) -> Response:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Response:
        return await self.request("DELETE", path, **kw)

    async def relogin(self, email: str, password: str) -> None:
        """Start a fresh session (team/role changes invalidate the old one)."""
        self.client.cookies.clear()
        response = await self.request(
            "POST",
            "/api/auth/staff/login",
            json={"email": email, "password": password},
            expect=(200,),
        )
        assert response.json()["next_step"] == "authenticated"

    def assert_clean(self) -> None:
        assert not self.unexpected, "unexpected 422 responses:\n" + "\n".join(self.unexpected)


@pytest.fixture
async def world() -> AsyncIterator[World]:
    from app.security.passwords import StaffPasswords

    admin_dsn = _admin_dsn()
    runtime = _runtime_dsn()
    token = uuid4().hex
    # Every row the test creates carries this tag, so the teardown can sweep
    # the database by tag and leave nothing behind.
    tag = uuid4().hex[:10]
    world = World(
        admin_dsn=admin_dsn,
        runtime=runtime,
        delivery=CapturingDelivery(),
        admin_id=uuid4(),
        admin_email=f"flow-admin-{tag}@example.test",
        admin_hash=await StaffPasswords().hash(PASSWORD),
        company_id=uuid4(),
        customer_mobile=f"+201{int(token[:9], 16) % 1_000_000_000:09d}",
        customer_email=f"flow-customer-{token}@example.test",
        # A structurally valid Egyptian national ID: century, year, month, day,
        # governorate, serial and check digit.
        national_id=(
            f"290{int(token[9:11], 16) % 12 + 1:02d}{int(token[11:13], 16) % 28 + 1:02d}"
            f"01{int(token[13:17], 16) % 10000:04d}{int(token[17], 16) % 10}"
        ),
        invitee_national_id=(
            f"290{int(token[9:11], 16) % 12 + 1:02d}{int(token[11:13], 16) % 28 + 1:02d}"
            f"02{int(token[13:17], 16) % 10000:04d}{int(token[17], 16) % 10}"
        ),
        tag=tag,
    )
    _seed(world)
    saved = dict(app.state._state)
    redis = fakeredis.aioredis.FakeRedis()
    close = await install_state(app, _settings(runtime), redis=redis)
    app.state.captcha = None
    app.state.auth.delivery = world.delivery
    app.state.auth.staff_mail = CapturingStaffMail(world.delivery)
    try:
        yield world
    finally:
        await close()
        await redis.flushdb()
        await redis.aclose()
        app.state._state.clear()
        app.state._state.update(saved)
        import asyncio as _asyncio

        await _asyncio.to_thread(_cleanup, world)


@pytest.fixture
async def staff(world: World) -> AsyncIterator[Api]:
    client = AsyncClient(transport=ASGITransport(app=app), base_url=BASE)
    api = Api(client)
    response = await api.post(
        "/api/auth/staff/login",
        json={"email": world.admin_email, "password": PASSWORD},
        expect=(200,),
    )
    assert response.json()["next_step"] == "authenticated"
    try:
        yield api
    finally:
        await client.aclose()


@pytest.fixture
async def customer(world: World) -> AsyncIterator[Api]:
    client = AsyncClient(transport=ASGITransport(app=app), base_url=BASE)
    api = Api(client)
    await api.post(
        "/api/auth/customer/otp/start",
        json={"mobile": world.customer_mobile, "email": world.customer_email},
        expect=202,
    )
    code = world.delivery.otp_codes.get(world.customer_email.lower())
    assert code, "the OTP was never delivered to the captured mailbox"
    response = await api.post(
        "/api/auth/customer/otp/verify",
        json={
            "mobile": world.customer_mobile,
            "email": world.customer_email,
            "code": code,
            "national_id": world.national_id,
            "full_name": "عميل الاختبار",
        },
        expect=200,
    )
    assert response.json()["authenticated_as"] == "customer"
    try:
        yield api
    finally:
        await client.aclose()


async def _create_complaint(api: Api, world: World) -> str:
    response = await api.post(
        "/api/customer/complaints",
        json={
            "national_id": world.national_id,
            "mobile": world.customer_mobile,
            "company_id": str(world.company_id),
            "insurance_type": "health",
            "policy_number": "POL-2026-0001",
            "subject": "تأخر صرف التعويض",
            "description": "تم تقديم طلب التعويض قبل شهرين ولم يصل أي رد حتى الآن.",
        },
        headers={"Idempotency-Key": str(uuid4())},
        expect=(200, 201),
    )
    body = response.json()
    complaint_id = body.get("id") or (body.get("complaint") or {}).get("id")
    assert complaint_id, f"complaint creation returned no id: {body}"
    return str(complaint_id)


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #


async def test_staff_sign_in_and_session_management(staff: Api, world: World) -> None:
    me = await staff.get("/api/me")
    body = me.json()
    assert body["kind"] == "staff"
    assert body["access"]["status"] == "active"

    await staff.get("/api/me/notifications")
    await staff.get("/api/me/sessions")
    await staff.post("/api/me/sessions/revoke-all", expect=(200, 204))
    staff.assert_clean()


async def test_customer_otp_sign_in_and_profile(customer: Api, world: World) -> None:
    profile = await customer.get("/api/customer/profile")
    assert profile.json()["mobile"] == world.customer_mobile

    await customer.patch(
        "/api/customer/profile",
        json={
            "full_name": "عميل الاختبار المحدث",
            "email": world.customer_email,
            "whatsapp_available": True,
            "notification_preferences": {"email": True, "whatsapp": False, "sms": True},
        },
    )
    await customer.get("/api/customer/complaints")
    await customer.get("/api/customer/complaints/counters")
    await customer.get("/api/customer/notifications")
    await customer.get("/api/customer/privacy/consents")
    await customer.get("/api/customer/privacy/requests")
    customer.assert_clean()


async def test_customer_complaint_lifecycle(customer: Api, world: World) -> None:
    complaint_id = await _create_complaint(customer, world)

    await customer.get(f"/api/customer/complaints/{complaint_id}")
    await customer.get(f"/api/customer/complaints/{complaint_id}/timeline")
    await customer.get(f"/api/customer/complaints/{complaint_id}/attachments")
    await customer.post(
        f"/api/customer/complaints/{complaint_id}/reply",
        json={"body": "شكراً لكم، أود متابعة حالة الطلب."},
    )
    await customer.post(
        f"/api/customer/complaints/{complaint_id}/voice-consent", json={"enabled": True}
    )
    await customer.post(
        f"/api/customer/complaints/{complaint_id}/csat",
        json={"score": 4, "comment": "خدمة جيدة"},
        expect=(200, 201, 409),
    )
    await customer.post(
        "/api/customer/privacy/consents", json={"purpose": "marketing", "granted": False}
    )
    await customer.post(
        "/api/customer/privacy/requests",
        json={"kind": "access", "details": "أريد نسخة من بياناتي"},
    )
    customer.assert_clean()


async def test_customer_attachment_upload_plan(customer: Api, world: World) -> None:
    complaint_id = await _create_complaint(customer, world)
    payload = _png()
    plan = await customer.post(
        f"/api/customer/complaints/{complaint_id}/attachments/plan",
        json={
            "files": [
                {
                    "filename": "receipt.png",
                    "mime_type": "image/png",
                    "size_bytes": len(payload),
                    "checksum_sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
        },
    )
    body = plan.json()
    upload_id = body.get("upload_id") or (body.get("uploads") or [{}])[0].get("id")
    assert upload_id, f"no upload id in plan response: {body}"
    reservation = (body.get("uploads") or [{}])[0]
    await customer.put(
        f"/api/customer/attachments/uploads/{upload_id}/content",
        content=payload,
        headers={
            "Content-Type": "image/png",
            "X-Upload-Token": reservation.get("upload_token", ""),
        },
    )
    customer.assert_clean()


async def test_staff_ticket_lifecycle(staff: Api, customer: Api, world: World) -> None:
    complaint_id = await _create_complaint(customer, world)

    await staff.get("/api/staff/complaints")
    await staff.get("/api/staff/complaints/counters")
    await staff.get("/api/staff/complaints/filter-options")
    await staff.get(f"/api/staff/complaints/{complaint_id}")
    await staff.get(f"/api/staff/complaints/{complaint_id}/timeline")
    await staff.get(f"/api/staff/complaints/{complaint_id}/sla")
    await staff.get(f"/api/staff/complaints/{complaint_id}/duplicates")
    await staff.get(f"/api/staff/complaints/{complaint_id}/canned-replies")

    await staff.post(
        f"/api/staff/complaints/{complaint_id}/assign",
        json={"agent_id": str(world.agent_id)},
    )
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/reply",
        json={"body": "نتابع طلبك مع شركة التأمين."},
    )
    await staff.post(f"/api/staff/complaints/{complaint_id}/notes", json={"body": "ملاحظة داخلية"})
    await staff.patch(f"/api/staff/complaints/{complaint_id}/status", json={"status": "in_review"})
    await staff.patch(f"/api/staff/complaints/{complaint_id}/status", json={"status": "resolved"})
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/escalate",
        json={"reason": "تجاوز مدة الحل"},
        expect=(200, 201, 409),
    )
    staff.assert_clean()


async def test_staff_complaint_filter_accepts_single_character_search(
    staff: Api, world: World
) -> None:
    """Regression: a one-character search must not 422 the list or the counters."""
    for term in ("a", "ب", "1"):
        await staff.get("/api/staff/complaints", params={"search": term})
        await staff.get("/api/staff/complaints/counters", params={"search": term})
    await staff.get("/api/staff/complaints", params={"search": "   "})
    staff.assert_clean()


async def test_staff_saved_views(staff: Api, world: World) -> None:
    created = await staff.post(
        "/api/staff/views",
        json={"name": "Flow view", "criteria": {"status": "new", "page": 1, "page_size": 20}},
    )
    view_id = created.json()["id"]
    await staff.get("/api/staff/views")
    await staff.get(f"/api/staff/views/{view_id}/complaints")
    await staff.put(
        f"/api/staff/views/{view_id}",
        json={"name": "Flow view renamed", "criteria": {"status": "resolved"}},
    )
    await staff.delete(f"/api/staff/views/{view_id}")
    staff.assert_clean()


async def test_admin_departments_teams_and_roles(staff: Api, world: World) -> None:
    department = await staff.post(
        "/api/staff/departments",
        json={"name": f"قسم جديد {world.tag}", "description": "وصف", "is_active": True},
    )
    department_id = department.json()["id"]
    await staff.get("/api/staff/departments")
    await staff.put(
        f"/api/staff/departments/{department_id}",
        json={"name": f"قسم معدل {world.tag}", "description": "وصف معدل", "is_active": True},
    )
    await staff.patch(
        f"/api/staff/users/{world.agent_id}/department", json={"department_id": department_id}
    )

    team = await staff.post(
        "/api/staff/admin/teams",
        json={
            "name": f"فريق جديد {world.tag}",
            "lead_staff_id": str(world.admin_id),
            "member_ids": [str(world.admin_id), str(world.agent_id)],
        },
    )
    team_id = team.json()["id"]
    # Changing the membership invalidates the sessions of the members (the
    # ownership scope is re-evaluated), so the admin signs in again.
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.put(
        f"/api/staff/admin/teams/{team_id}",
        json={
            "name": f"فريق معدل {world.tag}",
            "member_ids": [str(world.admin_id), str(world.agent_id)],
        },
    )
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.get("/api/staff/admin/teams")

    role = await staff.post(
        "/api/staff/roles",
        json={
            "name": f"دور جديد {world.tag}",
            "description": "وصف الدور",
            "permissions": ["complaints.view", "complaints.edit"],
        },
    )
    role_id = role.json()["id"]
    await staff.get("/api/staff/roles")
    await staff.put(
        f"/api/staff/roles/{role_id}",
        json={
            "name": f"دور معدل {world.tag}",
            "description": "وصف معدل",
            "permissions": ["complaints.view"],
        },
    )
    clone = await staff.post(
        f"/api/staff/roles/{role_id}/clone", json={"name": f"نسخة {world.tag}"}
    )
    assert clone.status_code in (200, 201)
    staff.assert_clean()


async def test_admin_users_and_access(staff: Api, world: World) -> None:
    await staff.get("/api/staff/users")
    await staff.get("/api/staff/admin/access-options")
    await staff.get(f"/api/staff/users/{world.agent_id}/access")
    await staff.get(f"/api/staff/users/{world.agent_id}/access/explain")
    await staff.get(f"/api/staff/users/{world.agent_id}/profile")

    await staff.relogin(world.admin_email, PASSWORD)
    access = await staff.put(
        f"/api/staff/users/{world.agent_id}/access",
        json={
            "role_ids": [str(world.role_id)],
            "allow": [],
            "deny": [],
            "all_companies": False,
            "company_ids": [str(world.company_id)],
            "insurance_types": ["health"],
            "ownership": "all",
            "team_ids": [str(world.team_id)],
        },
    )
    assert access.status_code in (200, 204)
    await staff.relogin(world.admin_email, PASSWORD)

    await staff.patch(f"/api/staff/users/{world.agent_id}/2fa-policy", json={"required": False})
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.patch("/api/staff/settings/staff-2fa", json={"required": False})
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.patch(
        f"/api/staff/users/{world.agent_id}",
        json={"email": f"flow-agent-{world.tag}@example.test", "full_name": "Flow Agent Renamed"},
    )
    staff.assert_clean()


async def test_admin_staff_invitation_round_trip(staff: Api, world: World) -> None:
    """The invite flow must produce a token the invitee can actually accept."""
    department = await staff.post(
        "/api/staff/departments", json={"name": f"قسم الدعوة {world.tag}"}
    )
    department_id = department.json()["id"]
    invitee_email = f"invitee-{world.tag}@example.test"
    await staff.post(
        "/api/staff/users/invitations",
        json={
            "department_id": department_id,
            "email": invitee_email,
            "full_name": "المدعو للاختبار",
        },
    )
    body = next(
        (m.get("body", "") for m in world.delivery.messages if m.get("to") == invitee_email),
        "",
    )
    token = re.search(r"(?:token=|\/invite\/)([A-Za-z0-9_\-]+)", body)
    assert token, f"no invitation token in the delivered mail: {body!r}"

    inspect = await staff.post("/api/auth/staff/invitation/inspect", json={"token": token.group(1)})
    assert inspect.status_code == 200

    accept = await staff.post(
        "/api/auth/staff/invitation/accept",
        json={
            "token": token.group(1),
            "password": PASSWORD,
            "profile": {
                "full_name": "المدعو للاختبار",
                "national_id": world.invitee_national_id,
                "mobile": "+201112223344",
                "address": "12 Nile Street",
                "city": "Giza",
            },
            "avatar_base64": base64.b64encode(_png()).decode(),
        },
        expect=(200, 201),
    )
    assert accept.json()["next_step"] == "sign_in_with_password"
    staff.assert_clean()


async def test_admin_catalog_company_categories_templates(staff: Api, world: World) -> None:
    company = await staff.post(
        "/api/staff/admin/companies",
        json={
            "slug": f"flow-co-{world.tag}",
            "name_en": "Flow Company",
            "name_ar": "شركة فلو",
            "contact_emails": ["ops@flow.example"],
            "supported_types": ["health", "general"],
            "is_active": True,
        },
    )
    company_id = company.json()["id"]
    await staff.get("/api/staff/admin/companies")
    await staff.put(
        f"/api/staff/admin/companies/{company_id}",
        json={
            "slug": f"flow-co-{world.tag}",
            "name_en": "Flow Company Renamed",
            "name_ar": "شركة فلو المحدثة",
            "contact_emails": ["ops@flow.example"],
            "supported_types": ["health", "general", "both"],
            "is_active": True,
        },
    )

    category = await staff.post(
        "/api/staff/admin/categories",
        json={
            "company_id": None,
            "name_en": "Flow Category",
            "name_ar": "تصنيف الاختبار",
            "is_active": True,
        },
    )
    category_id = category.json()["id"]
    await staff.get("/api/staff/admin/categories")
    await staff.put(
        f"/api/staff/admin/categories/{category_id}",
        json={
            "company_id": None,
            "name_en": "Flow Category Renamed",
            "name_ar": "تصنيف الاختبار المحدث",
            "is_active": True,
        },
    )

    template = await staff.post(
        "/api/staff/admin/templates",
        json={
            "company_id": None,
            "title": "Flow template",
            "body_en": "Dear customer, we are following up.",
            "body_ar": "عميلنا العزيز، نتابع مع طلبكم.",
            "is_active": True,
        },
    )
    template_id = template.json()["id"]
    await staff.get("/api/staff/admin/templates")
    await staff.put(
        f"/api/staff/admin/templates/{template_id}",
        json={
            "company_id": None,
            "title": "Flow template renamed",
            "body_en": "Dear customer, we are still following up.",
            "body_ar": "عميلنا العزيز، ما زلنا نتابع طلبكم.",
            "is_active": True,
        },
    )
    staff.assert_clean()


async def test_admin_sla_rules_accept_an_empty_business_calendar(staff: Api, world: World) -> None:
    """Regression: the SPA posts ``business_hours: {}`` for "no calendar"."""
    rule = await staff.post(
        "/api/staff/admin/sla-rules",
        json={
            "company_id": None,
            "insurance_type": "health",
            "priority": "normal",
            "response_minutes": 60,
            "resolution_minutes": 1440,
            "alert_before_minutes": 30,
            "business_hours": {},
            "is_active": True,
        },
    )
    rule_id = rule.json()["id"]
    await staff.get("/api/staff/admin/sla-rules")
    await staff.put(
        f"/api/staff/admin/sla-rules/{rule_id}",
        json={
            "company_id": None,
            "insurance_type": "health",
            "priority": "normal",
            "response_minutes": 30,
            "resolution_minutes": 720,
            "alert_before_minutes": 15,
            "business_hours": {},
            "is_active": True,
        },
    )
    await staff.post(
        "/api/staff/admin/sla-rules/simulate",
        json={
            "company_id": None,
            "insurance_type": "health",
            "priority": "normal",
            "created_at": NOW.isoformat(),
        },
    )
    await staff.post(
        "/api/staff/admin/sla-rules/business-hours/preview",
        json={"business_hours": {}, "minutes": 120, "from_at": NOW.isoformat()},
    )
    staff.assert_clean()


async def test_admin_assignment_rules_require_a_target(staff: Api, world: World) -> None:
    """Regression: a rule without a team or an agent is not a valid rule."""
    await staff.get("/api/staff/admin/assignment-rules")
    await staff.get("/api/staff/admin/automation-options")
    await staff.post(
        "/api/staff/admin/assignment-rules/preview",
        json={
            "company_id": str(world.company_id),
            "insurance_type": "health",
            "priority": "normal",
        },
    )
    rule = await staff.post(
        "/api/staff/admin/assignment-rules",
        json={
            "company_id": None,
            "insurance_type": "health",
            "priority": "normal",
            "target_team_id": str(world.team_id),
            "target_agent_id": None,
            "strategy": "least_workload",
            "sort_order": 10,
            "is_active": True,
        },
    )
    rule_id = rule.json()["id"]
    await staff.put(
        f"/api/staff/admin/assignment-rules/{rule_id}",
        json={
            "company_id": None,
            "insurance_type": "health",
            "priority": "high",
            "target_team_id": None,
            "target_agent_id": str(world.agent_id),
            "strategy": "round_robin",
            "sort_order": 20,
            "is_active": True,
        },
    )
    await staff.delete(f"/api/staff/admin/assignment-rules/{rule_id}")
    staff.assert_clean()


async def test_admin_brand_platform_name_and_logo(staff: Api, world: World) -> None:
    """Regression: the logo must be a same-origin asset path."""
    await staff.put(
        "/api/platform/brand",
        json={"platform_name": "Insurance Flow", "logo_url": "/insurance-logo.svg"},
    )
    await staff.post(
        "/api/platform/brand/logo",
        content=_png(),
        headers={"Content-Type": "image/png"},
    )
    staff.assert_clean()


async def test_admin_integrations_api_keys_and_webhooks(staff: Api, world: World) -> None:
    await staff.get("/api/staff/admin/integration-connections")
    await staff.get("/api/staff/admin/webhooks")
    await staff.get("/api/staff/admin/api-keys")
    await staff.get("/api/staff/admin/integration-jobs/dead-letters")
    await staff.get("/api/staff/admin/duplicates")
    await staff.get("/api/staff/storage")
    await staff.get("/api/staff/customers")
    staff.assert_clean()


async def test_admin_reports_exports_analytics_and_audit(staff: Api, world: World) -> None:
    await staff.get("/api/staff/analytics/overview", params=WINDOW)
    await staff.get("/api/staff/analytics/rollup", params=WINDOW)
    await staff.get("/api/staff/analytics/scorecards", params=WINDOW)
    await staff.get("/api/staff/audit/events")
    await staff.get("/api/staff/audit/access-changes")
    await staff.get("/api/staff/exports")
    await staff.get("/api/staff/notifications")
    staff.assert_clean()


async def test_public_endpoints(staff: Api, world: World) -> None:
    await staff.get("/api/health/live")
    await staff.get("/api/health/ready")
    await staff.get("/api/public/config")
    await staff.get("/api/companies")
    staff.assert_clean()


async def test_guest_tracking(staff: Api, world: World) -> None:
    """A guest with an unknown ticket number must be answered, not 422'd."""
    response = await staff.post(
        "/api/guest/track",
        json={"national_id": world.national_id, "ticket_no": "CMP-2026-000001"},
        expect=(200, 404),
    )
    assert response.status_code != 422
    staff.assert_clean()


async def test_admin_webhooks_and_api_keys(staff: Api, world: World) -> None:
    """Webhooks and API keys are created through the console, not by hand."""
    webhook = await staff.post(
        "/api/staff/admin/webhooks",
        json={
            "url": "https://hooks.example.com/insurance",
            "company_id": str(world.company_id),
            "event_types": ["complaint.status_changed"],
        },
    )
    webhook_id = webhook.json()["id"]
    await staff.get("/api/staff/admin/webhooks")
    await staff.delete(f"/api/staff/admin/webhooks/{webhook_id}")

    key = await staff.post(
        "/api/staff/admin/api-keys",
        json={
            "permissions": ["complaints.view"],
            "all_companies": False,
            "company_ids": [str(world.company_id)],
        },
    )
    key_body = key.json()
    key_id = key_body.get("id")
    assert key_id, f"no api key id in the response: {key_body}"
    await staff.get("/api/staff/admin/api-keys")
    await staff.delete(f"/api/staff/admin/api-keys/{key_id}")
    staff.assert_clean()


async def test_admin_exports_and_report_schedules(staff: Api, world: World) -> None:
    export = await staff.post(
        "/api/staff/exports",
        json={"kind": "complaints", "format": "csv", "filters": {**WINDOW}},
    )
    job_id = export.json()["id"]
    await staff.get("/api/staff/exports")
    await staff.get(f"/api/staff/exports/{job_id}/download", expect=(200, 202, 404, 409))

    schedule = await staff.post(
        "/api/staff/reports/schedules",
        json={
            "name": f"تقرير الاختبار {world.tag}",
            "filters": {"window_days": 7},
            "cron_expression": "0 8 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["reports@example.com"],
            "format": "csv",
            "is_active": True,
        },
    )
    schedule_id = schedule.json()["id"]
    await staff.get("/api/staff/reports/schedules")
    await staff.put(
        f"/api/staff/reports/schedules/{schedule_id}",
        json={
            "name": f"تقرير الاختبار المعدل {world.tag}",
            "filters": {"window_days": 14},
            "cron_expression": "0 9 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["reports@example.com"],
            "format": "xlsx",
            "is_active": True,
        },
    )
    await staff.post(f"/api/staff/reports/schedules/{schedule_id}/run", expect=(200, 202))
    await staff.get(f"/api/staff/reports/schedules/{schedule_id}/runs")
    await staff.delete(f"/api/staff/reports/schedules/{schedule_id}")
    staff.assert_clean()


async def test_staff_privacy_queue(staff: Api, customer: Api, world: World) -> None:
    created = await customer.post(
        "/api/customer/privacy/requests",
        json={"kind": "erasure", "details": "أطلب حذف بياناتي"},
    )
    request_id = created.json()["id"]
    queue = await staff.get("/api/staff/privacy/requests")
    assert any(str(row.get("id")) == request_id for row in queue.json())
    await staff.post(
        f"/api/staff/privacy/requests/{request_id}",
        json={"status": "in_progress", "resolution": "قيد المراجعة"},
    )
    staff.assert_clean()


async def test_staff_outbound_messages(staff: Api, customer: Api, world: World) -> None:
    """The outbound channels are exercised end to end; the provider may refuse."""
    complaint_id = await _create_complaint(customer, world)
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/email",
        json={"locale": "ar", "agent_note": "تم التواصل مع العميل", "attachment_ids": []},
        headers={"Idempotency-Key": str(uuid4())},
        expect=(200, 201, 202, 400, 409, 503),
    )
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/sms",
        json={"body": "تم استلام طلبك وسيتم المتابعة."},
        headers={"Idempotency-Key": str(uuid4())},
        expect=(200, 201, 202, 400, 409, 503),
    )
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/whatsapp",
        json={"body": "تم استلام طلبك وسيتم المتابعة.", "language": "ar"},
        headers={"Idempotency-Key": str(uuid4())},
        expect=(200, 201, 202, 400, 409, 503),
    )
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/ai/analyze",
        json={"locale": "ar"},
        headers={"Idempotency-Key": str(uuid4())},
        expect=(200, 201, 202, 400, 409, 503),
    )
    await staff.post(
        f"/api/staff/complaints/{complaint_id}/voice-followups",
        json={"scheduled_at": (NOW + timedelta(days=1)).isoformat(), "language": "ar"},
        headers={"Idempotency-Key": str(uuid4())},
        expect=(200, 201, 202, 400, 409, 503),
    )
    await staff.get(f"/api/staff/complaints/{complaint_id}/ai/insights")
    await staff.get(f"/api/staff/complaints/{complaint_id}/ai/similar")
    await staff.get(f"/api/staff/complaints/{complaint_id}/inbox")
    staff.assert_clean()


async def test_staff_bulk_actions(staff: Api, customer: Api, world: World) -> None:
    first = await _create_complaint(customer, world)
    second = await _create_complaint(customer, world)
    ids = [first, second]
    await staff.post(
        "/api/staff/complaints/bulk",
        json={
            "action": "assign",
            "complaint_ids": ids,
            "assignment": {"agent_id": str(world.agent_id)},
        },
    )
    await staff.post(
        "/api/staff/complaints/bulk",
        json={"action": "status", "complaint_ids": ids, "status": "in_review"},
    )
    staff.assert_clean()


async def test_staff_merge_and_duplicate_scan(staff: Api, customer: Api, world: World) -> None:
    first = await _create_complaint(customer, world)
    second = await _create_complaint(customer, world)
    await staff.get(f"/api/staff/complaints/{first}/duplicates")
    await staff.post(f"/api/staff/admin/duplicates/scan/{first}", expect=(200, 201, 202, 404))
    await staff.post(
        f"/api/staff/complaints/{second}/merge",
        json={"target_id": first},
        expect=(200, 201, 202, 400, 409),
    )
    await staff.get("/api/staff/admin/duplicates")
    staff.assert_clean()


async def test_admin_user_lifecycle(staff: Api, world: World) -> None:
    await staff.post(f"/api/staff/users/{world.agent_id}/deactivate", expect=(200, 204))
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.post(f"/api/staff/users/{world.agent_id}/reactivate", expect=(200, 204))
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.post(f"/api/staff/users/{world.agent_id}/force-logout", expect=(200, 204))
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.post(f"/api/staff/users/{world.agent_id}/password-reset", expect=(200, 202, 204))
    await staff.relogin(world.admin_email, PASSWORD)
    await staff.get(f"/api/staff/users/{world.agent_id}/avatar", expect=(200, 404))
    staff.assert_clean()


async def test_admin_integration_connections(staff: Api, world: World) -> None:
    """A company-scoped provider is saved for one company, never platform wide."""
    await staff.put(
        "/api/staff/admin/integration-connections",
        json={
            "company_id": str(world.company_id),
            "provider": "smtp",
            "enabled": True,
            "clear_credentials": False,
            "credentials": {
                "host": "smtp.example.com",
                "port": "587",
                "start_tls": "true",
                "use_tls": "false",
                "username": "claims@example.com",
                "password": "not-a-real-secret",
                "from_email": "claims@example.com",
            },
        },
    )
    await staff.get("/api/staff/admin/integration-connections")
    await staff.post(
        "/api/staff/admin/integration-connections/test",
        json={"company_id": str(world.company_id), "provider": "smtp"},
        expect=(200, 201, 202, 400, 409, 503),
    )
    await staff.put(
        "/api/staff/admin/integration-connections",
        json={
            "company_id": str(world.company_id),
            "provider": "smtp",
            "enabled": False,
            "clear_credentials": True,
        },
    )
    staff.assert_clean()


async def test_staff_2fa_enrollment(staff: Api, world: World) -> None:
    started = await staff.post("/api/me/2fa/enroll/start", expect=(200, 201, 202))
    body = started.json()
    assert body.get("provisioning_uri"), f"no provisioning uri in the response: {body}"
    # The confirmation needs a real TOTP code; an invalid one must be a clean
    # rejection, never an unprocessable payload.
    await staff.post(
        "/api/me/2fa/enroll/confirm",
        json={"code": "000000"},
        expect=(200, 201, 400, 401, 409, 422),
    )
    staff.assert_clean()


async def test_staff_notifications_and_canned_replies(staff: Api, world: World) -> None:
    await staff.get("/api/staff/notifications")
    await staff.get("/api/staff/complaints/filter-options")
    await staff.get("/api/staff/customers")
    await staff.get("/api/staff/storage")
    staff.assert_clean()
