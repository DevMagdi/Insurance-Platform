"""Part 4 (live): staff notices for customer activity + the live staff console.

Run against the running local stack (``make local-up`` + ``make local-seed``)
with the seeded demo staff. It proves, over real HTTP against real PostgreSQL
with RLS enabled:

* S1 - a staff session is established (forced password rotation included).
* S2 - a customer complaint **and** a customer message land in the staff
  notification list with the complaint link the UI navigates to (the Arabic
  "open that complaint's chat" requirement).
* S3 - the open-ticket SSE stream and the complaints-queue WebSocket push the
  new data without any manual refresh.
* S4 - the cheap directory revision signatures answer for a global
  administrator, stay stable when nothing changed, and stay closed (403) for
  staff without the permission.
* S5 - the notice fan-out is scoped: a customer message on a complaint inside a
  scoped agent's AccessScope reaches that agent, and no agent ever receives a
  notice for a complaint they cannot read (deny-wins, no cross-tenant leak).

The customer writes run through the application's own repository code inside an
RLS-scoped transaction, exactly as the customer portal does it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import psycopg
import websockets
from qa_lib import QAClient, Recorder


def as_uuid(value: object) -> UUID:
    """psycopg already decodes ``uuid`` columns, the API returns strings."""
    return value if isinstance(value, UUID) else UUID(str(value))


RECORDER = Recorder()
RECORDER.section = "S1 staff session"

ADMIN_EMAIL = "admin@demo.example"
AGENT_EMAIL = "agent@demo.example"
DEMO_PASSWORD = "DemoStaffPass2026!"
ROTATED_PASSWORD = "DemoStaffPass2026!x"
WS_URL = "ws://127.0.0.1:8000/api/staff/complaints/stream"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def env_value(key: str) -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip()
    raise SystemExit(f"{key} is missing from .env - run `make init`")


def admin_dsn() -> str:
    return (
        f"host=127.0.0.1 port=5432 user={env_value('POSTGRES_USER')}"
        f" password={env_value('POSTGRES_PASSWORD')} dbname={env_value('POSTGRES_DB')}"
    )


def runtime_dsn() -> str:
    return (
        f"host=127.0.0.1 port=5432 user=app_runtime"
        f" password={env_value('APP_DB_PASSWORD')} dbname={env_value('POSTGRES_DB')}"
    )


def staff_session(email: str, password: str = ROTATED_PASSWORD) -> QAClient:
    """Log a demo staff member in, rotating the seeded password when required."""
    client = QAClient()
    login = client.post(
        "/api/auth/staff/login", json_body={"email": email, "password": password}
    )
    if login.status != 200:
        login = client.post(
            "/api/auth/staff/login",
            json_body={"email": email, "password": DEMO_PASSWORD},
        )
    step = login.json().get("next_step")
    if step == "password_change":
        changed = client.post(
            "/api/auth/staff/password/change",
            json_body={
                "challenge": login.json()["challenge"],
                "password": ROTATED_PASSWORD,
            },
        )
        RECORDER.check(
            f"forced password change accepted for {email}",
            changed.status == 200,
            "200",
            changed.text[:80],
        )
        login = client.post(
            "/api/auth/staff/login",
            json_body={"email": email, "password": ROTATED_PASSWORD},
        )
        step = login.json().get("next_step")
    RECORDER.check(
        f"staff login as {email}",
        step == "authenticated",
        "authenticated",
        login.text[:100],
    )
    identity = client.get("/api/me")
    RECORDER.check(
        f"session identity resolves for {email}",
        identity.status == 200 and identity.json()["kind"] == "staff",
        "200 staff",
        identity.text[:80],
    )
    return client


async def customer_actions() -> dict[str, str]:
    """A customer replies to an open ticket and files a new complaint."""
    from app.db import Actor, make_engine, scoped_transaction
    from app.domain.access import AccessScope
    from app.domain.schemas import ComplaintCreate
    from app.repositories.complaints import ComplaintRepository
    from sqlalchemy.ext.asyncio import async_sessionmaker

    with psycopg.connect(admin_dsn()) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.id, c.customer_id, c.company_id FROM complaints c"
            " WHERE c.status <> 'resolved' ORDER BY c.created_at DESC LIMIT 1"
        )
        complaint_id, customer_id, company_id = cursor.fetchone()

    engine = make_engine(
        f"postgresql+asyncpg://app_runtime:{env_value('APP_DB_PASSWORD')}"
        f"@127.0.0.1:5432/{env_value('POSTGRES_DB')}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = ComplaintRepository()
    scope = AccessScope.for_customer(Actor(as_uuid(customer_id), "customer"))

    async with scoped_transaction(
        factory, Actor(as_uuid(customer_id), "customer")
    ) as db:
        parent = await repo.get(db, scope, as_uuid(complaint_id))
        assert parent is not None, "the customer cannot read the complaint (RLS)"
        await repo.reply(db, scope, parent, "رسالة تحقق مباشرة من العميل")
        created = await repo.create(
            db,
            customer_id=as_uuid(customer_id),
            item=ComplaintCreate.model_validate(
                {
                    "national_id": "29901010112345",
                    "mobile": "+201000000000",
                    "company_id": str(company_id),
                    "insurance_type": "health",
                    "policy_number": "POL-LIVE-1",
                    "subject": "شكوى تحقق مباشر",
                    "description": "وصف تحقق مباشر من إنشاء الشكوى.",
                }
            ),
            policy_id=None,
            idempotency=SimpleNamespace(
                complaint_id=None, complaint_created_at=None, ticket_no=None
            ),
        )
        await db.commit()
    await engine.dispose()
    return {
        "complaint_id": str(complaint_id),
        "created_id": str(created.id),
        "ticket_no": created.ticket_no,
    }


async def scoped_customer_reply(complaint_id: str) -> None:
    """A customer replies on a complaint inside a scoped agent's AccessScope."""
    from app.db import Actor, make_engine, scoped_transaction
    from app.domain.access import AccessScope
    from app.repositories.complaints import ComplaintRepository
    from sqlalchemy.ext.asyncio import async_sessionmaker

    with psycopg.connect(admin_dsn()) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.customer_id FROM complaints c WHERE c.id = %s", (complaint_id,)
        )
        (customer_id,) = cursor.fetchone()

    engine = make_engine(
        f"postgresql+asyncpg://app_runtime:{env_value('APP_DB_PASSWORD')}"
        f"@127.0.0.1:5432/{env_value('POSTGRES_DB')}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = ComplaintRepository()
    scope = AccessScope.for_customer(Actor(as_uuid(customer_id), "customer"))
    async with scoped_transaction(
        factory, Actor(as_uuid(customer_id), "customer")
    ) as db:
        parent = await repo.get(db, scope, as_uuid(complaint_id))
        assert parent is not None
        await repo.reply(db, scope, parent, "رسالة تحقق للنطاق المصرح")
        await db.commit()
    await engine.dispose()


def queue_snapshot(client: QAClient) -> dict[str, object]:
    """Read one frame from the staff complaints queue WebSocket."""
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    query = "page=1&page_size=50&sort_by=created_at&sort_dir=desc"

    async def read() -> dict[str, object]:
        async with websockets.connect(
            f"{WS_URL}?{query}",
            # The endpoint only accepts a same-origin client: the Origin netloc
            # must equal Host, exactly like the browser sends it.
            additional_headers={"Cookie": cookie, "Origin": "http://127.0.0.1:8000"},
            open_timeout=10,
        ) as socket:
            return json.loads(await asyncio.wait_for(socket.recv(), 10))

    return asyncio.run(read())


def ticket_signature(client: QAClient, complaint_id: str) -> str:
    """Read the first live frame of the open-ticket SSE stream."""
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    signature = ""
    # The stream stays open, so read it incrementally and stop after the first
    # live frame; the Secure-only cookies are sent explicitly (plain HTTP).
    with client.http.stream(
        "GET",
        f"/api/staff/complaints/{complaint_id}/stream",
        headers={"Cookie": cookie},
        timeout=15.0,
    ) as response:
        if response.status_code != 200:
            return ""
        for line in response.iter_lines():
            if line.startswith("data:"):
                signature = json.loads(line[5:]).get("signature", "")
                break
    return signature


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    RECORDER.section = "S2 customer activity reaches staff notifications"
    admin = staff_session(ADMIN_EMAIL)

    notices = admin.get("/api/me/notifications").json()
    RECORDER.check(
        "staff notification list is readable",
        isinstance(notices, list),
        "list",
        str(len(notices)),
    )

    actions = asyncio.run(customer_actions())
    RECORDER.check(
        "customer message + new complaint stored through the app repository",
        bool(actions["ticket_no"]),
        actions["ticket_no"],
    )

    notices = admin.get("/api/me/notifications").json()
    reply_notices = [
        item
        for item in notices
        if item["kind"] == "reply" and item["complaint_id"] == actions["complaint_id"]
    ]
    RECORDER.check(
        "customer message appears in staff notifications with the complaint link",
        bool(reply_notices),
        "notice carries complaint_id + ticket_no",
        json.dumps(reply_notices[0]["payload"], ensure_ascii=False)[:160]
        if reply_notices
        else "-",
    )
    RECORDER.check(
        "new complaint appears in staff notifications",
        any(item["complaint_id"] == actions["created_id"] for item in notices),
        actions["ticket_no"],
    )

    timeline = admin.get(
        f"/api/staff/complaints/{actions['complaint_id']}/timeline"
    ).json()
    RECORDER.check(
        "staff timeline contains the new customer message",
        any(m["body"] == "رسالة تحقق مباشرة من العميل" for m in timeline["messages"]),
        "message readable by staff",
    )

    # ------------------------------------------------------------------ #
    RECORDER.section = "S3 live streams (no manual refresh)"
    RECORDER.check(
        "staff ticket SSE stream opens",
        ticket_signature(admin, actions["complaint_id"]) != "",
    )
    signature = ticket_signature(admin, actions["complaint_id"])
    RECORDER.check(
        "staff ticket SSE pushes a live signature", bool(signature), signature[:16]
    )
    snapshot = queue_snapshot(admin)
    RECORDER.check(
        "staff complaints queue WebSocket pushes a scoped snapshot",
        isinstance(snapshot.get("items"), list) and int(snapshot.get("total", 0)) > 0,
        "items + total",
        f"total={snapshot.get('total')}",
    )
    RECORDER.check(
        "the new complaint is visible in the live queue snapshot",
        any(item["id"] == actions["created_id"] for item in snapshot["items"]),
        actions["ticket_no"],
    )

    # ------------------------------------------------------------------ #
    RECORDER.section = "S4 live directory revision signatures"
    users_revision = admin.get("/api/staff/users/revision").json()["revision"]
    customers_revision = admin.get("/api/staff/customers/revision").json()["revision"]
    RECORDER.check(
        "staff directory revision signature returned",
        bool(users_revision),
        "opaque signature",
    )
    RECORDER.check(
        "customer directory revision signature returned",
        bool(customers_revision),
        "opaque signature",
    )
    stable = admin.get("/api/staff/users/revision").json()["revision"]
    RECORDER.check(
        "revision signature is stable while nothing changed",
        stable == users_revision,
        stable,
    )
    anonymous = QAClient()  # a session-less client cannot read it either
    RECORDER.check(
        "anonymous client cannot read the customer directory revision",
        anonymous.get("/api/staff/customers/revision").status in (401, 403),
        "401/403",
    )

    # ------------------------------------------------------------------ #
    RECORDER.section = "S5 scoped fan-out and cross-tenant isolation"
    agent = staff_session(AGENT_EMAIL)
    agent_notices = agent.get("/api/me/notifications").json()
    scoped_complaint = next(
        (item["complaint_id"] for item in agent_notices if item.get("complaint_id")),
        None,
    )
    RECORDER.check(
        "scoped agent already holds complaint notices",
        scoped_complaint is not None,
        f"{len(agent_notices)} notices",
    )

    customers_revision_agent = agent.get("/api/staff/customers/revision")
    RECORDER.check(
        "non-admin staff cannot read the customer directory revision (403)",
        customers_revision_agent.status == 403,
        "403",
        customers_revision_agent.text[:60],
    )
    users_revision_agent = agent.get("/api/staff/users/revision")
    RECORDER.check(
        "agent without users.view cannot read the staff directory revision (403)",
        users_revision_agent.status == 403,
        "403",
        users_revision_agent.text[:60],
    )

    before_scoped = [
        item
        for item in agent_notices
        if item["complaint_id"] == scoped_complaint and item["kind"] == "reply"
    ]
    asyncio.run(scoped_customer_reply(scoped_complaint))
    after_notices = agent.get("/api/me/notifications").json()
    after_scoped = [
        item
        for item in after_notices
        if item["complaint_id"] == scoped_complaint and item["kind"] == "reply"
    ]
    RECORDER.check(
        "a customer message on an in-scope complaint reaches the scoped agent",
        len(after_scoped) == len(before_scoped) + 1,
        f"{len(before_scoped)} -> {len(after_scoped)}",
    )

    out_of_scope = []
    for item in after_notices:
        complaint = item.get("complaint_id")
        if complaint is None:
            continue
        readable = agent.get(f"/api/staff/complaints/{complaint}/timeline")
        if readable.status != 200:
            out_of_scope.append((complaint, readable.status))
    RECORDER.check(
        "every notice an agent holds points at a complaint inside their AccessScope",
        not out_of_scope,
        f"checked {len(after_notices)} notices",
        str(out_of_scope[:3]),
    )

    RECORDER.save(Path(__file__).parent / "results_staff_notices.json")
    failed = [item for item in RECORDER.results if item["status"] == "FAIL"]
    if failed:
        print("FAILED:", [item["name"] for item in failed])
        return 1
    print(f"\n{len(RECORDER.results)}/{len(RECORDER.results)} live checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
