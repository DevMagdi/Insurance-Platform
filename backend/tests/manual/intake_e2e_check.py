"""Deterministic end-to-end intake test. Resets channel data, then asserts."""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
env = {}
for line in (pathlib.Path(__file__).resolve().parents[3] / ".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
os.environ.update(env)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.security.blobs import BlobCipher
from app.security.crypto import build_keyring
from app.services.intake import InboundEvent, InboundMedia, RequestIntakeService

PW, DB = env["POSTGRES_PASSWORD"], env["POSTGRES_DB"]
WPW = env.get("POSTGRES_APP_WORKER_PASSWORD") or env.get("APP_WORKER_PASSWORD") or PW
failures = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:46} got={got!r}" + ("" if ok else f" want={want!r}"))
    if not ok: failures.append(label)

async def main():
    admin = create_async_engine(f"postgresql+psycopg://postgres:{PW}@127.0.0.1:5432/{DB}")
    async with admin.begin() as c:
        await c.execute(text("DELETE FROM public.request_intake_events"))
        await c.execute(text("DELETE FROM public.attachments WHERE complaint_id IN (SELECT id FROM public.complaints WHERE intake_source IN ('whatsapp','email','phone','sms'))"))
        await c.execute(text("DELETE FROM public.complaint_messages WHERE complaint_id IN (SELECT id FROM public.complaints WHERE intake_source IN ('whatsapp','email','phone','sms'))"))
        await c.execute(text("DELETE FROM public.complaints WHERE intake_source IN ('whatsapp','email','phone','sms')"))
        await c.execute(text("DELETE FROM public.customers WHERE mobile LIKE '+2010999%' OR mobile LIKE '+2011888%' OR mobile LIKE '+2012777%'"))
        company = await c.scalar(text("SELECT id FROM public.insurance_companies LIMIT 1"))

    intake = RequestIntakeService(BlobCipher(build_keyring(env["APP_ENCRYPTION_KEY"], int(env.get("APP_ENCRYPTION_KEY_ID", 1)))))
    worker = create_async_engine(f"postgresql+psycopg://app_worker:{WPW}@127.0.0.1:5432/{DB}")
    async def ingest(e):
        async with worker.begin() as conn:
            await conn.execute(text("SELECT set_config('app.scope','worker',false)"))
            return await intake.ingest(AsyncSession(bind=conn, expire_on_commit=False), e, company_id=company)

    print("\nWhatsApp intake (the brokerage's primary channel)")
    a = await ingest(InboundEvent("whatsapp", "ev-1", "+201099900001", sender_name="منى سعيد",
        body="السلام عليكم، اسمي منى سعيد ومحتاجة موافقة على أشعة رنين. رقم الكارت: CARD-77881",
        media=[InboundMedia("m1", "image/jpeg", "card.jpg", "الكارت", b"\xff\xd8\xff\xe0JPEGDATA")]))
    check("new request created", a.state, "created")
    check("ticket minted", bool(a.ticket_no and a.ticket_no.startswith("REQ-")), True)
    check("attachment stored", a.attachments, 1)

    b = await ingest(InboundEvent("whatsapp", "ev-2", "201099900001", body="في تحديث؟"))
    check("follow-up joins same request", b.complaint_id == a.complaint_id, True)
    check("follow-up is linked not new", b.state, "linked")

    c2 = await ingest(InboundEvent("whatsapp", "ev-1", "+201099900001", body="replay"))
    check("replayed webhook short-circuits", c2.duplicate, True)
    check("replay returns original request", c2.complaint_id == a.complaint_id, True)

    d = await ingest(InboundEvent("whatsapp", "ev-3", "+201188800002", sender_name="Ahmed",
        body="محتاج اعرف الشبكة الطبية"))
    check("message with no reference accepted", d.state, "created")

    print("\nEmail intake (rare, but must not be dropped)")
    e = await ingest(InboundEvent("email", "msg-9@x.example", "+201277700003", subject="طلب موافقة",
        body="وثيقة رقم POL-5541 محتاج موافقة",
        media=[InboundMedia("p1", "application/pdf", "report.pdf", None, b"%PDF-1.4 data")]))
    check("email request created", e.state, "created")
    check("email attachment stored", e.attachments, 1)

    print("\nFailure handling")
    f = await ingest(InboundEvent("whatsapp", "ev-bad", "not-a-phone-number", body="hello"))
    check("unparseable sender recorded, not lost", f.state in {"created", "failed"}, True)

    print("\nPersisted state")
    async with admin.begin() as c:
        rows = (await c.execute(text(
            "SELECT c.ticket_no, c.intake_source, c.policy_number, c.card_number, cu.mobile, cu.full_name,"
            " (SELECT count(*) FROM public.attachments a WHERE a.complaint_id=c.id) att,"
            " (SELECT count(*) FROM public.complaint_messages m WHERE m.complaint_id=c.id) msg"
            " FROM public.complaints c JOIN public.customers cu ON cu.id=c.customer_id"
            " WHERE c.intake_source IN ('whatsapp','email') ORDER BY c.created_at"))).all()
        for r in rows:
            print(f"    {r.ticket_no} {r.intake_source:9} policy={str(r.policy_number):9} card={str(r.card_number):12} {r.mobile} {r.full_name!r} att={r.att} msgs={r.msg}")
        check("exactly 3 requests (no duplicates)", len(rows), 3)
        check("name parsed from Arabic text", rows[0].full_name, "منى سعيد")
        check("mobile normalised to E.164", rows[0].mobile, "+201099900001")
        check("first request has 2 messages", rows[0].msg, 2)
        inbox = (await c.execute(text("SELECT state, count(*) FROM public.request_intake_events GROUP BY state ORDER BY 1"))).all()
        print(f"    intake inbox: {[(s, n) for s, n in inbox]}")
        check("every distinct event audited", sum(n for _, n in inbox), 5)  # the replay reuses its row
        leak = await c.scalar(text("SELECT bool_or(position('JPEGDATA' in encode(content,'escape')) > 0) FROM public.attachments"))
        check("attachment bytes encrypted at rest", bool(leak), False)

    await worker.dispose(); await admin.dispose()
    print("\n" + ("ALL INTAKE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0

sys.exit(asyncio.run(main()))
