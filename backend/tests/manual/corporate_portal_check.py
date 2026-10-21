"""End-to-end corporate portal check against a live, migrated database.

Seeds one corporate client (contract, portal user, two employees with
requests), then drives the HTTP API the way the portal will: sign in, read the
contract, list and filter employees, and confirm the account cannot reach
anything else.

    ./.venv/bin/python backend/tests/manual/corporate_portal_check.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

env: dict[str, str] = {}
for line in (BACKEND.parent / ".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
os.environ.update(env)
# Normally supplied by scripts/local_stack.py when it starts the API.
os.environ.setdefault(
    "DATABASE_URL",
    f"postgresql+asyncpg://app_runtime:{env['APP_DB_PASSWORD']}"
    f"@127.0.0.1:5432/{env['POSTGRES_DB']}",
)
os.environ.setdefault(
    "WORKER_DATABASE_URL",
    f"postgresql+asyncpg://app_worker:{env['APP_WORKER_DB_PASSWORD']}"
    f"@127.0.0.1:5432/{env['POSTGRES_DB']}",
)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("ENVIRONMENT", "development")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.main import app  # noqa: E402
from app.security.passwords import StaffPasswords  # noqa: E402

PW = env["POSTGRES_PASSWORD"]
DB = env["POSTGRES_DB"]
PASSWORD = "Corp0rate-Portal-2026"
EMPLOYEES = (("+201500000001", "سعاد فؤاد"), ("+201500000002", "Karim Nabil"))
failures: list[str] = []


def check(label: str, got: object, want: object = True) -> None:
    ok = got == want
    print(
        f"  {'PASS' if ok else 'FAIL'}  {label:46} got={got!r}" + ("" if ok else f" want={want!r}")
    )
    if not ok:
        failures.append(label)


async def seed() -> None:
    admin = create_async_engine(f"postgresql+psycopg://postgres:{PW}@127.0.0.1:5432/{DB}")
    password_hash = await StaffPasswords().hash(PASSWORD)
    async with admin.begin() as conn:
        await conn.execute(text("SELECT set_config('app.scope','worker',false)"))
        client_id = await conn.scalar(
            text(
                "INSERT INTO public.corporate_clients (slug, name_ar, name_en, is_active)"
                " VALUES ('acme-eg','شركة أكمي','Acme Egypt', true)"
                " ON CONFLICT (slug) DO UPDATE SET name_en = EXCLUDED.name_en RETURNING id"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO public.corporate_contracts (corporate_client_id, contract_no,"
                " contract_value, currency, payment_frequency, starts_on, renews_on, status,"
                " headcount) VALUES (:cid,'CT-ACME-1', 2500000.00,'EGP','quarterly',"
                " DATE '2026-01-01', DATE '2027-01-01','active', 430)"
                " ON CONFLICT (contract_no) DO UPDATE SET contract_value = EXCLUDED.contract_value"
            ),
            {"cid": client_id},
        )
        await conn.execute(
            text(
                "INSERT INTO public.corporate_users (corporate_client_id, username, full_name,"
                " password_hash, status, must_change_password)"
                " VALUES (:cid,'acme.hr','HR Acme',:h,'active', false)"
                " ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash,"
                " corporate_client_id = EXCLUDED.corporate_client_id, status = 'active'"
            ),
            {"cid": client_id, "h": password_hash},
        )
        company = await conn.scalar(text("SELECT id FROM public.insurance_companies LIMIT 1"))

    # Raise the employees' requests through the real intake path.
    worker = create_async_engine(
        f"postgresql+psycopg://app_worker:{env['APP_WORKER_DB_PASSWORD']}@127.0.0.1:5432/{DB}"
    )
    for mobile, name in EMPLOYEES:
        async with worker.begin() as conn:
            await conn.execute(text("SELECT set_config('app.scope','worker',false)"))
            await conn.execute(
                text(
                    "SELECT app_security.create_channel_request(:co,"
                    " CAST('whatsapp' AS public.communication_channel),'whatsapp',:m,:n,"
                    " 'طلب موافقة','تفاصيل الطلب',NULL,'CARD-'||right(:m,4),"
                    " CAST('health' AS public.insurance_type),'seed-'||right(:m,4),NULL)"
                ),
                {"co": company, "m": mobile, "n": name},
            )
    await worker.dispose()

    mobiles = {"m1": EMPLOYEES[0][0], "m2": EMPLOYEES[1][0]}
    async with admin.begin() as conn:
        await conn.execute(text("SELECT set_config('app.scope','worker',false)"))
        await conn.execute(
            text(
                "UPDATE public.customers SET corporate_client_id = :cid,"
                " employee_code = 'E-'||right(mobile,4) WHERE mobile IN (:m1,:m2)"
            ),
            {"cid": client_id, **mobiles},
        )
        await conn.execute(
            text(
                "UPDATE public.complaints SET corporate_client_id = :cid WHERE customer_id IN"
                " (SELECT id FROM public.customers WHERE mobile IN (:m1,:m2))"
            ),
            {"cid": client_id, **mobiles},
        )
    await admin.dispose()

    # Repeated runs would otherwise trip the brute-force lockout from the
    # deliberate wrong-password attempt below.
    import redis.asyncio as aioredis

    cache = aioredis.from_url(os.environ["REDIS_URL"])
    for pattern in (
        "lockout:corporate*",
        "lockfail:corporate*",
        "lockn:corporate*",
        "limit:corporate*",
    ):
        async for key in cache.scan_iter(match=pattern):
            await cache.delete(key)
    await cache.aclose()
    print(f"seeded corporate client {client_id}")


async def main() -> int:
    await seed()
    transport = ASGITransport(app=app)
    # https:// so the Secure session cookies are retained by the client.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="https://test") as http,
    ):
        print("\nAuthentication")
        bad = await http.post(
            "/api/corporate/login", json={"username": "acme.hr", "password": "wrong-password-x"}
        )
        check("wrong password rejected", bad.status_code, 401)
        signed_in = await http.post(
            "/api/corporate/login", json={"username": "acme.hr", "password": PASSWORD}
        )
        check("valid login accepted", signed_in.status_code, 200)
        if signed_in.status_code != 200:
            print("   body:", signed_in.text[:400])
            return 1
        check("session cookie retained", "access_token" in http.cookies, True)

        print("\nThe company's own data")
        overview = await http.get("/api/corporate/overview")
        check("overview readable", overview.status_code, 200)
        if overview.status_code == 200:
            body = overview.json()
            contracts = {c["contract_no"]: c for c in body.get("contracts", [])}
            seeded = contracts.get("CT-ACME-1", {})
            check("client identified", (body.get("client") or {}).get("slug"), "acme-eg")
            check("contract value visible", float(seeded.get("contract_value", 0)), 2500000.0)
            check("payment frequency visible", seeded.get("payment_frequency"), "quarterly")
            check("renewal date visible", str(seeded.get("renews_on")), "2027-01-01")
            check(
                "renewal countdown provided", isinstance(seeded.get("days_to_renewal"), int), True
            )

        members = await http.get("/api/corporate/members")
        check("members readable", members.status_code, 200)
        if members.status_code == 200:
            items = members.json().get("items", [])
            names = {m.get("full_name") for m in items}
            check("seeded employees listed", {n for (_, n) in EMPLOYEES} <= names, True)
            check("employee requests attached", any(m.get("requests") for m in items), True)

        for term, expected in (("Karim", "Karim Nabil"), ("سعاد", "سعاد فؤاد")):
            hit = await http.get("/api/corporate/members", params={"search": term})
            got = [m.get("full_name") for m in hit.json().get("items", [])]
            check(f"filter by name {term!r}", got, [expected])
        miss = await http.get("/api/corporate/members", params={"search": "zzz-no-such"})
        check("unmatched filter returns nothing", miss.json().get("items", []), [])

        check("documents readable", (await http.get("/api/corporate/documents")).status_code, 200)

        print("\nIsolation")
        staff = await http.get("/api/staff/complaints")
        check("cannot reach the staff console", staff.status_code in (401, 403), True)
        async with AsyncClient(transport=transport, base_url="https://test") as anon:
            blocked = await anon.get("/api/corporate/overview")
            check("anonymous blocked", blocked.status_code in (401, 403), True)

    print("\n" + ("ALL CORPORATE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
