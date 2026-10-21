"""End-to-end request-intake check against a live, migrated database.

Resets the channel-sourced rows, then proves the behaviour the brokerage
depends on: a WhatsApp message with no quoted ticket number opens a request,
a follow-up from the same mobile joins it, a replayed webhook is a no-op,
e-mail intake works, a message carrying no policy or card reference is still
accepted, and attachment bytes are encrypted at rest.

    ./.venv/bin/python backend/tests/manual/intake_e2e_check.py
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

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.security.blobs import BlobCipher  # noqa: E402
from app.security.crypto import build_keyring  # noqa: E402
from app.services.intake import InboundEvent, InboundMedia, RequestIntakeService  # noqa: E402

PW = env["POSTGRES_PASSWORD"]
DB = env["POSTGRES_DB"]
WORKER_URL = f"postgresql+psycopg://app_worker:{env['APP_WORKER_DB_PASSWORD']}@127.0.0.1:5432/{DB}"
ADMIN_URL = f"postgresql+psycopg://postgres:{PW}@127.0.0.1:5432/{DB}"
CHANNELS = "('whatsapp','email','phone','sms')"
failures: list[str] = []


def check(label: str, got: object, want: object = True) -> None:
    ok = got == want
    print(
        f"  {'PASS' if ok else 'FAIL'}  {label:44} got={got!r}" + ("" if ok else f" want={want!r}")
    )
    if not ok:
        failures.append(label)


async def main() -> int:
    admin = create_async_engine(ADMIN_URL)
    async with admin.begin() as conn:
        for statement in (
            "DELETE FROM public.request_intake_events",
            f"DELETE FROM public.attachments WHERE complaint_id IN (SELECT id FROM"
            f" public.complaints WHERE intake_source IN {CHANNELS})",
            f"DELETE FROM public.complaint_messages WHERE complaint_id IN (SELECT id FROM"
            f" public.complaints WHERE intake_source IN {CHANNELS})",
            f"DELETE FROM public.complaints WHERE intake_source IN {CHANNELS}",
            "DELETE FROM public.customers WHERE mobile LIKE '+2010999%'"
            " OR mobile LIKE '+2011888%' OR mobile LIKE '+2012777%'",
        ):
            await conn.execute(text(statement))
        company = await conn.scalar(text("SELECT id FROM public.insurance_companies LIMIT 1"))

    keyring = build_keyring(env["APP_ENCRYPTION_KEY"], int(env.get("APP_ENCRYPTION_KEY_ID", 1)))
    intake = RequestIntakeService(BlobCipher(keyring))
    worker = create_async_engine(WORKER_URL)

    async def ingest(event: InboundEvent) -> object:
        async with worker.begin() as conn:
            await conn.execute(text("SELECT set_config('app.scope','worker',false)"))
            session = AsyncSession(bind=conn, expire_on_commit=False)
            return await intake.ingest(session, event, company_id=company)

    print("\nWhatsApp intake (the brokerage's primary channel)")
    first = await ingest(
        InboundEvent(
            channel="whatsapp",
            external_event_id="ev-1",
            sender="+201099900001",
            sender_name="منى سعيد",
            body="السلام عليكم، اسمي منى سعيد ومحتاجة موافقة على أشعة. رقم الكارت: CARD-77881",
            media=[
                InboundMedia("m1", "image/jpeg", "card.jpg", "الكارت", b"\xff\xd8\xff\xe0JPEGDATA")
            ],
        )
    )
    check("new request created", first.state, "created")
    check("ticket minted", bool(first.ticket_no and first.ticket_no.startswith("REQ-")), True)
    check("attachment stored", first.attachments, 1)

    follow_up = await ingest(
        InboundEvent(
            channel="whatsapp", external_event_id="ev-2", sender="201099900001", body="في تحديث؟"
        )
    )
    check("follow-up joins same request", follow_up.complaint_id == first.complaint_id, True)
    check("follow-up is linked, not new", follow_up.state, "linked")

    replay = await ingest(
        InboundEvent(
            channel="whatsapp", external_event_id="ev-1", sender="+201099900001", body="replay"
        )
    )
    check("replayed webhook short-circuits", replay.duplicate, True)
    check("replay returns the original request", replay.complaint_id == first.complaint_id, True)

    bare = await ingest(
        InboundEvent(
            channel="whatsapp",
            external_event_id="ev-3",
            sender="+201188800002",
            sender_name="Ahmed",
            body="محتاج اعرف الشبكة الطبية",
        )
    )
    check("message with no reference accepted", bare.state, "created")

    print("\nEmail intake (rare, but must not be dropped)")
    mail = await ingest(
        InboundEvent(
            channel="email",
            external_event_id="msg-9@x.example",
            sender="+201277700003",
            subject="طلب موافقة",
            body="وثيقة رقم POL-5541 محتاج موافقة",
            media=[InboundMedia("p1", "application/pdf", "report.pdf", None, b"%PDF-1.4 data")],
        )
    )
    check("email request created", mail.state, "created")
    check("email attachment stored", mail.attachments, 1)

    print("\nFailure handling")
    broken = await ingest(
        InboundEvent(
            channel="whatsapp", external_event_id="ev-bad", sender="not-a-phone", body="hello"
        )
    )
    check("unparseable sender recorded, not lost", broken.state in {"created", "failed"}, True)

    print("\nPersisted state")
    async with admin.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.ticket_no, c.intake_source, c.policy_number, c.card_number,"
                    " cu.mobile, cu.full_name,"
                    " (SELECT count(*) FROM public.attachments a"
                    "  WHERE a.complaint_id = c.id) att,"
                    " (SELECT count(*) FROM public.complaint_messages m"
                    "  WHERE m.complaint_id = c.id) msg"
                    " FROM public.complaints c JOIN public.customers cu ON cu.id = c.customer_id"
                    " WHERE c.intake_source IN ('whatsapp','email') ORDER BY c.created_at"
                )
            )
        ).all()
        for row in rows:
            print(
                f"    {row.ticket_no} {row.intake_source:9}"
                f" policy={row.policy_number!s:9} card={row.card_number!s:12}"
                f" {row.mobile} {row.full_name!r} att={row.att} msgs={row.msg}"
            )
        check("exactly 3 requests (no duplicates)", len(rows), 3)
        check("name parsed from Arabic text", rows[0].full_name, "منى سعيد")
        check("mobile normalised to E.164", rows[0].mobile, "+201099900001")
        check("first request has both messages", rows[0].msg, 2)

        inbox = (
            await conn.execute(
                text("SELECT state, count(*) FROM public.request_intake_events GROUP BY state")
            )
        ).all()
        print(f"    intake inbox: {sorted((str(s), n) for s, n in inbox)}")
        # Six ingests but five distinct event ids: the replay reuses its row.
        check("every distinct event audited", sum(n for _, n in inbox), 5)

        leak = await conn.scalar(
            text(
                "SELECT bool_or(position('JPEGDATA' in encode(content,'escape')) > 0)"
                " FROM public.attachments"
            )
        )
        check("attachment bytes encrypted at rest", bool(leak), False)

    await worker.dispose()
    await admin.dispose()
    print("\n" + ("ALL INTAKE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
