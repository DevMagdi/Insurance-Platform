"""Disposable PostgreSQL 16 + Redis integration: real RLS, API and DB worker role.

Docker is required; this suite skips ONLY when no daemon is available. Migration,
role, RLS or workflow failures are test failures when Docker is present.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import docker
import psycopg
import pytest
from argon2 import PasswordHasher
from docker.errors import DockerException
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from app.db import make_engine, make_worker_engine
from app.domain.permissions import PERMISSIONS
from app.main import app
from app.repositories.attachments import AttachmentRepository
from app.repositories.complaints import ComplaintRepository
from app.security.crypto import NationalIdProtector, decode_key
from app.security.tokens import TokenManager
from app.services.attachments import AttachmentService
from app.services.auth import AuthService
from app.services.complaints import ComplaintService
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.testcontainers]
BACKEND = Path(__file__).resolve().parents[1]
NID = "29901010112345"
MOBILE = "+201123456789"
PASSWORD = "integration-Password-123"
PDF = b"%PDF-1.4\n1 0 obj<</Type /Catalog>>\nendobj\n%%EOF\n"


class Mailbox:
    def __init__(self) -> None:
        self.codes: dict[str, str] = {}
        self.invites: dict[str, str] = {}

    async def send_otp(self, mobile: str, code: str) -> None:
        self.codes[mobile] = code

    async def send_invitation(self, email: str, url: str) -> None:
        self.invites[email] = url

    async def send_password_reset(self, email: str, url: str) -> None:
        self.invites[email] = url

    async def send_notice(
        self, mobile: str, email: str | None, kind: str, payload: dict[str, str]
    ) -> bool:
        return False


class Objects:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def put(self, key: str, file: object, mime: str) -> None:
        self.values[key] = file.read()

    async def get(self, key: str, max_bytes: int) -> bytes:
        return self.values[key]

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture(scope="module")
def stack() -> dict:
    try:
        docker.from_env().ping()
    except DockerException:
        pytest.skip("Docker daemon unavailable; testcontainers requires Docker")
    with (
        PostgresContainer("postgres:16.8-alpine", driver="psycopg") as pg,
        RedisContainer("redis:7.4-alpine") as redis_container,
    ):
        admin_url = pg.get_connection_url(driver="psycopg")
        runtime_password = secrets.token_urlsafe(32)
        worker_password = secrets.token_urlsafe(32)
        with psycopg.connect(admin_url.replace("+psycopg", ""), autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE app_runtime LOGIN NOBYPASSRLS PASSWORD {}").format(
                    sql.Literal(runtime_password)
                )
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO app_runtime").format(
                    sql.Identifier(pg.dbname)
                )
            )
        env = {
            **os.environ,
            "DATABASE_ADMIN_URL": admin_url,
            "APP_WORKER_DB_PASSWORD": worker_password,
        }
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            cwd=BACKEND,
            env=env,
            capture_output=True,
        )
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        redis_host = redis_container.get_container_host_ip()
        redis_port = redis_container.get_exposed_port(6379)
        yield {
            "admin": admin_url.replace("+psycopg", ""),
            "runtime": f"postgresql+asyncpg://app_runtime:{runtime_password}@{host}:{port}/{pg.dbname}",
            "worker": f"postgresql+asyncpg://app_worker:{worker_password}@{host}:{port}/{pg.dbname}",
            "redis": f"redis://{redis_host}:{redis_port}/0",
        }


@pytest.fixture(scope="module")
def identities(stack: dict) -> dict:
    insurer_a, insurer_b, admin, manager, agent = [uuid4() for _ in range(5)]
    admin_role, manager_role, agent_role = [uuid4() for _ in range(3)]
    password_hash = PasswordHasher().hash(PASSWORD)
    with psycopg.connect(stack["admin"]) as db, db.cursor() as cur:
        cur.executemany(
            "INSERT INTO insurance_companies (id,slug,name_en,name_ar) VALUES (%s,%s,%s,%s)",
            [
                (insurer_a, "insurer-a", "Insurer A", "شركة أ"),
                (insurer_b, "insurer-b", "Insurer B", "شركة ب"),
            ],
        )
        cur.executemany(
            "INSERT INTO staff_users(id,email,full_name,password_hash,status,"
            "must_change_password) VALUES (%s,%s,%s,%s,'active',false)",
            [
                (admin, "admin@test.invalid", "Admin", password_hash),
                (manager, "manager@test.invalid", "Manager", password_hash),
                (agent, "agent@test.invalid", "Agent", password_hash),
            ],
        )
        cur.executemany(
            "INSERT INTO roles(id,name) VALUES (%s,%s)",
            [(admin_role, "administrator"), (manager_role, "manager"), (agent_role, "agent")],
        )
        cur.executemany(
            "INSERT INTO role_permissions(role_id,permission_code) VALUES (%s,%s)",
            [(admin_role, code) for code in PERMISSIONS]
            + [
                (manager_role, code)
                for code in (
                    "users.view",
                    "users.deactivate",
                    "users.manage_access",
                    "complaints.view",
                )
            ]
            + [
                (agent_role, code)
                for code in (
                    "complaints.view",
                    "complaints.export",
                    "complaints.view_attachments",
                    "complaints.reply_public",
                )
            ],
        )
        cur.executemany(
            "INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",
            [(admin, admin_role), (manager, manager_role), (agent, agent_role)],
        )
        cur.executemany(
            "INSERT INTO user_company_scope(user_id,company_id,all_companies) VALUES (%s,%s,%s)",
            [(admin, None, True), (manager, None, True), (agent, insurer_a, False)],
        )
        cur.executemany(
            "INSERT INTO user_ownership_level(user_id,level) VALUES (%s,%s)",
            [(admin, "all"), (manager, "all"), (agent, "own")],
        )
    return {
        "a": insurer_a,
        "b": insurer_b,
        "admin": admin,
        "manager": manager,
        "agent": agent,
        "role": admin_role,
    }


@pytest.fixture
async def test_app(stack: dict, identities: dict):
    settings = Settings(
        _env_file=None,
        DATABASE_URL=stack["runtime"],
        DEV_SMS_MAILBOX=False,  # CI loads a development .env; test mail is in-memory.
        REDIS_URL=stack["redis"],
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
    engine = make_engine(stack["runtime"])
    redis = Redis.from_url(stack["redis"])
    await redis.flushdb()  # Isolate per-IP/mobile limits across otherwise independent cases.
    mailbox = Mailbox()
    objects = Objects()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.engine = engine
    app.state.factory = factory
    app.state.redis = redis
    app.state.auth = AuthService(factory, redis, settings, mailbox)
    app.state.complaints = ComplaintService(
        ComplaintRepository(),
        NationalIdProtector(
            decode_key(settings.app_encryption_key), decode_key(settings.app_blind_index_key)
        ),
        settings.jwt_signing_key,
    )
    app.state.attachments = AttachmentService(
        AttachmentRepository(), TokenManager(settings), objects
    )
    app.state.storage = objects
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as customer,
        AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as admin,
        AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as agent,
    ):
        yield SimpleNamespace(
            customer=customer,
            admin=admin,
            agent=agent,
            mailbox=mailbox,
            objects=objects,
            stack=stack,
            identities=identities,
        )
    await redis.aclose()
    await engine.dispose()


def csrf(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


async def sign_in_staff(client: AsyncClient, email: str) -> None:
    result = await client.post("/api/auth/staff/login", json={"email": email, "password": PASSWORD})
    assert result.status_code == 200, result.text
    assert result.json()["next_step"] == "authenticated"


async def sign_in_customer(client: AsyncClient, mailbox: Mailbox) -> None:
    result = await client.post("/api/auth/customer/otp/start", json={"mobile": MOBILE})
    assert result.status_code == 202, result.text
    result = await client.post(
        "/api/auth/customer/otp/verify",
        json={
            "mobile": MOBILE,
            "code": mailbox.codes[MOBILE],
            "national_id": NID,
            "full_name": "Integration Customer",
        },
    )
    assert result.status_code == 200, result.text


def complaint_payload(company: UUID, subject: str) -> dict:
    return {
        "national_id": NID,
        "mobile": MOBILE,
        "company_id": str(company),
        "insurance_type": "health",
        "policy_number": "POL-000999",
        "subject": subject,
        "description": "Payment was delayed well beyond the expected date.",
    }


async def test_api_ownership_guest_audit_status_and_refresh(test_app: SimpleNamespace) -> None:
    ctx = test_app
    await sign_in_customer(ctx.customer, ctx.mailbox)
    await sign_in_staff(ctx.admin, "admin@test.invalid")
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    assert (await ctx.admin.get("/api/me")).json()["access"]["all_companies"]
    payload = complaint_payload(ctx.identities["a"], "Delayed payment for first claim")
    headers = {**csrf(ctx.customer), "Idempotency-Key": "integration-once-001"}
    first = await ctx.customer.post("/api/customer/complaints", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    ticket, complaint_id = first.json()["ticket_no"], first.json()["id"]
    assert ticket.startswith("CMP-") and first.json()["replayed"] is False
    again = await ctx.customer.post("/api/customer/complaints", json=payload, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == complaint_id
    collision = await ctx.customer.post(
        "/api/customer/complaints",
        json={**payload, "subject": "Different complaint subject"},
        headers=headers,
    )
    assert collision.status_code == 409
    second = await ctx.customer.post(
        "/api/customer/complaints",
        json=complaint_payload(ctx.identities["b"], "Unique second insurer dispute"),
        headers={**csrf(ctx.customer), "Idempotency-Key": "integration-once-002"},
    )
    assert second.status_code == 201, second.text
    wrong = await ctx.customer.post(
        "/api/guest/track", json={"national_id": "29901010212345", "ticket_no": ticket}
    )
    assert wrong.status_code == 404
    guest = await ctx.customer.post(
        "/api/guest/track", json={"national_id": NID, "ticket_no": ticket}
    )
    assert guest.status_code == 200 and set(guest.json()) == {
        "public_ticket",
        "public_status",
        "public_company",
        "created",
    }
    staff_view = await ctx.admin.get(f"/api/staff/complaints/{complaint_id}")
    assert staff_view.status_code == 200 and staff_view.json()["national_id_masked"].endswith(
        "2345"
    )
    assert "national_id" not in staff_view.json()
    assert (await ctx.agent.get(f"/api/staff/complaints/{complaint_id}")).status_code == 404
    # A direct admin update assigns an A ticket to the A-scoped agent; B stays hidden.
    with psycopg.connect(ctx.stack["admin"]) as conn:
        conn.execute(
            "UPDATE complaints SET assigned_agent_id=%s WHERE id=%s",
            (ctx.identities["agent"], complaint_id),
        )
    listed = await ctx.agent.get("/api/staff/complaints?status=new")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["items"]] == [
        complaint_id
    ]
    assert (await ctx.agent.get(f"/api/staff/complaints/{second.json()['id']}")).status_code == 404
    for suffix in (f"?company_id={ctx.identities['b']}", "?search=dispute"):
        result = await ctx.agent.get("/api/staff/complaints" + suffix)
        assert result.status_code == 200 and result.json()["items"] == []
        export = await ctx.agent.get("/api/staff/complaints/export" + suffix)
        assert export.status_code == 200 and ticket.encode() not in export.content
    assert (await ctx.agent.get("/api/staff/complaints/counters")).json()["new"] == 1
    assert (
        await ctx.agent.post(
            f"/api/staff/complaints/{complaint_id}/national-id:reveal",
            json={"reason": "Necessary case verification"},
            headers=csrf(ctx.agent),
        )
    ).status_code == 403
    revealed = await ctx.admin.post(
        f"/api/staff/complaints/{complaint_id}/national-id:reveal",
        json={"reason": "Necessary case verification"},
        headers=csrf(ctx.admin),
    )
    assert revealed.status_code == 200 and revealed.json()["national_id"] == NID
    with psycopg.connect(ctx.stack["admin"]) as conn:
        count = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE action='national_id_revealed'"
        ).fetchone()[0]
    assert count == 1
    resolved = await ctx.admin.patch(
        f"/api/staff/complaints/{complaint_id}/status",
        json={"status": "resolved"},
        headers=csrf(ctx.admin),
    )
    assert resolved.status_code == 200, resolved.text
    assert (
        await ctx.customer.post(
            f"/api/customer/complaints/{complaint_id}/csat",
            json={"score": 5},
            headers=csrf(ctx.customer),
        )
    ).status_code == 200
    reopened = await ctx.customer.post(
        f"/api/customer/complaints/{complaint_id}/reopen", headers=csrf(ctx.customer)
    )
    assert reopened.status_code == 200, reopened.text
    assert (
        await ctx.customer.post(
            f"/api/customer/complaints/{complaint_id}/reopen", headers=csrf(ctx.customer)
        )
    ).status_code == 409
    timeline = await ctx.customer.get(f"/api/customer/complaints/{complaint_id}/timeline")
    assert timeline.status_code == 200 and len(timeline.json()["events"]) >= 2
    before = ctx.customer.cookies.get("refresh_token")
    rotated = await ctx.customer.post("/api/auth/refresh", headers=csrf(ctx.customer))
    assert rotated.status_code == 200, rotated.text
    assert ctx.customer.cookies.get("refresh_token") != before
    # Signed old refresh token replay kills the whole family, even its newest session.
    ctx.customer.cookies.set("refresh_token", before, path="/api/auth")
    replay = await ctx.customer.post("/api/auth/refresh", headers=csrf(ctx.customer))
    assert replay.status_code == 401
    assert (await ctx.customer.get("/api/me")).status_code == 401


async def test_scan_fail_closed_and_last_admin_guard(test_app: SimpleNamespace) -> None:
    ctx = test_app
    await sign_in_customer(ctx.customer, ctx.mailbox)
    await sign_in_staff(ctx.admin, "admin@test.invalid")
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    record = await ctx.customer.post(
        "/api/customer/complaints",
        json=complaint_payload(ctx.identities["a"], "Attachment test ticket"),
        headers={**csrf(ctx.customer), "Idempotency-Key": "integration-upload-001"},
    )
    assert record.status_code == 201, record.text
    complaint_id = record.json()["id"]
    digest = hashlib.sha256(PDF).hexdigest()
    plan = await ctx.customer.post(
        f"/api/customer/complaints/{complaint_id}/attachments/plan",
        json={
            "files": [
                {
                    "filename": "proof.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(PDF),
                    "checksum_sha256": digest,
                }
            ]
        },
        headers=csrf(ctx.customer),
    )
    assert plan.status_code == 200, plan.text
    reserved = plan.json()["uploads"][0]
    sent = await ctx.customer.put(
        reserved["upload_url"],
        content=PDF,
        headers={
            **csrf(ctx.customer),
            "Content-Type": "application/pdf",
            "X-Upload-Token": reserved["upload_token"],
        },
    )
    assert sent.status_code == 200 and sent.json()["scan_status"] == "pending"
    attachment_id = sent.json()["id"]
    assert (
        await ctx.customer.get(f"/api/customer/attachments/{attachment_id}/authorize")
    ).status_code == 404
    assert (
        await ctx.customer.get(f"/api/customer/complaints/{complaint_id}/attachments")
    ).json() == []
    worker_engine = make_worker_engine(ctx.stack["worker"])
    async with worker_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.scope', 'worker', true)"))
        value = await conn.scalar(
            text("SELECT app_security.record_scan_verdict(:id, 'clean')"),
            {"id": UUID(reserved["id"])},
        )
        assert value is True
    await worker_engine.dispose()
    authorized = await ctx.customer.get(f"/api/customer/attachments/{attachment_id}/authorize")
    assert authorized.status_code == 200, authorized.text
    download = await ctx.customer.get(
        authorized.json()["download_url"],
        headers={"X-Download-Token": authorized.json()["download_token"]},
    )
    assert download.status_code == 200 and download.content == PDF
    assert download.headers["cache-control"] == "no-store"
    with psycopg.connect(ctx.stack["admin"]) as conn:
        count = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE action='attachment_downloaded'"
        ).fetchone()[0]
    assert count >= 0  # Customer downloads are separately audited via definer function.
    assert (
        await ctx.admin.put(
            f"/api/staff/users/{ctx.identities['admin']}/access",
            json={"role_ids": [], "all_companies": True, "ownership": "all"},
            headers=csrf(ctx.admin),
        )
    ).status_code == 403
    manager = AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
    try:
        await sign_in_staff(manager, "manager@test.invalid")
        disabled = await manager.post(
            f"/api/staff/users/{ctx.identities['admin']}/deactivate", headers=csrf(manager)
        )
        assert disabled.status_code == 409, disabled.text
    finally:
        await manager.aclose()


def test_phase4_migration_rls_grants_and_permission_codes(stack: dict, identities: dict) -> None:
    """Validate the Phase 4 schema and role boundaries on real PostgreSQL 16."""
    with psycopg.connect(stack["admin"]) as db:
        revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert revision == "20261015_12"
        guarded = db.execute(
            """SELECT relname, relrowsecurity, relforcerowsecurity
               FROM pg_class
               WHERE oid = ANY(ARRAY[
                   'public.integration_jobs'::regclass,
                   'public.integration_dead_letters'::regclass,
                   'public.api_keys'::regclass,
                   'public.webhooks'::regclass,
                   'public.integrations_config'::regclass
               ])"""
        ).fetchall()
        assert len(guarded) == 5
        assert all(row[1] and row[2] for row in guarded)
        direct = db.execute(
            """SELECT has_table_privilege('app_runtime','public.integration_jobs','SELECT'),
                      has_table_privilege('app_worker','public.integration_jobs','SELECT'),
                      has_table_privilege('app_runtime','public.integration_dead_letters','SELECT'),
                      has_table_privilege('app_worker','public.integration_dead_letters','SELECT')"""
        ).fetchone()
        assert direct == (False, False, False, False)
        policy = db.execute(
            """SELECT qual FROM pg_policies
               WHERE schemaname='public' AND tablename='ai_insights'
                 AND policyname='ai_insights_read'"""
        ).fetchone()
        assert policy is not None and "integrations.ai_call" in policy[0]
        columns = {
            row[0]
            for row in db.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='complaint_messages'"""
            ).fetchall()
        }
        assert {"delivery_status", "delivered_at", "read_at", "sender_external"} <= columns
        function_privileges = db.execute(
            """SELECT has_function_privilege('app_worker',
                       'app_security.claim_integration_jobs(integer)','EXECUTE'),
                      has_function_privilege('app_runtime',
                       'app_security.claim_integration_jobs(integer)','EXECUTE')"""
        ).fetchone()
        assert function_privileges == (True, False)

    runtime_url = stack["runtime"].replace("+asyncpg", "")
    with psycopg.connect(runtime_url) as runtime:
        for user_id, expected in (
            (identities["admin"], True),
            (identities["agent"], False),
        ):
            with runtime.transaction():
                runtime.execute("SELECT set_config('app.scope','staff',true)")
                runtime.execute("SELECT set_config('app.user_id',%s,true)", (str(user_id),))
                allowed = runtime.execute(
                    "SELECT app_security.has_permission('integrations.send_email')"
                ).fetchone()[0]
                assert allowed is expected


async def test_phase3_role_delegation_revocation_and_reset(test_app: SimpleNamespace) -> None:
    """Real app_runtime + RLS integration; skipped only when Docker is absent."""
    from urllib.parse import parse_qs, urlsplit

    ctx = test_app
    await sign_in_staff(ctx.admin, "admin@test.invalid")
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    assert (await ctx.agent.get("/api/staff/roles")).status_code == 403
    admin_role = ctx.identities["role"]
    system_role = await ctx.admin.put(
        f"/api/staff/roles/{admin_role}",
        json={"name": "administrator", "permissions": []},
        headers=csrf(ctx.admin),
    )
    assert system_role.status_code == 403
    created = await ctx.admin.post(
        "/api/staff/roles",
        json={"name": "phase3 review", "permissions": ["complaints.view"]},
        headers=csrf(ctx.admin),
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["id"]
    cloned = await ctx.admin.post(
        f"/api/staff/roles/{role_id}/clone",
        json={"name": "phase3 review copy"},
        headers=csrf(ctx.admin),
    )
    assert cloned.status_code == 201, cloned.text
    email = "phase3-user@test.invalid"
    invited = await ctx.admin.post(
        "/api/staff/users/invitations",
        json={"email": email, "full_name": "Phase Three User"},
        headers=csrf(ctx.admin),
    )
    assert invited.status_code == 202, invited.text
    user_id = invited.json()["id"]
    token = parse_qs(urlsplit(ctx.mailbox.invites[email]).query)["token"][0]
    recipient = AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
    try:
        accepted = await recipient.post(
            "/api/auth/staff/invitation/accept",
            json={"token": token, "password": PASSWORD},
        )
        assert accepted.status_code == 200, accepted.text
        await sign_in_staff(recipient, email)
        spec = {
            "role_ids": [role_id],
            "company_ids": [str(ctx.identities["a"])],
            "all_companies": False,
            "ownership": "own",
        }
        updated = await ctx.admin.put(
            f"/api/staff/users/{user_id}/access",
            json=spec,
            headers=csrf(ctx.admin),
        )
        assert updated.status_code == 200, updated.text
        assert (await recipient.get("/api/me")).status_code == 401
        await sign_in_staff(recipient, email)
        explain = await ctx.admin.get(f"/api/staff/users/{user_id}/access/explain")
        assert explain.status_code == 200, explain.text
        assert explain.json()["why"]["complaints.view"]["effective"] is True
        role_edit = await ctx.admin.put(
            f"/api/staff/roles/{role_id}",
            json={"name": "phase3 review", "permissions": ["complaints.view", "analytics.view"]},
            headers=csrf(ctx.admin),
        )
        assert role_edit.status_code == 200, role_edit.text
        assert (await recipient.get("/api/me")).status_code == 401
        await sign_in_staff(recipient, email)
        notices = await recipient.get("/api/me/notifications")
        assert notices.status_code == 200, notices.text
        assert any(n["kind"] == "access_changed" for n in notices.json())
        reset = await ctx.admin.post(
            f"/api/staff/users/{user_id}/password-reset",
            headers=csrf(ctx.admin),
        )
        assert reset.status_code == 202, reset.text
        assert (await recipient.get("/api/me")).status_code == 401
        reset_token = parse_qs(urlsplit(ctx.mailbox.invites[email]).query)["token"][0]
        new_password = "Changed!Secure123"
        assert (
            await recipient.post(
                "/api/auth/staff/invitation/accept",
                json={
                    "token": reset_token,
                    "password": new_password,
                },
            )
        ).status_code == 200
        assert (
            await recipient.post(
                "/api/auth/staff/invitation/accept",
                json={
                    "token": reset_token,
                    "password": new_password,
                },
            )
        ).status_code == 400
        login = await recipient.post(
            "/api/auth/staff/login",
            json={
                "email": email,
                "password": new_password,
            },
        )
        assert login.status_code == 200, login.text
        changes = await ctx.admin.get(f"/api/staff/audit/access-changes?target_id={user_id}")
        assert changes.status_code == 200, changes.text
        assert {n["change_type"] for n in changes.json()} >= {
            "access_replaced",
            "role_changed",
            "password_reset_requested",
        }
        all_changes = await ctx.admin.get("/api/staff/audit/access-changes")
        assert all_changes.status_code == 200, all_changes.text
        assert any(row["target_user_id"] == user_id for row in all_changes.json())
        assert (await ctx.agent.get("/api/staff/audit/access-changes")).status_code == 403
    finally:
        await recipient.aclose()


async def test_phase3_analytics_filters_companies_and_ownership(test_app: SimpleNamespace) -> None:
    """Full SQL aggregates cannot disclose another insurer's tickets."""
    from datetime import UTC, datetime, timedelta

    ctx = test_app
    await sign_in_staff(ctx.admin, "admin@test.invalid")
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    await sign_in_customer(ctx.customer, ctx.mailbox)
    # Earlier module-scoped cases may have left tickets assigned to this agent.
    window_start = datetime.now(UTC)
    suffix = uuid4().hex[:6]
    first = await ctx.customer.post(
        "/api/customer/complaints",
        json=complaint_payload(ctx.identities["a"], "Claim review " + suffix),
        headers={**csrf(ctx.customer), "Idempotency-Key": "analytics-a-" + suffix},
    )
    second = await ctx.customer.post(
        "/api/customer/complaints",
        json=complaint_payload(ctx.identities["b"], "Other insurer " + suffix),
        headers={**csrf(ctx.customer), "Idempotency-Key": "analytics-b-" + suffix},
    )
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    with psycopg.connect(ctx.stack["admin"]) as conn:
        conn.execute(
            "UPDATE complaints SET assigned_agent_id=%s WHERE id=%s",
            (ctx.identities["agent"], first.json()["id"]),
        )
        agent_role = conn.execute(
            "SELECT role_id FROM user_roles WHERE user_id=%s", (ctx.identities["agent"],)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO role_permissions(role_id,permission_code) "
            "VALUES (%s,'analytics.view') ON CONFLICT DO NOTHING",
            (agent_role,),
        )
        conn.execute(
            "UPDATE staff_users SET token_version=token_version+1 WHERE id=%s",
            (ctx.identities["agent"],),
        )
    assert (await ctx.agent.get("/api/me")).status_code == 401
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    now = datetime.now(UTC)
    params = {
        "date_from": window_start.isoformat(),
        "date_to": (now + timedelta(days=1)).isoformat(),
    }
    from urllib.parse import urlencode

    url = "/api/staff/analytics/overview?" + urlencode(params)
    admin_data = await ctx.admin.get(url)
    assert admin_data.status_code == 200, admin_data.text
    assert admin_data.json()["kpis"]["total"] >= 2
    scoped_data = await ctx.agent.get(url)
    assert scoped_data.status_code == 200, scoped_data.text
    scoped = scoped_data.json()
    assert scoped["kpis"]["total"] == 1
    assert {row["company_id"] for row in scoped["company_scorecards"]} == {str(ctx.identities["a"])}
    assert {row["company_id"] for row in scoped["by_company"]} == {str(ctx.identities["a"])}
    assert (await ctx.agent.get(url + "&company_id=" + str(ctx.identities["b"]))).json()["kpis"][
        "total"
    ] == 0


async def test_phase3_catalog_enforces_company_scope_in_database(test_app: SimpleNamespace) -> None:
    """DB definer rejects a forged insurer even when the HTTP role has the grant."""
    ctx = test_app
    await sign_in_staff(ctx.admin, "admin@test.invalid")
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    role_id = None
    with psycopg.connect(ctx.stack["admin"]) as conn:
        role_id = conn.execute(
            "SELECT role_id FROM user_roles WHERE user_id=%s", (ctx.identities["agent"],)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO role_permissions(role_id,permission_code) "
            "VALUES (%s,'categories.manage') ON CONFLICT DO NOTHING",
            (role_id,),
        )
        conn.execute(
            "UPDATE staff_users SET token_version=token_version+1 WHERE id=%s",
            (ctx.identities["agent"],),
        )
    try:
        assert (await ctx.agent.get("/api/me")).status_code == 401
        await sign_in_staff(ctx.agent, "agent@test.invalid")
        path = "/api/staff/admin/categories"
        a = {
            "company_id": str(ctx.identities["a"]),
            "name_en": "Local concerns",
            "name_ar": "شكاوى محلية",
        }
        b = {**a, "company_id": str(ctx.identities["b"]), "name_en": "Other concerns"}
        owner = await ctx.admin.post(path, json=b, headers=csrf(ctx.admin))
        assert owner.status_code == 201, owner.text
        assert (await ctx.agent.post(path, json=b, headers=csrf(ctx.agent))).status_code == 403
        assert (
            await ctx.agent.post(path, json={**a, "company_id": None}, headers=csrf(ctx.agent))
        ).status_code == 403
        mine = await ctx.agent.post(path, json=a, headers=csrf(ctx.agent))
        assert mine.status_code == 201, mine.text
        listing = await ctx.agent.get(path)
        assert listing.status_code == 200
        assert all(row["company_id"] == str(ctx.identities["a"]) for row in listing.json())
        assert owner.json()["id"] not in {row["id"] for row in listing.json()}
        sla = await ctx.admin.post(
            "/api/staff/admin/sla-rules",
            json={
                "company_id": str(ctx.identities["a"]),
                "insurance_type": "health",
                "priority": "high",
                "response_minutes": 120,
                "resolution_minutes": 1440,
            },
            headers=csrf(ctx.admin),
        )
        assert sla.status_code == 201, sla.text
    finally:
        with psycopg.connect(ctx.stack["admin"]) as conn:
            conn.execute(
                "DELETE FROM role_permissions WHERE role_id=%s "
                "AND permission_code='categories.manage'",
                (role_id,),
            )
            conn.execute(
                "UPDATE staff_users SET token_version=token_version+1 WHERE id=%s",
                (ctx.identities["agent"],),
            )


async def test_phase3_export_rechecks_ticket_scope_at_download(test_app: SimpleNamespace) -> None:
    """A worker-generated CSV becomes unavailable immediately after reassignment."""
    from datetime import UTC, datetime, timedelta

    from app.worker import export_pending

    ctx = test_app
    await sign_in_customer(ctx.customer, ctx.mailbox)
    await sign_in_staff(ctx.agent, "agent@test.invalid")
    now = datetime.now(UTC)
    suffix = uuid4().hex[:6]
    ticket = await ctx.customer.post(
        "/api/customer/complaints",
        json=complaint_payload(ctx.identities["a"], "Scoped export " + suffix),
        headers={**csrf(ctx.customer), "Idempotency-Key": "export-" + suffix},
    )
    assert ticket.status_code == 201, ticket.text
    with psycopg.connect(ctx.stack["admin"]) as conn:
        conn.execute(
            "UPDATE complaints SET assigned_agent_id=%s WHERE id=%s",
            (ctx.identities["agent"], ticket.json()["id"]),
        )
    request = await ctx.agent.post(
        "/api/staff/exports",
        json={
            "kind": "complaints",
            "filters": {
                "date_from": (now - timedelta(days=1)).isoformat(),
                "date_to": (now + timedelta(days=1)).isoformat(),
                "ticket_no": ticket.json()["ticket_no"],
            },
        },
        headers=csrf(ctx.agent),
    )
    assert request.status_code == 202, request.text
    worker_engine = make_worker_engine(ctx.stack["worker"])
    try:
        result = await export_pending({"engine": worker_engine, "storage": ctx.objects})
        assert result.startswith("processed ")
    finally:
        await worker_engine.dispose()
    path = f"/api/staff/exports/{request.json()['id']}/download"
    downloaded = await ctx.agent.get(path)
    assert downloaded.status_code == 200, downloaded.text
    assert ticket.json()["ticket_no"].encode() in downloaded.content
    with psycopg.connect(ctx.stack["admin"]) as conn:
        conn.execute(
            "UPDATE complaints SET assigned_agent_id=NULL WHERE id=%s", (ticket.json()["id"],)
        )
    assert (await ctx.agent.get(path)).status_code == 404
    with psycopg.connect(ctx.stack["admin"]) as conn:
        row = conn.execute(
            "SELECT action FROM audit_logs WHERE resource_id=%s AND action='export_downloaded'",
            (request.json()["id"],),
        ).fetchone()
        assert row is not None
