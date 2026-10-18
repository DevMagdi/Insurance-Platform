"""Live end-to-end exercise of the Phase-4 integration queue on real PostgreSQL.

Runs only with the TEST_* database URLs (pytest -m integration). External
providers (SMTP/HTTP/IMAP) are replaced with in-memory fakes; every database
function, claim, finish, inbound event and AI/voice path is the production code.
NOTE: as of the Phase-5.4 integrations work these live probes were NOT run
against real Google/Microsoft/Meta/Twilio/OpenAI/ClamAV endpoints - the
sandbox has no real provider accounts. Provider behaviour is verified
through mocked transports in ``test_integration_connections.py`` and
``test_phase4_integrations.py``. To run this file, export the documented
LIVE_INTEGRATION_* credentials and execute ``pytest -m integration``.

"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import (
    Actor,
    make_engine,
    make_worker_engine,
    purpose_transaction,
    scoped_transaction,
)
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import (
    AIAnalysisRequest,
    SendComplaintEmailRequest,
    VoiceFollowupRequest,
    WhatsAppSendRequest,
)
from app.security.blobs import BlobCipher
from app.security.crypto import Keyring, idempotency_root
from app.security.integration_crypto import SecretCipher, derive_idempotency_key
from app.services import integration_providers
from app.services.integrations import IntegrationService
from app.worker import _process_integration_job, deliver_notices, poll_imap_inboxes

pytestmark = pytest.mark.integration

ADMIN = os.getenv("TEST_DATABASE_ADMIN_URL", "").replace("postgresql+psycopg://", "postgresql://")
RUNTIME = os.getenv("TEST_DIRECT_DATABASE_URL", "")


def _worker_url() -> str:
    from sqlalchemy.engine import make_url

    url = make_url(RUNTIME)
    password = os.getenv("APP_WORKER_DB_PASSWORD") or url.password or ""
    # `str(url)` masks the password as "***"; the worker login needs the real one.
    return (
        url.set(username="app_worker").set(password=password).render_as_string(hide_password=False)
    )


WORKER = _worker_url() if RUNTIME else ""

MASTER = b"l" * 32


def _skip() -> None:
    if not ADMIN or not RUNTIME:
        pytest.skip("Set TEST_DATABASE_ADMIN_URL and TEST_DIRECT_DATABASE_URL")
    if not os.getenv("APP_WORKER_DB_PASSWORD"):
        pytest.skip("Set APP_WORKER_DB_PASSWORD for the restricted app_worker login")


@pytest.fixture(scope="module")
def world() -> dict[str, Any]:
    _skip()
    unique = uuid4().hex[:10]
    company = uuid4()
    customer = uuid4()
    staff = uuid4()
    role = uuid4()
    complaint = uuid4()
    now = datetime.now(UTC).replace(microsecond=0)

    mobile = f"+201{int(unique, 16) % 10**9:09d}"
    with psycopg.connect(ADMIN, prepare_threshold=None) as conn, conn.cursor() as cur:
        # Drop leftovers from earlier runs so counts and windows stay exact.
        for statement in (
            "DELETE FROM integration_dead_letters d USING integration_jobs j "
            " WHERE d.job_id = j.id AND j.company_id IN "
            "   (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%')",
            "DELETE FROM ai_insights WHERE integration_job_id IN "
            " (SELECT id FROM integration_jobs WHERE company_id IN "
            "   (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%'))",
            "DELETE FROM integration_jobs WHERE company_id IN "
            " (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%')",
            "DELETE FROM notification_outbox WHERE recipient_customer_id IN "
            " (SELECT id FROM customers WHERE email LIKE 'customer-%@example.test')",
            "DELETE FROM audit_logs WHERE complaint_created_at IS NOT NULL AND complaint_id IN "
            " (SELECT id FROM complaints WHERE company_id IN "
            "   (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%'))",
            "DELETE FROM complaint_messages WHERE complaint_id IN "
            " (SELECT id FROM complaints WHERE company_id IN "
            "   (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%'))",
            "DELETE FROM complaint_events WHERE complaint_id IN "
            " (SELECT id FROM complaints WHERE company_id IN "
            "   (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%'))",
            "DELETE FROM call_logs WHERE complaint_id IN "
            " (SELECT id FROM complaints WHERE company_id IN "
            "   (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%'))",
            "DELETE FROM complaints WHERE company_id IN "
            " (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%')",
            "DELETE FROM complaint_keys WHERE id NOT IN (SELECT id FROM complaints)",
            "DELETE FROM integrations_config WHERE company_id IN "
            " (SELECT id FROM insurance_companies WHERE slug LIKE 'live-%')",
            "DELETE FROM insurance_companies WHERE slug LIKE 'live-%'",
        ):
            cur.execute(statement)
        conn.commit()
        cur.execute(
            "INSERT INTO insurance_companies (id, slug, name_en, name_ar, contact_emails) "
            "VALUES (%s,%s,%s,%s,%s)",
            (company, f"live-{unique}", "Live Insurer", "شركة تجريبية", ["claims@insurer.test"]),
        )
        cur.execute(
            "INSERT INTO customers(id, full_name, national_id_ciphertext, "
            "national_id_blind_index, mobile, email, whatsapp_available) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                customer,
                "Live Customer",
                b"enc" + os.urandom(16),
                os.urandom(32),
                mobile,
                f"customer-{unique}@example.test",
                True,
            ),
        )
        cur.execute(
            "INSERT INTO staff_users(id, email, full_name, password_hash, status) "
            "VALUES (%s,%s,%s,%s,'active')",
            (staff, f"{unique}-agent@example.test", "Live Agent", "no-login"),
        )
        cur.execute("INSERT INTO roles(id, name) VALUES (%s,%s)", (role, f"live-{unique}"))
        for perm in (
            "complaints.view",
            "complaints.view_attachments",
            "complaints.reply_public",
            "complaints.add_internal_note",
            "complaints.change_status",
            "integrations.send_email",
            "integrations.send_whatsapp",
            "integrations.ai_call",
            "integrations.manage",
            "audit.view",
        ):
            cur.execute(
                "INSERT INTO role_permissions(role_id, permission_code) VALUES (%s,%s)",
                (role, perm),
            )
        cur.execute("INSERT INTO user_roles(user_id, role_id) VALUES (%s,%s)", (staff, role))
        cur.execute(
            "INSERT INTO user_company_scope(user_id, company_id, all_companies) VALUES (%s,%s,%s)",
            (staff, None, True),
        )
        cur.execute("INSERT INTO user_ownership_level(user_id, level) VALUES (%s,'all')", (staff,))
        ticket = cur.execute("SELECT app_security.issue_ticket_number(%s)", (now,)).fetchone()[0]
        cur.execute(
            "INSERT INTO complaint_keys(id, created_at, ticket_no) VALUES (%s,%s,%s)",
            (complaint, now, ticket),
        )
        cur.execute(
            "INSERT INTO complaints(id, created_at, ticket_no, customer_id, company_id, "
            "insurance_type, policy_number, subject, description, status) "
            "VALUES (%s,%s,%s,%s,%s,'health','POL-LIVE-1',%s,%s,'in_review')",
            (
                complaint,
                now,
                ticket,
                customer,
                company,
                "Claim follow-up CMP " + ticket,
                "Customer requests a status update about the claim.",
            ),
        )
        conn.commit()
        cur.execute(
            "SELECT set_config('app.scope','staff',true), set_config('app.user_id',%s,true)",
            (str(staff),),
        )
        conn.commit()

    keyring = Keyring.single(MASTER)
    cipher = SecretCipher(keyring)
    idem = derive_idempotency_key(idempotency_root(keyring, None))
    blobs = BlobCipher(keyring)
    return {
        "company": company,
        "customer": customer,
        "staff": staff,
        "complaint": complaint,
        "created": now,
        "ticket": ticket,
        "cipher": cipher,
        "idem": idem,
        "blobs": blobs,
        "unique": unique,
        "mobile": mobile,
    }


@pytest.fixture(scope="module")
def engines():
    _skip()
    runtime = make_engine(RUNTIME)
    worker = make_worker_engine(WORKER)
    yield runtime, worker


def _scope(staff: UUID) -> AccessScope:
    return AccessScope(
        actor=Actor(staff, "staff"), all_companies=True, ownership=OwnershipLevel.ALL
    )


async def _configure(
    runtime, world, *, provider: str, company: UUID | None, credentials: dict, enabled: bool = True
) -> None:
    cipher: SecretCipher = world["cipher"]
    from app.services.integrations import config_aad

    ciphertext = cipher.encrypt_json(credentials, associated_data=config_aad(company, provider))
    factory = async_sessionmaker(runtime, expire_on_commit=False)
    from app.repositories.integrations import IntegrationRepository

    async with scoped_transaction(factory, Actor(world["staff"], "staff")) as db:
        repo = IntegrationRepository()
        await repo.save_config(db, company, provider, ciphertext, enabled, False)
        await db.commit()


async def test_full_email_whatsapp_ai_voice_and_inbound_lifecycle(
    engines, world, monkeypatch
) -> None:
    runtime, worker_engine = engines
    factory = async_sessionmaker(runtime, expire_on_commit=False)
    worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
    cipher: SecretCipher = world["cipher"]
    service = IntegrationService(cipher, world["idem"])
    scope = _scope(world["staff"])

    # --- configure providers exactly like the admin UI would -------------
    await _configure(
        runtime,
        world,
        provider="smtp",
        company=None,
        credentials={
            "host": "mailpit",
            "port": "1025",
            "from_email": "no-reply@insurance.test",
            "start_tls": "false",
        },
    )
    await _configure(
        runtime,
        world,
        provider="whatsapp_cloud",
        company=None,
        credentials={
            "access_token": "wa-token",
            "phone_number_id": "1234",
            "app_secret": "wa-secret",
            "verify_token": "verify-me",
        },
    )
    await _configure(runtime, world, provider="mock_llm", company=None, credentials={})
    await _configure(runtime, world, provider="mock_voice", company=None, credentials={})
    await _configure(
        runtime,
        world,
        provider="email_webhook",
        company=world["company"],
        credentials={"webhook_secret": "mail-hook-secret"},
    )

    # --- 1. staff sends complaint by email to the insurer ----------------
    sent_mails: list[Any] = []

    async def fake_smtp(message, **kwargs):  # type: ignore[no-untyped-def]
        sent_mails.append(message)
        return {}

    monkeypatch.setattr(integration_providers.aiosmtplib, "send", fake_smtp)

    async with scoped_transaction(factory, scope.actor) as db:
        result = await service.send_complaint_by_email(
            db,
            scope,
            world["complaint"],
            SendComplaintEmailRequest(locale="en"),
            "live-email-000001",
        )
        await db.commit()
    assert result["state"] == "queued", result

    # --- worker claims and delivers it ----------------------------------
    claimed = await _claim(worker_factory, 10)
    email_jobs = [row for row in claimed if row.job_kind == "send_email"]
    assert email_jobs, f"no send_email job claimed: {[r.job_kind for r in claimed]}"
    row = email_jobs[0]
    ctx = {
        "engine": worker_engine,
        "integration_cipher": cipher,
        "blobs": world["blobs"],
        "integration_idempotency_key": world["idem"],
    }
    await _process_integration_job(ctx, row)
    assert sent_mails, "SMTP provider was never invoked"
    assert any("CMP" in str(m["Subject"]) for m in sent_mails)

    async with scoped_transaction(factory, scope.actor) as db:
        state = await service.status(db, result["job_id"])
        await db.commit()
    assert state["state"] == "sent", state

    # --- 2. staff replies to the customer, free-text WhatsApp -----------
    wa_requests: list[Any] = []

    class FakeWA:
        async def send(self, request, *, idempotency_key):  # type: ignore[no-untyped-def]
            wa_requests.append(request)
            return integration_providers.ProviderResult(f"wamid.live.{world['unique']}")

    monkeypatch.setattr("app.worker.WhatsAppCloudProvider", lambda _c: FakeWA())

    async with scoped_transaction(factory, scope.actor) as db:
        reply = await service.send_customer_email(
            db,
            scope,
            world["complaint"],
            "We are on it.",
            "en",
            "live-reply-000001",
        )
        await db.commit()
    assert reply["state"] == "queued"

    # free text outside the 24h window must be refused with 409
    from fastapi import HTTPException

    async with scoped_transaction(factory, scope.actor) as db:
        with pytest.raises(HTTPException) as refused:
            await service.send_whatsapp(
                db,
                scope,
                world["complaint"],
                WhatsAppSendRequest(body="Free text update"),
                "live-wa-free-001",
            )
        await db.rollback()
    assert refused.value.status_code == 409

    # template message is allowed and goes through the worker
    async with scoped_transaction(factory, scope.actor) as db:
        wa = await service.send_whatsapp(
            db,
            scope,
            world["complaint"],
            WhatsAppSendRequest(
                body="Template update", template_name="ticket_update", parameters=["x"]
            ),
            "live-wa-templ-001",
        )
        await db.commit()
    assert wa["state"] == "queued"

    claimed = await _claim(worker_factory, 10)
    wa_jobs = [row for row in claimed if row.job_kind == "send_whatsapp"]
    assert wa_jobs, "no send_whatsapp job claimed"
    await _process_integration_job(ctx, wa_jobs[0])
    assert wa_requests and wa_requests[0].recipient == world["mobile"]
    assert wa_requests[0].template_name == "ticket_update"

    # --- 3. AI analysis -> insights -> human approval -> send ------------
    async with scoped_transaction(factory, scope.actor) as db:
        ai = await service.request_ai_analysis(
            db,
            scope,
            world["complaint"],
            AIAnalysisRequest(locale="en"),
            "live-ai-000001",
        )
        await db.commit()
    assert ai["state"] == "queued"

    claimed = await _claim(worker_factory, 10)
    ai_jobs = [row for row in claimed if row.job_kind == "ai_analyze"]
    assert ai_jobs, "no ai_analyze job claimed"
    await _process_integration_job(ctx, ai_jobs[0])

    async with scoped_transaction(factory, scope.actor) as db:
        insights = await service.list_ai_insights(db, scope, world["complaint"])
        await db.commit()
    kinds = {item["kind"] for item in insights}
    assert {"classification", "sentiment", "summary", "reply"} <= kinds, kinds
    reply_insight = next(item for item in insights if item["kind"] == "reply")
    assert reply_insight["review_status"] == "suggested"

    async with scoped_transaction(factory, scope.actor) as db:
        reviewed = await service.review_ai_insight(
            db, scope, world["complaint"], reply_insight["id"], True
        )
        await db.commit()
    assert reviewed["review_status"] == "accepted"

    async with scoped_transaction(factory, scope.actor) as db:
        approved = await service.send_approved_reply(
            db,
            scope,
            world["complaint"],
            reply_insight["id"],
            "email",
            "live-approved-001",
        )
        await db.commit()
    assert approved["state"] == "queued"

    claimed = await _claim(worker_factory, 10)
    final_email = [row for row in claimed if row.job_kind == "send_email"]
    assert final_email, "approved AI reply was never claimed"
    await _process_integration_job(ctx, final_email[0])

    # --- 4. voice follow-up ---------------------------------------------
    async with scoped_transaction(factory, Actor(world["customer"], "customer")) as db:
        customer_scope = AccessScope.for_customer(Actor(world["customer"], "customer"))
        consent = await service.record_voice_consent(db, customer_scope, world["complaint"], True)
        await db.commit()
    assert consent["enabled"] is True

    async with scoped_transaction(factory, scope.actor) as db:
        call = await service.schedule_voice_followup(
            db,
            scope,
            world["complaint"],
            VoiceFollowupRequest(
                scheduled_at=datetime.now(UTC) - timedelta(seconds=30), language="en"
            ),
            "live-voice-000001",
        )
        await db.commit()
    assert call["state"] == "queued"

    claimed = await _claim(worker_factory, 10)
    voice_jobs = [row for row in claimed if row.job_kind == "voice_followup"]
    assert voice_jobs, "no voice_followup job claimed"
    await _process_integration_job(ctx, voice_jobs[0])

    async with scoped_transaction(factory, scope.actor) as db:
        after_calls = await service.unified_inbox(db, scope, world["complaint"], include_calls=True)
        await db.commit()
    call_rows = [item for item in after_calls["items"] if item["kind"] == "call"]
    assert call_rows, "voice call missing from the unified inbox"
    assert call_rows[0]["outcome"] == "completed", call_rows

    # --- 5. inbound email webhook -> unified inbox ----------------------
    secret = "mail-hook-secret"
    document = {
        "event_id": f"live-inbound-{world['unique']}",
        "from": "customer@example.test",
        "subject": f"Re: {world['ticket']}",
        "text": "Please update my claim.",
    }
    raw = json.dumps(document).encode()
    now_ts = str(int(datetime.now(UTC).timestamp()))
    signature = hmac.new(secret.encode(), now_ts.encode() + b"." + raw, hashlib.sha256).hexdigest()

    async with purpose_transaction(factory, "webhook") as db:
        accepted = await service.receive_webhook(
            db,
            company_id=world["company"],
            provider="email_webhook",
            raw_body=raw,
            headers={"x-webhook-timestamp": now_ts, "x-webhook-signature": signature},
        )
        await db.commit()
    assert accepted["accepted"] == 1

    claimed = await _claim(worker_factory, 10)
    inbound = [row for row in claimed if row.job_kind == "inbound_email"]
    assert inbound, "no inbound_email job claimed"
    await _process_integration_job(ctx, inbound[0])

    async with scoped_transaction(factory, scope.actor) as db:
        inbox = await service.unified_inbox(db, scope, world["complaint"], include_calls=True)
        await db.commit()
    bodies = [item.get("body") for item in inbox["items"] if item["kind"] == "message"]
    assert any("Please update my claim" in str(b) for b in bodies), bodies

    # --- 6. WhatsApp inbound + status callbacks -------------------------
    wa_doc = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": f"wamid.in.{world['unique']}",
                                    "from": world["mobile"].lstrip("+"),
                                    "text": {"body": f"Hello about {world['ticket']}"},
                                }
                            ],
                            "statuses": [],
                        }
                    }
                ]
            }
        ],
    }
    wa_raw = json.dumps(wa_doc).encode()
    wa_sig = hmac.new(b"wa-secret", wa_raw, hashlib.sha256).hexdigest()
    async with purpose_transaction(factory, "webhook") as db:
        wa_in = await service.receive_webhook(
            db,
            company_id=world["company"],
            provider="whatsapp_cloud",
            raw_body=wa_raw,
            headers={"x-hub-signature-256": f"sha256={wa_sig}"},
        )
        await db.commit()
    assert wa_in["accepted"] == 1
    claimed = await _claim(worker_factory, 10)
    wa_inbound = [row for row in claimed if row.job_kind == "inbound_whatsapp"]
    assert wa_inbound, "no inbound_whatsapp job claimed"
    await _process_integration_job(ctx, wa_inbound[0])

    # --- 7. email delivery-status callback ------------------------------
    provider_msg_id = str(sent_mails[0]["Message-ID"])
    status_doc = [
        {
            "event": "delivered",
            "sg_message_id": provider_msg_id,
            "event_id": f"live-status-{world['unique']}",
            "timestamp": "1",
        }
    ]
    st_raw = json.dumps(status_doc).encode()
    now_ts2 = str(int(datetime.now(UTC).timestamp()))
    st_sig = hmac.new(secret.encode(), now_ts2.encode() + b"." + st_raw, hashlib.sha256).hexdigest()
    async with purpose_transaction(factory, "webhook") as db:
        st_in = await service.receive_webhook(
            db,
            company_id=world["company"],
            provider="email_webhook",
            raw_body=st_raw,
            headers={"x-webhook-timestamp": now_ts2, "x-webhook-signature": st_sig},
        )
        await db.commit()
    assert st_in["accepted"] == 1
    claimed = await _claim(worker_factory, 10)
    st_jobs = [row for row in claimed if row.job_kind == "email_status"]
    assert st_jobs, "no email_status job claimed"
    await _process_integration_job(ctx, st_jobs[0])

    async with scoped_transaction(factory, scope.actor) as db:
        after_status = await service.unified_inbox(
            db, scope, world["complaint"], include_calls=True
        )
        await db.commit()
    sent_items = [
        item
        for item in after_status["items"]
        if item["kind"] == "message" and item["direction"] == "outbound"
    ]
    assert any(item["delivery_status"] == "delivered" for item in sent_items), sent_items

    # --- 8. IMAP polling enqueues inbound mail --------------------------
    class FakeIMAPProvider:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        async def fetch_unseen(self, *, limit: int = 20):  # type: ignore[no-untyped-def]
            return [
                integration_providers.IncomingEmail(
                    "1",
                    f"imap-live-{world['unique']}",
                    f"customer-{world['unique']}@example.test",
                    f"Re: {world['ticket']}",
                    "IMAP says hello",
                )
            ]

        async def mark_seen(self, sequence_id: str) -> None:
            return None

    monkeypatch.setattr(
        "app.worker.INBOUND_EMAIL_PROVIDER_CLASSES",
        {
            "imap": FakeIMAPProvider,
            "gmail_oauth": FakeIMAPProvider,
            "outlook_oauth": FakeIMAPProvider,
        },
    )
    await _configure(
        runtime,
        world,
        provider="imap",
        company=world["company"],
        credentials={"host": "imap.test", "username": "u", "password": "p"},
    )

    async def worker_query(engine, sql, params):  # type: ignore[no-untyped-def]
        async with worker_factory() as db, db.begin():
            await db.execute(text("SELECT set_config('app.scope','worker',true)"))
            result = await db.execute(text(sql), params)
            return list(result.all())

    monkeypatch.setattr("app.worker._worker_query", worker_query)
    summary = await poll_imap_inboxes(
        {
            "engine": worker_engine,
            "integration_cipher": cipher,
            "integration_idempotency_key": world["idem"],
        }
    )
    assert summary == "queued 1 inbound email message(s)", summary

    # --- 9. customer notices honor preferences --------------------------
    from app.services.providers import SMTPDelivery

    class FakeDelivery(SMTPDelivery):
        sent: list[Any] = []

        async def _mail(self, address, subject, body):  # type: ignore[no-untyped-def]
            FakeDelivery.sent.append((address, subject))

    settings = SimpleNamespace(
        smtp_host="mailpit",
        smtp_port=1025,
        smtp_from="no-reply@insurance.test",
        smtp_username=None,
        smtp_password=None,
        smtp_start_tls=False,
        dev_sms_mailbox=True,
    )
    with psycopg.connect(ADMIN, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_outbox(id, complaint_id, complaint_created_at, "
            "recipient_customer_id, kind, payload) "
            "VALUES (%s,%s,%s,%s,'status_changed',%s)",
            (
                uuid4(),
                world["complaint"],
                world["created"],
                world["customer"],
                json.dumps({"ticket_no": world["ticket"], "status": "in_review", "locale": "en"}),
            ),
        )
        conn.commit()
    result = await deliver_notices({"engine": worker_engine, "delivery": FakeDelivery(settings)})
    assert "processed" in result
    assert FakeDelivery.sent, "notice was never delivered"


async def _claim(worker_factory, limit: int) -> list[Any]:
    rows: list[Any] = []
    async with worker_factory() as db, db.begin():
        await db.execute(text("SELECT set_config('app.scope','worker',true)"))
        result = await db.execute(
            text("SELECT * FROM app_security.claim_integration_jobs(:limit)"), {"limit": limit}
        )
        rows = list(result.all())
    return rows
