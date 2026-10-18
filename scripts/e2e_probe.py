"""End-to-end probe of the live API: exercises real user + admin flows.

Run against the local stack (postgres + redis) with a capturing mail sink so
OTP / invitation / notification delivery is observable.

    .venv/bin/python scripts/e2e_probe.py

Every request is logged with its status. Anything that is not 2xx/3xx is
reported so 422 regressions are impossible to miss.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The probe speaks plain HTTP, so the Secure cookie flag must be off and the
# env file must not override it.
os.environ["COOKIE_SECURE"] = "false"
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")


def _load_local_env() -> None:
    """Fill the runtime environment from the git-ignored .env of the local stack.

    The probe talks to the same database and Redis as ``make local-up``; the
    values it needs are derived exactly like ``scripts/local_stack.py`` does.
    """
    env_file = ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    port = os.environ.get("POSTGRES_PORT", "5432")
    os.environ.setdefault(
        "DATABASE_URL",
        f"postgresql+asyncpg://app_runtime:{os.environ['APP_DB_PASSWORD']}"
        f"@127.0.0.1:{port}/{os.environ['POSTGRES_DB']}",
    )
    os.environ.setdefault(
        "REDIS_URL", f"redis://127.0.0.1:{os.environ.get('REDIS_PORT', '6379')}/0"
    )


_load_local_env()

import httpx
from app.main import app, install_state
from app.settings import get_settings
from httpx import ASGITransport, AsyncClient

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8000")
RUN = uuid.uuid4().hex[:8]


def run_national_id(serial_prefix: str) -> str:
    """A valid Egyptian national ID that is unique for this probe run."""
    seed = int(RUN, 16)
    month = seed % 12 + 1
    day = (seed // 12) % 28 + 1
    serial = (seed // 336) % 1000
    return f"290{month:02d}{day:02d}01{serial_prefix}{serial:03d}{seed % 10}"


CUSTOMER_NATIONAL_ID = run_national_id("1")
INVITEE_NATIONAL_ID = run_national_id("2")

RESULTS: list[dict[str, Any]] = []
# (collection path, id) of every resource the probe created; removed at the end
# so a probe run never leaves platform-wide state behind (a global assignment
# rule, for instance, would route every new complaint to the probe admin).
CREATED: list[tuple[str, str]] = []


def record(
    name: str,
    method: str,
    path: str,
    status: int,
    detail: Any = None,
    expected: tuple[int, ...] = (200, 201, 202, 204),
) -> None:
    ok = status in expected
    RESULTS.append(
        {
            "name": name,
            "method": method,
            "path": path,
            "status": status,
            "ok": ok,
            "detail": detail,
        }
    )
    flag = "OK " if ok else "FAIL"
    extra = ""
    if not ok and detail is not None:
        extra = f"  -> {json.dumps(detail, default=str)[:900]}"
    print(f"[{flag}] {status:>3} {method:<6} {path}  ({name}){extra}", flush=True)


class Client:
    """A browser-like client that keeps cookies and the double-submit CSRF token."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.http = AsyncClient(base_url=BASE, timeout=30, follow_redirects=True)
        self.csrf: str | None = None

    def adopt_cookies_from(self, other: Client) -> None:
        for cookie in other.http.cookies.jar:
            self.http.cookies.jar.set_cookie(cookie)
        self._sync_csrf()

    def _sync_csrf(self) -> None:
        self.csrf = self.http.cookies.get("csrf_token")

    async def aclose(self) -> None:
        await self.http.aclose()

    async def call(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
        csrf: bool = True,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if csrf and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            if self.csrf is None:
                self._sync_csrf()
            if self.csrf:
                request_headers["X-CSRF-Token"] = self.csrf
        response = await self.http.request(
            method.upper(),
            path,
            json=json_body,
            data=data,
            files=files,
            content=content,
            params=params,
            headers=request_headers,
        )
        self._sync_csrf()
        if method.upper() == "POST" and 200 <= response.status_code < 300:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = None
            if isinstance(body, dict) and isinstance(body.get("id"), str):
                CREATED.append((path, body["id"]))
        detail: Any = None
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:  # noqa: BLE001
                detail = response.text[:500]
        record(name, method.upper(), path, response.status_code, detail, expected)
        return response


class CapturingDelivery:
    """Stands in for SMTP so OTP, invitations and notices are observable."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_otp(self, email: str, code: str) -> None:
        self.sent.append({"kind": "otp", "email": email, "code": code})

    async def send_invitation(self, email: str, url: str) -> None:
        self.sent.append({"kind": "invitation", "email": email, "url": url})

    async def send_password_reset(self, email: str, url: str) -> None:
        self.sent.append({"kind": "password_reset", "email": email, "url": url})

    async def send_notice(
        self,
        mobile: str,
        email: str | None,
        kind: str,
        payload: dict[str, Any],
        preferences: dict[str, Any] | None = None,
    ) -> bool:
        self.sent.append({"kind": "notice", "email": email, "notice_kind": kind})
        return True

    async def send_insurer(self, email: str, ticket: str, reason: str) -> bool:
        self.sent.append({"kind": "insurer", "email": email, "ticket": ticket})
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
        self.sent.append({"kind": "report", "email": ",".join(recipients)})
        return True


def otp_for(delivery: CapturingDelivery, email: str) -> str | None:
    for item in reversed(delivery.sent):
        if item.get("kind") == "otp" and item.get("email") == email:
            return str(item["code"])
    return None


def invitation_for(delivery: CapturingDelivery, email: str) -> str | None:
    for item in reversed(delivery.sent):
        if item.get("kind") == "invitation" and item.get("email") == email:
            match = re.search(r"token=([A-Za-z0-9._~-]+)", str(item["url"]))
            if match:
                return match.group(1)
    return None


def iso(dt: datetime) -> str:
    return dt.isoformat()


# Created through the API and removable through the API. Deleting an API key
# revokes it and deleting a webhook deactivates it: the rows stay, inert, for
# the audit trail -- exactly what the console does.
API_DELETABLE = {
    "/api/staff/admin/assignment-rules": "/api/staff/admin/assignment-rules/{id}",
    "/api/staff/admin/api-keys": "/api/staff/admin/api-keys/{id}",
    "/api/staff/admin/webhooks": "/api/staff/admin/webhooks/{id}",
    "/api/staff/reports/schedules": "/api/staff/reports/schedules/{id}",
    "/api/staff/views": "/api/staff/views/{id}",
}
# No delete endpoint exists (the console deactivates these); remove by id.
SQL_DELETABLE = {
    "/api/staff/admin/sla-rules": ["DELETE FROM sla_rules WHERE id = %s"],
    "/api/staff/admin/templates": ["DELETE FROM canned_replies WHERE id = %s"],
    "/api/staff/admin/teams": [
        "DELETE FROM team_members WHERE team_id = %s",
        "DELETE FROM teams WHERE id = %s",
    ],
    "/api/staff/admin/categories": ["DELETE FROM categories WHERE id = %s"],
    "/api/staff/exports": ["DELETE FROM export_jobs WHERE id = %s"],
    "/api/staff/roles": [
        "DELETE FROM role_permissions WHERE role_id = %s",
        "DELETE FROM user_roles WHERE role_id = %s",
        "DELETE FROM roles WHERE id = %s",
    ],
    "/api/staff/departments": ["DELETE FROM departments WHERE id = %s"],
}


def _admin_dsn() -> str:
    return (
        f"host=127.0.0.1 port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ['POSTGRES_USER']} password={os.environ['POSTGRES_PASSWORD']} "
        f"dbname={os.environ['POSTGRES_DB']}"
    )


async def cleanup_created(staff: Client, invite_email: str) -> None:
    """Remove everything this probe run created, newest first."""
    import psycopg

    leftovers: list[tuple[str, str]] = []
    for path, item_id in reversed(CREATED):
        template = API_DELETABLE.get(path)
        if template:
            await staff.call(
                f"cleanup {path.rsplit('/', 1)[-1]}",
                "DELETE",
                template.format(id=item_id),
                expected=(200, 204, 404),
            )
        else:
            leftovers.append((path, item_id))
    # The invited employee and the rows that point at the account.
    invitee = "(SELECT id FROM staff_users WHERE email = %s)"
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (f"DELETE FROM {table} WHERE {column} IN {invitee}", (invite_email,))
        for table, column in (
            ("staff_invitations", "staff_user_id"),
            ("staff_profiles", "staff_user_id"),
            ("auth_sessions", "subject_id"),
            ("access_change_log", "target_user_id"),
            ("audit_logs", "actor_staff_id"),
        )
    ]
    statements.append(("DELETE FROM staff_users WHERE email = %s", (invite_email,)))
    for path, item_id in leftovers:
        key = "/api/staff/roles" if path.startswith("/api/staff/roles") else path
        for statement in SQL_DELETABLE.get(key, []):
            statements.append((statement, (item_id,)))
    removed = failed = 0
    with psycopg.connect(_admin_dsn()) as conn, conn.cursor() as cur:
        for statement, parameters in statements:
            try:
                cur.execute("SAVEPOINT probe_cleanup")
                cur.execute(statement, parameters)
                # Read the count before RELEASE, which resets it.
                removed += max(0, cur.rowcount)
                cur.execute("RELEASE SAVEPOINT probe_cleanup")
            except Exception as error:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT probe_cleanup")
                failed += 1
                print(f"  cleanup skipped ({statement}): {str(error).splitlines()[0]}")
        conn.commit()
    print(f"cleanup: {removed} row(s) removed, {failed} statement(s) skipped")


async def main() -> int:
    settings = get_settings()
    delivery = CapturingDelivery()

    import app.main as main_module

    original_smtpd = main_module.SMTPDelivery
    main_module.SMTPDelivery = lambda *a, **k: delivery  # type: ignore[assignment]
    try:
        close = await install_state(app, settings)
    finally:
        main_module.SMTPDelivery = original_smtpd

    # The rate limiter keeps its buckets in Redis between runs: a previous
    # probe would make this one trip a 429. Start from a clean slate.
    redis = getattr(app.state, "redis", None)
    if redis is not None:
        stale = [key async for key in redis.scan_iter(match="limit:*")]
        if stale:
            await redis.delete(*stale)
            print(f"cleared {len(stale)} rate-limit bucket(s)")

    transport = ASGITransport(app=app)
    staff = Client("staff")
    staff.http = AsyncClient(transport=transport, base_url=BASE, timeout=30)
    customer = Client("customer")
    customer.http = AsyncClient(transport=transport, base_url=BASE, timeout=30)
    anon = Client("anon")
    anon.http = AsyncClient(transport=transport, base_url=BASE, timeout=30)

    admin_email = os.environ.get("PROBE_ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("PROBE_ADMIN_PASSWORD", "Str0ng!Passw0rd#2026")
    cust_email = f"probe.customer.{RUN}@example.com"
    cust_mobile = "+2010" + f"{int(RUN[:8], 16) % 10**8:08d}"
    invite_email = f"probe.agent.{RUN}@example.com"

    try:
        # ------------------------------------------------------------------
        print("\n=== PUBLIC ===")
        await anon.call("public config", "GET", "/api/public/config", expected=(200,))
        await anon.call("public companies", "GET", "/api/companies", expected=(200,))
        await anon.call(
            "public manifest",
            "GET",
            "/api/public/manifest.webmanifest",
            expected=(200,),
        )
        await anon.call(
            "public brand logo", "GET", "/api/public/brand/logo", expected=(200, 404)
        )
        await anon.call("me unauthenticated", "GET", "/api/me", expected=(401,))
        await anon.call("csrf bootstrap", "GET", "/api/auth/csrf", expected=(200,))

        # ------------------------------------------------------------------
        print("\n=== STAFF LOGIN (web) ===")
        await anon.call(
            "staff login wrong password",
            "POST",
            "/api/auth/staff/login",
            json_body={"email": admin_email, "password": "wrong-password"},
            expected=(401,),
        )
        await anon.call(
            "staff login bad email format",
            "POST",
            "/api/auth/staff/login",
            json_body={"email": "not-an-email", "password": "x"},
            expected=(422,),
        )
        await anon.call(
            "staff login missing field",
            "POST",
            "/api/auth/staff/login",
            json_body={"email": admin_email},
            expected=(422,),
        )
        login = await anon.call(
            "staff login",
            "POST",
            "/api/auth/staff/login",
            json_body={"email": admin_email, "password": admin_password},
            expected=(200,),
        )
        if login.status_code != 200:
            print("FATAL: staff login failed; cannot continue")
            return 1
        staff.adopt_cookies_from(anon)
        me = await staff.call("me (staff)", "GET", "/api/me", expected=(200,))
        if me.status_code != 200:
            print("FATAL: staff session not established")
            return 1

        # ------------------------------------------------------------------
        print("\n=== PLATFORM BRANDING ===")
        await staff.call(
            "update brand name",
            "PUT",
            "/api/platform/brand",
            json_body={
                "platform_name": "Assurance Egypt",
                "logo_url": "/insurance-logo.svg",
            },
            expected=(200,),
        )
        await staff.call(
            "update brand bad name",
            "PUT",
            "/api/platform/brand",
            json_body={"platform_name": "x", "logo_url": "/insurance-logo.svg"},
            expected=(422,),
        )
        await staff.call(
            "update brand external logo url",
            "PUT",
            "/api/platform/brand",
            json_body={
                "platform_name": "Assurance",
                "logo_url": "https://evil.example/x.svg",
            },
            expected=(422,),
        )
        await staff.call(
            "update brand extra field",
            "PUT",
            "/api/platform/brand",
            json_body={
                "platform_name": "Assurance",
                "logo_url": "/insurance-logo.svg",
                "unexpected": True,
            },
            expected=(422,),
        )
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        await staff.call(
            "upload platform logo",
            "POST",
            "/api/platform/brand/logo",
            content=png,
            headers={"Content-Type": "image/png"},
            expected=(200,),
        )
        await staff.call(
            "upload platform logo as text",
            "POST",
            "/api/platform/brand/logo",
            content=b"not-an-image",
            headers={"Content-Type": "image/png"},
            expected=(415,),
        )
        await staff.call(
            "read public config after brand change",
            "GET",
            "/api/public/config",
            expected=(200,),
        )

        # ------------------------------------------------------------------
        print("\n=== DEPARTMENTS (staff sections) ===")
        dep_payload = {
            "name": f"Motor Claims {RUN}",
            "description": "Motor claims",
            "is_active": True,
        }
        created_dep = await staff.call(
            "create department",
            "POST",
            "/api/staff/departments",
            json_body=dep_payload,
            expected=(201,),
        )
        dep_id = (
            created_dep.json().get("id") if created_dep.status_code == 201 else None
        )
        await staff.call(
            "create department missing name",
            "POST",
            "/api/staff/departments",
            json_body={"description": "no name", "is_active": True},
            expected=(422,),
        )
        await staff.call(
            "create department extra field",
            "POST",
            "/api/staff/departments",
            json_body={**dep_payload, "colour": "red"},
            expected=(422,),
        )
        if dep_id:
            await staff.call(
                "update department",
                "PUT",
                f"/api/staff/departments/{dep_id}",
                json_body={**dep_payload, "name": f"Motor Claims Unit {RUN}"},
                expected=(200,),
            )
        await staff.call(
            "list departments", "GET", "/api/staff/departments", expected=(200,)
        )

        # ------------------------------------------------------------------
        print("\n=== CATALOG: COMPANIES / CATEGORIES ===")
        companies = await staff.call(
            "list companies", "GET", "/api/staff/admin/companies", expected=(200,)
        )
        company_id = None
        if companies.status_code == 200 and companies.json():
            company_id = companies.json()[0].get("id")
        if not company_id:
            created = await staff.call(
                "create company",
                "POST",
                "/api/staff/admin/companies",
                json_body={
                    "slug": f"probe-{RUN}",
                    "name_en": f"Probe Insurance {RUN}",
                    "name_ar": "شركة بروبه",
                    "contact_emails": [f"claims.{RUN}@probe-insurance.example"],
                    "supported_types": ["health", "motor"],
                    "is_active": True,
                },
                expected=(201,),
            )
            if created.status_code == 201:
                company_id = created.json().get("id")
        await staff.call(
            "create company bad email",
            "POST",
            "/api/staff/admin/companies",
            json_body={
                "slug": f"probe-bad-{RUN}",
                "name_en": "Probe Bad",
                "name_ar": "بروبه باد",
                "contact_emails": ["Bad@Uppercase.example"],
                "supported_types": ["health"],
                "is_active": True,
            },
            expected=(422,),
        )
        await staff.call(
            "create company bad slug",
            "POST",
            "/api/staff/admin/companies",
            json_body={
                "slug": "Bad Slug!",
                "name_en": "Probe Bad",
                "name_ar": "بروبه باد",
                "contact_emails": [],
                "supported_types": ["health"],
                "is_active": True,
            },
            expected=(422,),
        )
        cat = await staff.call(
            "create category",
            "POST",
            "/api/staff/admin/categories",
            json_body={
                "company_id": company_id,
                "parent_id": None,
                "name_en": f"Motor Claims {RUN}",
                "name_ar": "مطالبات السيارات",
                "is_active": True,
            },
            expected=(201,),
        )
        category_id = cat.json().get("id") if cat.status_code == 201 else None
        await staff.call(
            "create category missing arabic name",
            "POST",
            "/api/staff/admin/categories",
            json_body={"name_en": "No Arabic", "is_active": True},
            expected=(422,),
        )
        if category_id:
            await staff.call(
                "update category",
                "PUT",
                f"/api/staff/admin/categories/{category_id}",
                json_body={
                    "company_id": company_id,
                    "parent_id": None,
                    "name_en": f"Motor Claims v2 {RUN}",
                    "name_ar": "مطالبات السيارات",
                    "is_active": True,
                },
                expected=(200,),
            )
        await staff.call(
            "list categories", "GET", "/api/staff/admin/categories", expected=(200,)
        )

        # ------------------------------------------------------------------
        print("\n=== STAFF USERS & INVITATIONS ===")
        users = await staff.call(
            "list staff users", "GET", "/api/staff/users", expected=(200,)
        )
        me = await staff.call("me (self id)", "GET", "/api/me", expected=(200,))
        staff_id = (
            (me.json().get("access") or {}).get("id") if me.status_code == 200 else None
        )
        other_staff_id = next(
            (
                user.get("id")
                for user in (users.json() if users.status_code == 200 else [])
                if user.get("id") != staff_id and user.get("status") == "active"
            ),
            None,
        )
        await staff.call(
            "create invitation",
            "POST",
            "/api/staff/users/invitations",
            json_body={
                "department_id": dep_id,
                "email": invite_email,
                "full_name": "Probe Agent",
            },
            expected=(202,),
        )
        await staff.call(
            "create invitation missing department",
            "POST",
            "/api/staff/users/invitations",
            json_body={"email": f"probe2.{RUN}@example.com", "full_name": "Probe Two"},
            expected=(422,),
        )
        await staff.call(
            "create invitation bad email",
            "POST",
            "/api/staff/users/invitations",
            json_body={
                "department_id": dep_id,
                "email": "not-an-email",
                "full_name": "Probe Three",
            },
            expected=(422,),
        )
        token = invitation_for(delivery, invite_email)
        if token:
            await anon.call(
                "inspect invitation",
                "POST",
                "/api/auth/staff/invitation/inspect",
                json_body={"token": token},
                expected=(200,),
            )
            await anon.call(
                "accept invitation weak password",
                "POST",
                "/api/auth/staff/invitation/accept",
                json_body={
                    "token": token,
                    "password": "weak",
                    "profile": {"email": invite_email, "full_name": "Probe Agent"},
                },
                expected=(422,),
            )
            await anon.call(
                "accept invitation bad token",
                "POST",
                "/api/auth/staff/invitation/accept",
                json_body={
                    "token": "not-a-real-token",
                    "password": "Pr0be!Agent#2026",
                    "profile": {"email": "x@example.com", "full_name": "X"},
                },
                expected=(422,),
            )
            await anon.call(
                "accept invitation",
                "POST",
                "/api/auth/staff/invitation/accept",
                json_body={
                    "token": token,
                    "password": "Pr0be!Agent#2026",
                    "profile": {
                        "full_name": "Probe Agent",
                        "national_id": INVITEE_NATIONAL_ID,
                        "mobile": "+2011" + f"{int(RUN[:8], 16) % 10**8:08d}",
                        "address": "12 Nile Street, Dokki",
                        "city": "Giza",
                        "job_title": "Claims agent",
                        "emergency_phone": None,
                    },
                    "avatar_base64": base64.b64encode(png).decode(),
                },
                expected=(200, 204),
            )
        else:
            record(
                "invitation delivery",
                "POST",
                "mail",
                0,
                {"note": "no invitation captured"},
                (200,),
            )

        # ------------------------------------------------------------------
        print("\n=== ROLES & PERMISSIONS ===")
        roles = await staff.call(
            "list roles", "GET", "/api/staff/roles", expected=(200,)
        )
        role_id = (
            roles.json()[0].get("id")
            if roles.status_code == 200 and roles.json()
            else None
        )
        new_role = await staff.call(
            "create role",
            "POST",
            "/api/staff/roles",
            json_body={
                "name": f"Probe Role {RUN}",
                "description": "probe",
                "permissions": ["complaints.view"],
            },
            expected=(201,),
        )
        if new_role.status_code == 201:
            role_id = new_role.json().get("id")
        await staff.call(
            "create role unknown permission",
            "POST",
            "/api/staff/roles",
            json_body={
                "name": f"Bad Role {RUN}",
                "description": "",
                "permissions": ["not.a.permission"],
            },
            expected=(422,),
        )
        await staff.call(
            "create role short name",
            "POST",
            "/api/staff/roles",
            json_body={"name": "x", "description": "", "permissions": []},
            expected=(422,),
        )
        if role_id:
            await staff.call(
                "update role",
                "PUT",
                f"/api/staff/roles/{role_id}",
                json_body={
                    "name": f"Probe Role {RUN}",
                    "description": "updated",
                    "permissions": ["complaints.view", "complaints.reply_public"],
                },
                expected=(200,),
            )
            await staff.call(
                "clone role",
                "POST",
                f"/api/staff/roles/{role_id}/clone",
                json_body={"name": f"Probe Role Clone {RUN}"},
                expected=(201,),
            )
            await staff.call(
                "clone role missing name",
                "POST",
                f"/api/staff/roles/{role_id}/clone",
                json_body={},
                expected=(422,),
            )
        await staff.call(
            "access options", "GET", "/api/staff/admin/access-options", expected=(200,)
        )

        # ------------------------------------------------------------------
        print("\n=== STAFF PROFILE ===")
        if staff_id:
            await staff.call(
                "staff profile",
                "GET",
                f"/api/staff/users/{staff_id}/profile",
                expected=(200,),
            )
            await staff.call(
                "staff avatar",
                "GET",
                f"/api/staff/users/{staff_id}/avatar",
                expected=(200, 404),
            )
            await staff.call(
                "edit staff profile (self -> 403)",
                "PATCH",
                f"/api/staff/users/{staff_id}",
                json_body={"email": admin_email, "full_name": "Platform Admin"},
                expected=(403,),
            )
            await staff.call(
                "edit staff profile bad email",
                "PATCH",
                f"/api/staff/users/{staff_id}",
                json_body={"email": "bad", "full_name": "Platform Admin"},
                expected=(422,),
            )
            await staff.call(
                "staff access snapshot",
                "GET",
                f"/api/staff/users/{staff_id}/access",
                expected=(200,),
            )
            await staff.call(
                "staff access explain",
                "GET",
                f"/api/staff/users/{staff_id}/access/explain",
                expected=(200,),
            )
            await staff.call(
                "set 2fa policy",
                "PATCH",
                f"/api/staff/users/{staff_id}/2fa-policy",
                json_body={"required": False},
                expected=(403, 200),
            )
            await staff.call(
                "global 2fa policy",
                "PATCH",
                "/api/staff/settings/staff-2fa",
                json_body={"required": False},
                expected=(200,),
            )
            # Changing the global policy revokes every staff session (including
            # the actor's) so the new rule applies at the next sign-in.
            staff.http.cookies.clear()
            staff.csrf = None
            await staff.call(
                "csrf bootstrap (re-login)", "GET", "/api/auth/csrf", expected=(200,)
            )
            await staff.call(
                "staff login (after global policy)",
                "POST",
                "/api/auth/staff/login",
                json_body={"email": admin_email, "password": admin_password},
                expected=(200,),
            )
        await staff.call("my sessions", "GET", "/api/me/sessions", expected=(200,))
        await staff.call(
            "my notifications", "GET", "/api/me/notifications", expected=(200,)
        )

        # ------------------------------------------------------------------
        print("\n=== CUSTOMER OTP FLOW (web) ===")
        await anon.call(
            "otp start",
            "POST",
            "/api/auth/customer/otp/start",
            json_body={"mobile": cust_mobile, "email": cust_email},
            expected=(202,),
        )
        await anon.call(
            "otp start bad mobile",
            "POST",
            "/api/auth/customer/otp/start",
            json_body={"mobile": "0100000000", "email": cust_email},
            expected=(422,),
        )
        await anon.call(
            "otp start missing email",
            "POST",
            "/api/auth/customer/otp/start",
            json_body={"mobile": cust_mobile},
            expected=(422,),
        )
        code = otp_for(delivery, cust_email)
        if code:
            await anon.call(
                "otp verify wrong code",
                "POST",
                "/api/auth/customer/otp/verify",
                json_body={
                    "mobile": cust_mobile,
                    "email": cust_email,
                    "code": "000000",
                },
                expected=(401, 422),
            )
            verify = await anon.call(
                "otp verify",
                "POST",
                "/api/auth/customer/otp/verify",
                json_body={
                    "mobile": cust_mobile,
                    "email": cust_email,
                    "code": code,
                    "full_name": "Probe Customer",
                    "national_id": CUSTOMER_NATIONAL_ID,
                },
                expected=(200,),
            )
            if verify.status_code == 200:
                customer.adopt_cookies_from(anon)
                await customer.call("me (customer)", "GET", "/api/me", expected=(200,))
        else:
            record(
                "otp delivery", "POST", "mail", 0, {"note": "no OTP captured"}, (200,)
            )

        # ------------------------------------------------------------------
        print("\n=== CUSTOMER PROFILE & COMPLAINTS ===")
        if customer.http.cookies.get("access_token"):
            await customer.call(
                "customer profile", "GET", "/api/customer/profile", expected=(200,)
            )
            await customer.call(
                "update customer profile",
                "PATCH",
                "/api/customer/profile",
                json_body={"full_name": "Probe Customer", "email": cust_email},
                expected=(200,),
            )
            await customer.call(
                "update customer profile bad email",
                "PATCH",
                "/api/customer/profile",
                json_body={"full_name": "Probe Customer", "email": "nope"},
                expected=(422,),
            )
            await customer.call(
                "customer privacy consents",
                "POST",
                "/api/customer/privacy/consents",
                json_body={"purpose": "marketing", "granted": True},
                expected=(200, 201),
            )
            await customer.call(
                "customer complaint list",
                "GET",
                "/api/customer/complaints",
                expected=(200,),
            )
            create = await customer.call(
                "create customer complaint",
                "POST",
                "/api/customer/complaints",
                json_body={
                    "national_id": CUSTOMER_NATIONAL_ID,
                    "mobile": cust_mobile,
                    "company_id": company_id,
                    "insurance_type": "health",
                    "policy_number": "POL-2026-0001",
                    "subject": "Probe complaint subject",
                    "description": "Probe complaint description long enough for the API.",
                },
                headers={"Idempotency-Key": f"probe-key-{RUN}"},
                expected=(201, 200),
            )
            complaint_id = (
                create.json().get("id") if create.status_code in {200, 201} else None
            )
            if complaint_id:
                await customer.call(
                    "customer complaint detail",
                    "GET",
                    f"/api/customer/complaints/{complaint_id}",
                    expected=(200,),
                )
                await customer.call(
                    "customer reply",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/reply",
                    json_body={"body": "Following up on my complaint."},
                    expected=(200,),
                )
                await customer.call(
                    "customer reply empty body",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/reply",
                    json_body={"body": ""},
                    expected=(422,),
                )
                await customer.call(
                    "customer csat",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/csat",
                    json_body={"score": 5, "comment": "great"},
                    # Feedback opens once the complaint is resolved; the portal only
                    # shows the rating box when the API reports can_csat.
                    expected=(200, 201, 409),
                )
                await customer.call(
                    "customer csat out of range",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/csat",
                    json_body={"score": 9, "comment": "bad"},
                    expected=(422,),
                )
                await customer.call(
                    "customer voice consent",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/voice-consent",
                    json_body={"enabled": True},
                    expected=(200,),
                )
                await customer.call(
                    "customer attachment plan",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/attachments/plan",
                    # The exact TicketDetailPage/uploadFiles plan: a list of files
                    # with their SHA-256 checksum.
                    json_body={
                        "files": [
                            {
                                "filename": "proof.pdf",
                                "mime_type": "application/pdf",
                                "size_bytes": 1024,
                                "checksum_sha256": "0" * 64,
                            }
                        ]
                    },
                    expected=(201, 200, 409),
                )
                await customer.call(
                    "customer reopen",
                    "POST",
                    f"/api/customer/complaints/{complaint_id}/reopen",
                    expected=(200, 409),
                )

        # ------------------------------------------------------------------
        print("\n=== STAFF COMPLAINT WORKSPACE ===")
        complaints = await staff.call(
            "staff complaint list", "GET", "/api/staff/complaints", expected=(200,)
        )
        staff_complaint_id = None
        if complaints.status_code == 200:
            body = complaints.json()
            items = body.get("items") or body.get("data") or []
            if items:
                staff_complaint_id = items[0].get("id")
        await staff.call(
            "complaint filter options",
            "GET",
            "/api/staff/complaints/filter-options",
            expected=(200,),
        )
        await staff.call(
            "complaint counters",
            "GET",
            "/api/staff/complaints/counters",
            expected=(200,),
        )
        await staff.call(
            "complaint filtered list",
            "GET",
            "/api/staff/complaints",
            params={"status": "new", "page": 1, "page_size": 10},
            expected=(200,),
        )
        await staff.call(
            "complaint list bad page size",
            "GET",
            "/api/staff/complaints",
            params={"page_size": 0},
            expected=(422,),
        )
        await staff.call(
            "complaint list bad status filter",
            "GET",
            "/api/staff/complaints",
            params={"status": "not-a-status"},
            expected=(422,),
        )
        await staff.call(
            "complaint export",
            "GET",
            "/api/staff/complaints/export",
            params={
                "date_from": iso(datetime.now(UTC) - timedelta(hours=1)),
                "date_to": iso(datetime.now(UTC) + timedelta(minutes=1)),
            },
            expected=(200,),
        )
        if staff_complaint_id:
            await staff.call(
                "staff complaint detail",
                "GET",
                f"/api/staff/complaints/{staff_complaint_id}",
                expected=(200,),
            )
            await staff.call(
                "staff complaint timeline",
                "GET",
                f"/api/staff/complaints/{staff_complaint_id}/timeline",
                expected=(200,),
            )
            await staff.call(
                "staff reply public",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/reply",
                json_body={"body": "We are investigating."},
                expected=(200,),
            )
            await staff.call(
                "staff internal note",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/notes",
                json_body={"body": "Internal note."},
                expected=(200,),
            )
            await staff.call(
                "staff change status",
                "PATCH",
                f"/api/staff/complaints/{staff_complaint_id}/status",
                json_body={"status": "in_review"},
                expected=(200,),
            )
            await staff.call(
                "staff change status invalid",
                "PATCH",
                f"/api/staff/complaints/{staff_complaint_id}/status",
                json_body={"status": "not_a_status"},
                expected=(422,),
            )
            await staff.call(
                "staff sla",
                "GET",
                f"/api/staff/complaints/{staff_complaint_id}/sla",
                expected=(200,),
            )
            await staff.call(
                "staff notifications",
                "GET",
                "/api/staff/notifications",
                expected=(200,),
            )
            await staff.call(
                "canned replies",
                "GET",
                f"/api/staff/complaints/{staff_complaint_id}/canned-replies",
                expected=(200,),
            )
            await staff.call(
                "duplicates",
                "GET",
                f"/api/staff/complaints/{staff_complaint_id}/duplicates",
                expected=(200,),
            )
            await staff.call(
                "complaint attachments",
                "GET",
                f"/api/staff/complaints/{staff_complaint_id}/attachments",
                expected=(200,),
            )
            await staff.call(
                "complaint email",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/email",
                json_body={"locale": "en", "agent_note": "probe", "attachment_ids": []},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                expected=(200, 201, 202, 400, 409, 503),
            )
            await staff.call(
                "complaint whatsapp",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/whatsapp",
                json_body={
                    "body": "probe",
                    "template_name": "probe",
                    "language": "en",
                    "parameters": [],
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
                expected=(200, 201, 202, 400, 409, 503),
            )
            await staff.call(
                "complaint sms",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/sms",
                json_body={"body": "probe"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                expected=(200, 201, 202, 400, 409, 503),
            )
            await staff.call(
                "assign complaint to nobody (negative)",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/assign",
                json_body={"agent_id": None, "team_id": None},
                expected=(422,),
            )
            await staff.call(
                "assign complaint",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/assign",
                json_body={"agent_id": staff_id, "team_id": None},
                expected=(200, 201),
            )
            await staff.call(
                "escalate complaint",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/escalate",
                json_body={"reason": "probe escalation"},
                expected=(200, 202),
            )
            await staff.call(
                "reveal national id",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/national-id:reveal",
                json_body={"reason": "Customer identity verification for the probe"},
                expected=(200,),
            )
            await staff.call(
                "ai analyze",
                "POST",
                f"/api/staff/complaints/{staff_complaint_id}/ai/analyze",
                json_body={"locale": "en"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                expected=(200, 201, 202, 400, 409, 503),
            )

        # ------------------------------------------------------------------
        print("\n=== SAVED VIEWS ===")
        view = await staff.call(
            "create saved view",
            "POST",
            "/api/staff/views",
            json_body={"name": f"Probe View {RUN}", "criteria": {"status": "new"}},
            expected=(201,),
        )
        if view.status_code == 201:
            view_id = view.json().get("id")
            await staff.call(
                "update saved view",
                "PUT",
                f"/api/staff/views/{view_id}",
                json_body={
                    "name": f"Probe View v2 {RUN}",
                    "criteria": {"status": "in_review"},
                },
                expected=(200,),
            )
            await staff.call(
                "saved view complaints",
                "GET",
                f"/api/staff/views/{view_id}/complaints",
                expected=(200,),
            )
            await staff.call(
                "delete saved view",
                "DELETE",
                f"/api/staff/views/{view_id}",
                expected=(204,),
            )
        await staff.call(
            "create saved view empty name",
            "POST",
            "/api/staff/views",
            json_body={"name": "", "criteria": {}},
            expected=(422,),
        )

        # ------------------------------------------------------------------
        print("\n=== TEAMS, SLA RULES, TEMPLATES, ASSIGNMENT RULES ===")
        team = await staff.call(
            "create team",
            "POST",
            "/api/staff/admin/teams",
            json_body={
                "name": f"Probe Team {RUN}",
                "lead_staff_id": other_staff_id,
                "member_ids": [other_staff_id] if other_staff_id else [],
            },
            expected=(201,),
        )
        if team.status_code == 201:
            team_id = team.json().get("id")
            await staff.call(
                "update team",
                "PUT",
                f"/api/staff/admin/teams/{team_id}",
                json_body={
                    "name": f"Probe Team v2 {RUN}",
                    "lead_staff_id": other_staff_id,
                    "member_ids": [other_staff_id] if other_staff_id else [],
                },
                expected=(200,),
            )
        await staff.call("list teams", "GET", "/api/staff/admin/teams", expected=(200,))
        await staff.call(
            "create sla rule",
            "POST",
            "/api/staff/admin/sla-rules",
            json_body={
                "company_id": None,
                "insurance_type": None,
                "priority": None,
                "response_minutes": 60,
                "resolution_minutes": 240,
                "alert_before_minutes": 30,
                "business_hours": None,
                "is_active": True,
            },
            expected=(201,),
        )
        await staff.call(
            "create sla rule bad ordering",
            "POST",
            "/api/staff/admin/sla-rules",
            json_body={
                "company_id": None,
                "insurance_type": None,
                "priority": None,
                "response_minutes": 600,
                "resolution_minutes": 60,
                "alert_before_minutes": 30,
                "business_hours": None,
                "is_active": True,
            },
            expected=(422,),
        )
        await staff.call(
            "list sla rules", "GET", "/api/staff/admin/sla-rules", expected=(200,)
        )
        await staff.call(
            "simulate sla rule",
            "POST",
            "/api/staff/admin/sla-rules/simulate",
            json_body={
                "company_id": None,
                "insurance_type": "health",
                "priority": "normal",
                "created_at": iso(datetime.now(UTC)),
            },
            expected=(200,),
        )
        await staff.call(
            "preview business hours",
            "POST",
            "/api/staff/admin/sla-rules/business-hours/preview",
            json_body={
                "business_hours": {
                    "time_zone": "Africa/Cairo",
                    "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
                    "holidays": [],
                },
                "minutes": 120,
                "from_at": iso(datetime.now(UTC)),
                "company_id": None,
                "insurance_type": None,
            },
            expected=(200,),
        )
        await staff.call(
            "create template",
            "POST",
            "/api/staff/admin/templates",
            json_body={
                "company_id": None,
                "title": f"Probe Template {RUN}",
                "body_en": "Hello {customer}",
                "body_ar": "مرحبا {customer}",
                "is_active": True,
            },
            expected=(201,),
        )
        await staff.call(
            "list templates", "GET", "/api/staff/admin/templates", expected=(200,)
        )
        await staff.call(
            "create assignment rule",
            "POST",
            "/api/staff/admin/assignment-rules",
            json_body={
                "company_id": None,
                "insurance_type": None,
                "category_id": None,
                "priority": None,
                "target_team_id": None,
                "target_agent_id": staff_id,
                "strategy": "least_workload",
                "sort_order": 100,
                "is_active": True,
            },
            expected=(201,),
        )
        await staff.call(
            "assignment options",
            "GET",
            "/api/staff/admin/automation-options",
            expected=(200,),
        )
        await staff.call(
            "preview assignment rule",
            "POST",
            "/api/staff/admin/assignment-rules/preview",
            json_body={
                "company_id": company_id,
                "insurance_type": "health",
                "priority": "normal",
            },
            expected=(200,),
        )

        # ------------------------------------------------------------------
        print("\n=== REPORTS, EXPORTS, ANALYTICS, AUDIT, STORAGE ===")
        await staff.call(
            "analytics overview",
            "GET",
            "/api/staff/analytics/overview",
            params={
                "date_from": iso(datetime.now(UTC) - timedelta(days=29)),
                "date_to": iso(datetime.now(UTC)),
            },
            expected=(200,),
        )
        await staff.call(
            "analytics rollup",
            "GET",
            "/api/staff/analytics/rollup",
            params={
                "date_from": iso(datetime.now(UTC) - timedelta(days=29)),
                "date_to": iso(datetime.now(UTC)),
            },
            expected=(200,),
        )
        await staff.call(
            "analytics scorecards",
            "GET",
            "/api/staff/analytics/scorecards",
            params={
                "date_from": iso(datetime.now(UTC) - timedelta(days=29)),
                "date_to": iso(datetime.now(UTC)),
            },
            expected=(200,),
        )
        await staff.call(
            "audit events", "GET", "/api/staff/audit/events", expected=(200,)
        )
        await staff.call(
            "audit access changes",
            "GET",
            "/api/staff/audit/access-changes",
            expected=(200,),
        )
        await staff.call(
            "storage inventory", "GET", "/api/staff/storage", expected=(200,)
        )
        await staff.call(
            "customer directory", "GET", "/api/staff/customers", expected=(200, 403)
        )
        schedule = await staff.call(
            "create report schedule",
            "POST",
            "/api/staff/reports/schedules",
            json_body={
                "name": f"Probe Schedule {RUN}",
                "filters": {"window_days": 7},
                "cron_expression": "0 9 * * *",
                "time_zone": "Africa/Cairo",
                "recipient_emails": [f"reports.{RUN}@example.com"],
                "format": "csv",
                "next_run_at": None,
                "is_active": True,
            },
            expected=(201,),
        )
        if schedule.status_code == 201:
            schedule_id = schedule.json().get("id")
            await staff.call(
                "run report schedule",
                "POST",
                f"/api/staff/reports/schedules/{schedule_id}/run",
                expected=(202,),
            )
            await staff.call(
                "update report schedule",
                "PUT",
                f"/api/staff/reports/schedules/{schedule_id}",
                json_body={
                    "name": f"Probe Schedule v2 {RUN}",
                    "filters": {"window_days": 14},
                    "cron_expression": "0 10 * * *",
                    "time_zone": "Africa/Cairo",
                    "recipient_emails": [f"reports.{RUN}@example.com"],
                    "format": "csv",
                    "next_run_at": None,
                    "is_active": True,
                },
                expected=(200,),
            )
        await staff.call(
            "create report schedule bad cron",
            "POST",
            "/api/staff/reports/schedules",
            json_body={
                "name": f"Bad Schedule {RUN}",
                "filters": {},
                "cron_expression": "not a cron",
                "time_zone": "Africa/Cairo",
                "recipient_emails": ["a@example.com"],
                "format": "csv",
                "next_run_at": None,
                "is_active": True,
            },
            expected=(422,),
        )
        await staff.call(
            "list report schedules",
            "GET",
            "/api/staff/reports/schedules",
            expected=(200,),
        )
        export = await staff.call(
            "create export job",
            "POST",
            "/api/staff/exports",
            json_body={
                "kind": "complaints",
                "format": "csv",
                "filters": {
                    "date_from": iso(datetime.now(UTC) - timedelta(days=29)),
                    "date_to": iso(datetime.now(UTC)),
                },
            },
            expected=(201, 202),
        )
        if export.status_code in {201, 202}:
            job_id = export.json().get("id")
            listed = await staff.call(
                "export job list", "GET", "/api/staff/exports", expected=(200,)
            )
            if listed.status_code == 200 and not any(
                job.get("id") == job_id for job in listed.json()
            ):
                record(
                    "export job listed",
                    "GET",
                    "/api/staff/exports",
                    0,
                    {"note": "new job missing"},
                    (200,),
                )

        # ------------------------------------------------------------------
        print("\n=== INTEGRATIONS ===")
        await staff.call(
            "list integration connections",
            "GET",
            "/api/staff/admin/integration-connections",
            expected=(200,),
        )
        await staff.call(
            "create api key",
            "POST",
            "/api/staff/admin/api-keys",
            json_body={
                "permissions": ["complaints.view"],
                "all_companies": True,
                "company_ids": [],
                "expires_at": None,
            },
            expected=(201,),
        )
        await staff.call(
            "create api key conflicting scope",
            "POST",
            "/api/staff/admin/api-keys",
            json_body={
                "permissions": ["complaints.view"],
                "all_companies": True,
                "company_ids": [str(company_id)] if company_id else [],
                "expires_at": None,
            },
            expected=(422,),
        )
        await staff.call(
            "create webhook",
            "POST",
            "/api/staff/admin/webhooks",
            json_body={
                "url": "https://hooks.example.com/insurance",
                "company_id": None,
                "event_types": ["complaint.status_changed"],
            },
            expected=(201,),
        )
        await staff.call(
            "create webhook insecure url",
            "POST",
            "/api/staff/admin/webhooks",
            json_body={
                "url": "http://hooks.example.com/insurance",
                "company_id": None,
                "event_types": ["complaint.status_changed"],
            },
            expected=(422,),
        )
        await staff.call(
            "test integration connection",
            "POST",
            "/api/staff/admin/integration-connections/test",
            json_body={"company_id": None, "provider": "smtp"},
            expected=(200, 503),
        )

        # ------------------------------------------------------------------
        print("\n=== CUSTOMER SIDE EXTRA ===")
        if customer.http.cookies.get("access_token"):
            await customer.call(
                "customer notifications",
                "GET",
                "/api/customer/notifications",
                expected=(200,),
            )
            await customer.call(
                "customer counters",
                "GET",
                "/api/customer/complaints/counters",
                expected=(200,),
            )
            await customer.call(
                "customer privacy requests",
                "GET",
                "/api/customer/privacy/requests",
                expected=(200,),
            )
            await customer.call(
                "customer privacy export",
                "GET",
                "/api/customer/privacy/export",
                expected=(200,),
            )
        await anon.call(
            "guest track invalid",
            "POST",
            "/api/guest/track",
            json_body={"ticket_no": "CMP-2026-000001", "national_id": "29006220101234"},
            expected=(404, 422),
        )
        await anon.call(
            "public complaint lookup",
            "GET",
            "/api/public/v1/complaints/CMP-2026-000001",
            expected=(404, 401),
        )

        # ------------------------------------------------------------------
        print("\n=== CLEANUP (everything this run created) ===")
        await cleanup_created(staff, invite_email)

        print("\n=== SESSION LIFECYCLE ===")
        await staff.call(
            "refresh session", "POST", "/api/auth/refresh", expected=(200, 401)
        )
        await staff.call("logout", "POST", "/api/auth/logout", expected=(200,))

    finally:
        await staff.aclose()
        await customer.aclose()
        await anon.aclose()
        await close()

    failures = [item for item in RESULTS if not item["ok"]]
    print("\n" + "=" * 78)
    print(f"TOTAL: {len(RESULTS)}   FAILED: {len(failures)}")
    print("=" * 78)
    for item in failures:
        print(
            f"  {item['status']:>3} {item['method']:<6} {item['path']}  ({item['name']})"
        )
        if item["detail"] is not None:
            print(f"      {json.dumps(item['detail'], default=str)[:700]}")
    unprocessable = [item for item in RESULTS if item["status"] == 422]
    print(f"\n422 responses: {len(unprocessable)}")
    for item in unprocessable:
        print(f"  {item['method']:<6} {item['path']}  ({item['name']})")
        print(f"      {json.dumps(item['detail'], default=str)[:700]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
