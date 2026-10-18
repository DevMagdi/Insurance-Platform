"""Real PostgreSQL + real SMTP socket + production ASGI composition.

This verifies SMTP acceptance of the exact Gmail/company recipient envelope at a
LOCAL server. It deliberately does not claim delivery to an external inbox.
Requires disposable TEST_DATABASE_ADMIN_URL and TEST_DIRECT_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
from email import message_from_bytes
from email.policy import default
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import fakeredis.aioredis
import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import Actor, purpose_transaction, scoped_transaction
from app.main import app, install_state
from app.security.blobs import AVATAR
from app.services.integrations import config_aad
from app.settings import Settings

pytestmark = pytest.mark.integration
PASSWORD = "Cedar-River-Garden-49"
PROFILE = {
    "full_name": "Mona Ahmed Ali",
    "national_id": "29901010112345",
    "mobile": "+201123456789",
    "address": "12 Nile Street, Dokki",
    "city": "Giza",
    "job_title": "Claims specialist",
    "emergency_phone": "+201012345678",
}


def photo() -> str:
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
async def world():
    admin_url = os.getenv("TEST_DATABASE_ADMIN_URL", "").replace(
        "postgresql+psycopg://", "postgresql://"
    )
    runtime = os.getenv("TEST_DIRECT_DATABASE_URL", "")
    if not admin_url or not runtime:
        pytest.skip("Set disposable TEST_DATABASE_ADMIN_URL and TEST_DIRECT_DATABASE_URL")
    smtp = {"messages": [], "recipients": [], "reject": False}

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"220 local.test ESMTP\r\n")
            await writer.drain()
            while command := await reader.readline():
                verb = command.split(b" ", 1)[0].strip().upper()
                if verb in {b"EHLO", b"HELO"}:
                    writer.write(b"250-local.test\r\n250 SIZE 1048576\r\n")
                elif verb == b"RCPT":
                    if smtp["reject"]:
                        writer.write(b"550 recipient rejected\r\n")
                    else:
                        smtp["recipients"].append(command.decode().strip())
                        writer.write(b"250 OK\r\n")
                elif verb == b"DATA":
                    writer.write(b"354 send data\r\n")
                    await writer.drain()
                    body = bytearray()
                    while (line := await reader.readline()) not in {b".\r\n", b""}:
                        body.extend(line[1:] if line.startswith(b"..") else line)
                    smtp["messages"].append(message_from_bytes(bytes(body), policy=default))
                    writer.write(b"250 accepted by local SMTP\r\n")
                elif verb == b"QUIT":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 OK\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = Settings(
        _env_file=None,
        DATABASE_URL=runtime,
        DEV_SMS_MAILBOX=False,  # CI exports development-only mailbox settings.
        TURNSTILE_SITE_KEY=None,
        TURNSTILE_SECRET_KEY=None,
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
        PUBLIC_APP_URL="https://testserver",
    )
    old_state = app.state._state.copy()
    redis = fakeredis.aioredis.FakeRedis()
    close = await install_state(app, config, redis=redis)
    staff_id, role_id = uuid4(), uuid4()
    email = f"onboard-admin-{uuid4().hex}@example.test"
    hashed = await app.state.auth.passwords.hash(PASSWORD)
    credentials = {
        "host": "127.0.0.1",
        "port": str(port),
        "from_email": "admin@company.com",
        "start_tls": "false",
        "sandbox_recipient": "sandbox@example.test",
    }
    cipher = app.state.auth.staff_mail.cipher.encrypt_json(
        credentials, associated_data=config_aad(None, "smtp")
    )
    with psycopg.connect(admin_url) as conn:
        previous = conn.execute(
            "SELECT id, config_ciphertext, is_enabled, connection_status, updated_at "
            "FROM integrations_config WHERE company_id IS NULL AND provider='smtp'"
        ).fetchone()
        config_id = conn.execute(
            "INSERT INTO integrations_config(company_id,provider,config_ciphertext,"
            "is_enabled,connection_status) VALUES(NULL,'smtp',%s,true,'connected') "
            "ON CONFLICT(company_id,provider) DO UPDATE SET "
            "config_ciphertext=EXCLUDED.config_ciphertext, is_enabled=true, "
            "connection_status='connected', updated_at=now() RETURNING id",
            (cipher,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO staff_users(id,email,full_name,password_hash,status,must_change_password) "
            "VALUES(%s,%s,'Onboarding Administrator',%s,'active',false)",
            (staff_id, email, hashed),
        )
        conn.execute("INSERT INTO roles(id,name) VALUES(%s,%s)", (role_id, f"onboard-{role_id}"))
        conn.execute(
            "INSERT INTO role_permissions(role_id,permission_code) SELECT %s,code FROM permissions",
            (role_id,),
        )
        conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES(%s,%s)", (staff_id, role_id))
        conn.execute(
            "INSERT INTO user_company_scope(user_id,all_companies) VALUES(%s,true)", (staff_id,)
        )
        conn.execute(
            "INSERT INTO user_ownership_level(user_id,level) VALUES(%s,'all')", (staff_id,)
        )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            login = await client.post(
                "/api/auth/staff/login", json={"email": email, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
            department = await client.post(
                "/api/staff/departments",
                json={"name": f"Claims {uuid4().hex[:8]}"},
                headers=headers,
            )
            assert department.status_code == 201, department.text
            yield SimpleNamespace(
                client=client,
                headers=headers,
                department=department.json(),
                smtp=smtp,
                config=config,
                staff_id=staff_id,
                admin_url=admin_url,
                config_id=config_id,
            )
    finally:
        with psycopg.connect(admin_url) as conn:
            conn.execute(
                "DELETE FROM staff_profiles WHERE staff_user_id IN "
                "(SELECT staff_user_id FROM staff_invitations WHERE created_by=%s)",
                (staff_id,),
            )
            if previous:
                conn.execute(
                    "UPDATE integrations_config SET config_ciphertext=%s,is_enabled=%s,"
                    "connection_status=%s,updated_at=%s WHERE id=%s",
                    (*previous[1:], previous[0]),
                )
            else:
                conn.execute("DELETE FROM integrations_config WHERE id=%s", (config_id,))
        await close()
        await redis.aclose()
        app.state._state.clear()
        app.state._state.update(old_state)
        server.close()
        await server.wait_closed()


async def invite(world: SimpleNamespace, domain: str = "gmail.com") -> tuple[str, str, str]:
    email = f"employee-{uuid4().hex}@{domain}"
    result = await world.client.post(
        "/api/staff/users/invitations",
        json={
            "email": email,
            "full_name": "Invited Employee",
            "department_id": world.department["id"],
        },
        headers=world.headers,
    )
    assert result.status_code == 202, result.text
    message = world.smtp["messages"][-1]
    assert message["To"] == email
    assert world.smtp["recipients"][-1] == f"RCPT TO:<{email}>"
    url = re.search(r"https://testserver/invite#token=[A-Za-z0-9_-]+", message.get_content()).group(
        0
    )
    return result.json()["id"], email, parse_qs(urlsplit(url).fragment)["token"][0]


def payload(token: str, nid: str = PROFILE["national_id"]) -> dict:
    return {
        "token": token,
        "password": PASSWORD,
        "profile": PROFILE | {"national_id": nid},
        "avatar_base64": photo(),
    }


async def test_full_invitation_profile_photo_department_and_login(world: SimpleNamespace) -> None:
    user_id, email, token = await invite(world)
    inspected = await world.client.post("/api/auth/staff/invitation/inspect", json={"token": token})
    assert (
        inspected.status_code == 200
        and inspected.json()["department_name"] == world.department["name"]
    )
    assert "id" not in inspected.json()
    with psycopg.connect(world.admin_url) as conn:
        digest = conn.execute(
            "SELECT token_digest FROM staff_invitations "
            "WHERE staff_user_id=%s AND consumed_at IS NULL",
            (user_id,),
        ).fetchone()[0]
        assert digest == hashlib.sha256(token.encode()).digest()
        assert token.encode() not in digest
    assert (
        await world.client.post(
            "/api/auth/staff/invitation/accept", json={"token": token, "password": PASSWORD}
        )
    ).status_code == 422
    # Even callers of the old SECURITY DEFINER helper cannot bypass the profile.
    async with purpose_transaction(app.state.factory, "invitation") as db:
        assert (
            await db.scalar(
                text("SELECT app_security.accept_staff_invitation(:digest,:hash)"),
                {
                    "digest": hashlib.sha256(token.encode()).digest(),
                    "hash": await app.state.auth.passwords.hash(PASSWORD),
                },
            )
            is None
        )
    result = await world.client.post("/api/auth/staff/invitation/accept", json=payload(token))
    assert result.status_code == 200, result.text
    assert (
        await world.client.post("/api/auth/staff/invitation/accept", json=payload(token))
    ).status_code == 400
    assert (
        await world.client.post("/api/auth/staff/invitation/inspect", json={"token": token})
    ).status_code == 400
    directory = (await world.client.get("/api/staff/users")).json()
    employee = next(row for row in directory if row["id"] == user_id)
    assert employee["department_id"] == world.department["id"]
    assert employee["full_name"] == PROFILE["full_name"] and employee["profile_completed_at"]
    assert "national_id" not in employee and "address" not in employee
    profile = await world.client.get(f"/api/staff/users/{user_id}/profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["personal"] == PROFILE | {"date_of_birth": "1999-01-01"}
    assert "no-store" in profile.headers["cache-control"]
    photo_response = await world.client.get(employee["avatar_url"])
    assert (
        photo_response.status_code == 200 and photo_response.headers["content-type"] == "image/jpeg"
    )
    with Image.open(io.BytesIO(photo_response.content)) as image:
        assert image.format == "JPEG" and image.size == (32, 32)
    with psycopg.connect(world.admin_url) as conn:
        identity, avatar = conn.execute(
            "SELECT personal_ciphertext,avatar_content FROM staff_profiles WHERE staff_user_id=%s",
            (user_id,),
        ).fetchone()
        assert PROFILE["national_id"].encode() not in identity and identity[:3] == b"KR\x01"
        assert avatar[:3] == b"KR\x01" and not avatar.startswith(b"\xff\xd8")
        assert (
            app.state.auth.staff_profiles.blobs.decrypt(
                avatar, purpose=AVATAR, resource_id=UUID(user_id)
            )
            == photo_response.content
        )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as employee_client:
        login = await employee_client.post(
            "/api/auth/staff/login", json={"email": email, "password": PASSWORD}
        )
        assert login.status_code == 200 and login.json()["next_step"] == "authenticated"
        assert (await employee_client.get("/api/me")).json()["access"]["full_name"] == PROFILE[
            "full_name"
        ]
        assert (await employee_client.get(employee["avatar_url"])).status_code == 403
        assert (await employee_client.get(f"/api/staff/users/{user_id}/profile")).status_code == 403


async def test_rejected_email_rolls_back_invite_and_resend_invalidates_old_link(
    world: SimpleNamespace,
) -> None:
    world.smtp["reject"] = True
    email = f"reject-{uuid4().hex}@company.com"
    response = await world.client.post(
        "/api/staff/users/invitations",
        json={
            "email": email,
            "full_name": "Rejected Employee",
            "department_id": world.department["id"],
        },
        headers=world.headers,
    )
    assert response.status_code == 503
    with psycopg.connect(world.admin_url) as conn:
        assert (
            conn.execute("SELECT id FROM staff_users WHERE email=%s", (email,)).fetchone() is None
        )
    world.smtp["reject"] = False
    user, email, old = await invite(world, "company.com")
    world.smtp["reject"] = True
    assert (
        await world.client.post(f"/api/staff/users/{user}/resend-invitation", headers=world.headers)
    ).status_code == 503
    assert (
        await world.client.post("/api/auth/staff/invitation/inspect", json={"token": old})
    ).status_code == 200
    world.smtp["reject"] = False
    assert (
        await world.client.post(f"/api/staff/users/{user}/resend-invitation", headers=world.headers)
    ).status_code == 202
    assert world.smtp["messages"][-1]["To"] == email
    assert (
        await world.client.post("/api/auth/staff/invitation/inspect", json={"token": old})
    ).status_code == 400
    assert "sandbox@example.test" not in " ".join(world.smtp["recipients"])
    token = re.search(r"#token=([A-Za-z0-9_-]+)", world.smtp["messages"][-1].get_content()).group(1)
    with psycopg.connect(world.admin_url) as conn:
        conn.execute(
            "UPDATE staff_invitations SET created_at=now()-interval '3 days', "
            "expires_at=now()-interval '1 second' WHERE token_digest=%s",
            (hashlib.sha256(token.encode()).digest(),),
        )
    assert (
        await world.client.post("/api/auth/staff/invitation/accept", json=payload(token))
    ).status_code == 400


async def test_invalid_photo_duplicate_id_department_and_concurrent_acceptance(
    world: SimpleNamespace,
) -> None:
    user, _email, token = await invite(world)
    invalid = payload(token)
    invalid["avatar_base64"] = base64.b64encode(b"<svg>not a photo</svg>").decode()
    assert (
        await world.client.post("/api/auth/staff/invitation/accept", json=invalid)
    ).status_code == 422
    with psycopg.connect(world.admin_url) as conn:
        assert (
            conn.execute("SELECT status FROM staff_users WHERE id=%s", (user,)).fetchone()[0]
            == "invited"
        )
        assert (
            conn.execute(
                "SELECT staff_user_id FROM staff_profiles WHERE staff_user_id=%s", (user,)
            ).fetchone()
            is None
        )

    # A real DB row lock makes exactly one simultaneous redemption win.
    async def accept() -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            return (
                await client.post(
                    "/api/auth/staff/invitation/accept", json=payload(token, "29901010112346")
                )
            ).status_code

    assert sorted(await asyncio.gather(accept(), accept())) == [200, 400]
    _other, _email, second = await invite(world)
    assert (
        await world.client.post(
            "/api/auth/staff/invitation/accept", json=payload(second, "29901010112346")
        )
    ).status_code == 409
    next_department = await world.client.post(
        "/api/staff/departments",
        json={"name": f"Operations {uuid4().hex[:8]}"},
        headers=world.headers,
    )
    response = await world.client.patch(
        f"/api/staff/users/{user}/department",
        json={"department_id": next_department.json()["id"]},
        headers=world.headers,
    )
    assert response.status_code == 200
    assert (await world.client.get(f"/api/staff/users/{user}/profile")).json()[
        "department_name"
    ] == next_department.json()["name"]
    assert (
        await world.client.patch(
            f"/api/staff/users/{user}/department",
            json={"department_id": str(uuid4())},
            headers=world.headers,
        )
    ).status_code == 404
    assert (
        await world.client.post(
            "/api/staff/users/invitations",
            json={
                "email": f"invalid-{uuid4().hex}@company.com",
                "full_name": "Employee",
                "department_id": str(uuid4()),
            },
            headers=world.headers,
        )
    ).status_code == 422


async def test_departments_description_status_and_profile_roles(world: SimpleNamespace) -> None:
    name = f"Service {uuid4().hex[:8]}"
    created = await world.client.post(
        "/api/staff/departments",
        json={"name": name, "description": "Customer care", "is_active": False},
        headers=world.headers,
    )
    assert created.status_code == 201, created.text
    department_id = created.json()["id"]
    listing = (await world.client.get("/api/staff/departments")).json()
    department = next(item for item in listing if item["id"] == department_id)
    assert department["description"] == "Customer care"
    assert department["is_active"] is False and department["employee_count"] == 0
    assert (
        await world.client.post(
            "/api/staff/users/invitations",
            json={
                "email": f"inactive-{uuid4().hex}@company.com",
                "full_name": "Employee",
                "department_id": department_id,
            },
            headers=world.headers,
        )
    ).status_code == 422
    employee, _email, _token = await invite(world)
    assert (
        await world.client.patch(
            f"/api/staff/users/{employee}/department",
            json={"department_id": department_id},
            headers=world.headers,
        )
    ).status_code == 404
    path = f"/api/staff/departments/{department_id}"
    assert (await world.client.put(path, json={"name": name})).status_code == 403
    updated = await world.client.put(
        path,
        json={"name": name, "description": "Operations", "is_active": True},
        headers=world.headers,
    )
    assert updated.status_code == 200, updated.text
    assert (
        await world.client.patch(
            f"/api/staff/users/{employee}/department",
            json={"department_id": department_id},
            headers=world.headers,
        )
    ).status_code == 200
    assert (
        await world.client.put(
            path,
            json={"name": world.department["name"]},
            headers=world.headers,
        )
    ).status_code == 409
    admin_profile = (await world.client.get(f"/api/staff/users/{world.staff_id}/profile")).json()
    assert admin_profile["roles"] and "users.edit" in admin_profile["permissions"]
    assert "two_factor_enabled" in admin_profile and admin_profile["created_at"]
    assert (await world.client.get(f"/api/staff/users/{employee}/profile")).json()[
        "department_name"
    ] == name
    assert (
        await world.client.put(
            path,
            json={"name": name, "description": "Operations", "is_active": False},
            headers=world.headers,
        )
    ).status_code == 200
    # Existing employees keep their department; the disabled destination is no
    # longer assignable. No department status can grant permissions.
    profile = (await world.client.get(f"/api/staff/users/{employee}/profile")).json()
    assert profile["department_is_active"] is False
    assert (
        await world.client.patch(
            f"/api/staff/users/{world.staff_id}/department",
            json={"department_id": department_id},
            headers=world.headers,
        )
    ).status_code == 404


async def test_profile_rls_no_direct_grants_and_scope_checks(world: SimpleNamespace) -> None:
    user, _email, token = await invite(world)
    assert (
        await world.client.post(
            "/api/auth/staff/invitation/accept", json=payload(token, "29901010112347")
        )
    ).status_code == 200
    actor = Actor(world.staff_id, "staff")
    for table in ("staff_profiles", "departments"):
        with pytest.raises(DBAPIError):
            async with scoped_transaction(app.state.factory, actor) as db:
                await db.execute(text(f"SELECT * FROM public.{table}"))
    # Has users.view but lacks the required global company/ownership scope.
    with psycopg.connect(world.admin_url) as conn:
        conn.execute(
            "UPDATE user_ownership_level SET level='own' WHERE user_id=%s", (world.staff_id,)
        )
    async with scoped_transaction(app.state.factory, actor) as db:
        assert (
            await db.execute(
                text("SELECT * FROM app_security.staff_profile(:id)"), {"id": UUID(user)}
            )
        ).one_or_none() is None
        assert (
            await db.scalar(text("SELECT app_security.staff_avatar(:id)"), {"id": UUID(user)})
            is None
        )
        assert (
            await db.scalar(
                text("SELECT app_security.set_staff_department(:id,:department)"),
                {"id": UUID(user), "department": UUID(world.department["id"])},
            )
            is False
        )
    async with purpose_transaction(app.state.factory, "guest") as db:
        assert (
            await db.scalar(
                text("SELECT app_security.staff_invitation_details(:digest)"),
                {"digest": hashlib.sha256(token.encode()).digest()},
            )
            is None
        )
        assert (
            await db.execute(text("SELECT * FROM app_security.staff_email_connection()"))
        ).one_or_none() is None


async def test_password_reset_preserves_profile_and_rejects_disabled_accounts(
    world: SimpleNamespace,
) -> None:
    user, _email, token = await invite(world)
    assert (
        await world.client.post(
            "/api/auth/staff/invitation/accept", json=payload(token, "29901010112348")
        )
    ).status_code == 200
    before = (await world.client.get(f"/api/staff/users/{user}/profile")).json()
    reset = await world.client.post(
        f"/api/staff/users/{user}/password-reset", headers=world.headers
    )
    assert reset.status_code == 202
    reset_token = re.search(
        r"#token=([A-Za-z0-9_-]+)", world.smtp["messages"][-1].get_content()
    ).group(1)
    assert (
        await world.client.post("/api/auth/staff/invitation/inspect", json={"token": reset_token})
    ).json()["kind"] == "password_reset"
    assert (
        await world.client.post(
            "/api/auth/staff/invitation/accept", json={"token": reset_token, "password": PASSWORD}
        )
    ).status_code == 200
    assert (await world.client.get(f"/api/staff/users/{user}/profile")).json() == before
    _disabled, _email, invite_token = await invite(world)
    assert (
        await world.client.post(f"/api/staff/users/{_disabled}/deactivate", headers=world.headers)
    ).status_code == 200
    assert (
        await world.client.post("/api/auth/staff/invitation/accept", json=payload(invite_token))
    ).status_code == 400
