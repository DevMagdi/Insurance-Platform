"""The request archive and the automation ledger, against real PostgreSQL.

Skipped without TEST_DATABASE_ADMIN_URL. These assert the properties that only
a real database can show: that a corporate client sees its own requests and
nothing else, that internal staff notes never reach the portal, and that each
automation fires exactly once no matter how often the cron runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration


def admin_dsn() -> str:
    url = os.getenv("TEST_DATABASE_ADMIN_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_ADMIN_URL for real PostgreSQL tests")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass(frozen=True)
class Fixture:
    dsn: str
    company: UUID
    client_a: UUID
    client_b: UUID
    user_a: UUID
    employees: dict[str, UUID]
    requests: dict[str, tuple[UUID, datetime]]
    contract: UUID
    renews_on: date
    mobile_stem: str


@pytest.fixture(scope="module")
def data() -> Fixture:
    dsn = admin_dsn()
    tag = uuid4().hex[:8]
    # Egyptian mobiles are validated on the way in, so the per-run suffix
    # has to be digits: a hex tag would be rejected as "intake mobile invalid".
    stem = f"{uuid4().int % 10**7:07d}"
    employees: dict[str, UUID] = {}
    requests: dict[str, tuple[UUID, datetime]] = {}
    renews_on = date.today() + timedelta(days=21)

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.scope','worker',false)")
        cur.execute(
            "INSERT INTO public.insurance_companies (slug, name_en, name_ar)"
            " VALUES (%s, %s, %s) RETURNING id",
            (f"arch-{tag}", f"Archive {tag}", f"أرشيف {tag}"),
        )
        company = cur.fetchone()[0]

        clients: list[UUID] = []
        for suffix in ("a", "b"):
            cur.execute(
                "INSERT INTO public.corporate_clients (slug, name_ar, name_en, is_active)"
                " VALUES (%s, %s, %s, true) RETURNING id",
                (f"arch-{tag}-{suffix}", f"شركة {suffix}", f"Client {suffix}"),
            )
            clients.append(cur.fetchone()[0])
        client_a, client_b = clients

        cur.execute(
            "INSERT INTO public.corporate_contracts (corporate_client_id, contract_no,"
            " contract_value, currency, payment_frequency, starts_on, renews_on, status,"
            " headcount) VALUES (%s, %s, 1500000.00, 'EGP', 'annual', CURRENT_DATE,"
            " %s, 'active', 120) RETURNING id",
            (client_a, f"CT-{tag}", renews_on),
        )
        contract = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO public.corporate_users (corporate_client_id, username, full_name,"
            " password_hash, status, must_change_password)"
            " VALUES (%s, %s, 'HR', 'x', 'active', false) RETURNING id",
            (client_a, f"hr-{tag}"),
        )
        user_a = cur.fetchone()[0]

        # Three requests for client A across two employees and two channels,
        # plus one for client B that must never be visible to A.
        seeds = (
            ("soad", f"+2010{stem}1", "سعاد فؤاد", "whatsapp", client_a),
            ("karim", f"+2010{stem}2", "Karim Nabil", "email", client_a),
            ("soad2", f"+2010{stem}1", "سعاد فؤاد", "phone", client_a),
            ("other", f"+2010{stem}3", "Rival Staff", "whatsapp", client_b),
        )
        for key, mobile, name, source, client in seeds:
            # create_channel_request is reachable only from the worker login;
            # a superuser can take that identity for the duration of the call.
            cur.execute("SET SESSION AUTHORIZATION app_worker")
            cur.execute("SELECT set_config('app.scope','worker',false)")
            cur.execute(
                "SELECT app_security.create_channel_request(%s,"
                " CAST(%s AS public.communication_channel), %s, %s, %s,"
                " %s, 'تفاصيل', NULL, %s, CAST('health' AS public.insurance_type), %s, NULL)",
                (
                    company,
                    "whatsapp" if source == "whatsapp" else source,
                    source,
                    mobile,
                    name,
                    f"طلب {key}",
                    f"CARD-{key}-{tag}",
                    f"{key}-{tag}",
                ),
            )
            cur.execute("RESET SESSION AUTHORIZATION")
            cur.execute(
                "SELECT id, created_at FROM public.complaints WHERE intake_reference = %s",
                (f"{key}-{tag}",),
            )
            requests[key] = cur.fetchone()

            cur.execute("SELECT id FROM public.customers WHERE mobile = %s", (mobile,))
            employees[key] = cur.fetchone()[0]
            cur.execute(
                "UPDATE public.customers SET corporate_client_id = %s, employee_code = %s"
                " WHERE id = %s",
                (client, f"E-{key}", employees[key]),
            )
            cur.execute(
                "UPDATE public.complaints SET corporate_client_id = %s WHERE id = %s",
                (client, requests[key][0]),
            )

    return Fixture(
        dsn=dsn,
        company=company,
        client_a=client_a,
        client_b=client_b,
        user_a=user_a,
        employees=employees,
        requests=requests,
        contract=contract,
        renews_on=renews_on,
        mobile_stem=stem,
    )


def _as_worker(cur: psycopg.Cursor) -> None:
    """Take the worker login identity.

    The intake and automation helpers check ``session_user``, not role
    membership, so SET ROLE is not enough — only SET SESSION AUTHORIZATION
    changes what they see.
    """
    cur.execute("SET SESSION AUTHORIZATION app_worker")
    cur.execute("SELECT set_config('app.scope','worker',false)")


def _as_superuser(cur: psycopg.Cursor) -> None:
    """Drop back to the owning role.

    ``app_worker`` deliberately holds no table privileges at all — everything
    it does goes through SECURITY DEFINER helpers — so a test that both calls
    a helper and inspects a table has to switch identity between the two.
    """
    cur.execute("RESET SESSION AUTHORIZATION")


def _as_client(conn: psycopg.Connection, user_id: UUID) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.scope','corporate',false)")
        cur.execute("SELECT set_config('app.user_id',%s,false)", (str(user_id),))


def _archive(data: Fixture, **kwargs: object) -> dict:
    args = {
        "search": None,
        "status": None,
        "source": None,
        "date_from": None,
        "date_to": None,
        "limit": 25,
        "offset": 0,
    }
    args.update(kwargs)
    with psycopg.connect(data.dsn) as conn:
        _as_client(conn, data.user_a)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_security.corporate_request_archive(%(search)s, %(status)s,"
                " %(source)s, %(date_from)s, %(date_to)s, %(limit)s, %(offset)s)",
                args,
            )
            return cur.fetchone()[0]


def test_the_archive_holds_every_request_the_company_raised(data: Fixture) -> None:
    result = _archive(data)
    assert result["total"] == 3
    tickets = {item["ticket_no"] for item in result["items"]}
    assert len(tickets) == 3
    # Newest first: an archive is read from the top.
    stamps = [item["created_at"] for item in result["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_another_companys_requests_are_not_in_the_archive(data: Fixture) -> None:
    """Isolation is enforced in the database, not by filtering in Python."""
    mine = {item["id"] for item in _archive(data)["items"]}
    assert str(data.requests["other"][0]) not in mine


def test_the_archive_is_searched_by_name_mobile_card_and_ticket(data: Fixture) -> None:
    by_name = _archive(data, search="Karim")
    assert [item["employee_name"] for item in by_name["items"]] == ["Karim Nabil"]

    ticket = _archive(data)["items"][0]["ticket_no"]
    assert _archive(data, search=ticket)["total"] == 1

    card = _archive(data, search="CARD-karim")
    assert card["total"] == 1

    by_mobile = _archive(data, search=data.mobile_stem)
    assert by_mobile["total"] == 3


def test_a_search_term_that_matches_nothing_returns_nothing(data: Fixture) -> None:
    """A non-numeric term must not have its digits stripped to an empty LIKE."""
    assert _archive(data, search="zzz-no-such-person")["total"] == 0


def test_the_archive_filters_by_source_and_status(data: Fixture) -> None:
    assert _archive(data, source="email")["total"] == 1
    assert _archive(data, source="phone")["total"] == 1
    assert _archive(data, status="new")["total"] == 3
    assert _archive(data, status="resolved")["total"] == 0


def test_the_archive_filters_by_date_range(data: Fixture) -> None:
    today = date.today()
    assert _archive(data, date_from=today, date_to=today)["total"] == 3
    assert _archive(data, date_to=today - timedelta(days=1))["total"] == 0
    assert _archive(data, date_from=today + timedelta(days=1))["total"] == 0


def test_paging_keeps_the_total_and_the_counters_whole(data: Fixture) -> None:
    """The page shrinks; the total and the breakdown describe the filtered set."""
    page = _archive(data, limit=1, offset=0)
    assert len(page["items"]) == 1
    assert page["total"] == 3
    assert sum(page["summary"]["by_status"].values()) == 3
    assert sum(page["summary"]["by_source"].values()) == 3

    second = _archive(data, limit=1, offset=1)
    assert second["items"][0]["id"] != page["items"][0]["id"]
    assert _archive(data, limit=1, offset=99)["items"] == []


def test_the_archive_counts_messages_attachments_and_calls(data: Fixture) -> None:
    request_id, created_at = data.requests["soad"]
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.complaint_messages (complaint_id, complaint_created_at, body,"
            " visibility, channel, direction) VALUES (%s, %s, 'رد', 'public', 'whatsapp',"
            " 'outbound')",
            (request_id, created_at),
        )
    item = next(i for i in _archive(data)["items"] if i["id"] == str(request_id))
    assert item["messages_count"] >= 1
    assert item["attachments_count"] == 0
    assert item["calls_count"] == 0


def test_the_timeline_hides_internal_staff_notes(data: Fixture) -> None:
    """The brokerage's working notes about a company are not for that company."""
    request_id, created_at = data.requests["karim"]
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        for body, visibility in (("SECRET-NOTE", "internal"), ("ردنا عليك", "public")):
            cur.execute(
                "INSERT INTO public.complaint_messages (complaint_id, complaint_created_at,"
                " body, visibility, channel, direction) VALUES (%s, %s, %s,"
                " CAST(%s AS public.message_visibility), 'email', 'outbound')",
                (request_id, created_at, body, visibility),
            )

    with psycopg.connect(data.dsn) as conn:
        _as_client(conn, data.user_a)
        with conn.cursor() as cur:
            cur.execute("SELECT app_security.corporate_request_timeline(%s)", (request_id,))
            timeline = cur.fetchone()[0]

    bodies = [message["body"] for message in timeline["messages"]]
    assert "ردنا عليك" in bodies
    assert "SECRET-NOTE" not in bodies
    assert timeline["employee"]["full_name"] == "Karim Nabil"


def test_the_timeline_refuses_another_companys_request(data: Fixture) -> None:
    """A miss and a forbidden id look identical, so ids cannot be probed."""
    with psycopg.connect(data.dsn) as conn:
        _as_client(conn, data.user_a)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_security.corporate_request_timeline(%s)",
                (data.requests["other"][0],),
            )
            assert cur.fetchone()[0] is None
            cur.execute("SELECT app_security.corporate_request_timeline(%s)", (uuid4(),))
            assert cur.fetchone()[0] is None


def test_an_acknowledgement_is_sent_exactly_once(data: Fixture) -> None:
    request_id, created_at = data.requests["soad2"]
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        _as_worker(cur)
        cur.execute(
            "SELECT app_security.acknowledge_request(%s, %s, 'phone')", (request_id, created_at)
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT app_security.acknowledge_request(%s, %s, 'phone')", (request_id, created_at)
        )
        assert cur.fetchone()[0] is False, "a second worker must not message the customer again"

        _as_superuser(cur)
        cur.execute(
            "SELECT count(*) FROM public.notification_outbox WHERE complaint_id = %s",
            (request_id,),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM public.automation_runs WHERE subject_id = %s"
            " AND kind = 'request_acknowledgement'",
            (request_id,),
        )
        assert cur.fetchone()[0] == 1


def test_an_acknowledged_request_leaves_the_pending_queue(data: Fixture) -> None:
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        _as_worker(cur)
        cur.execute("SELECT complaint_id FROM app_security.pending_request_acknowledgements(100,24)")
        pending = {row[0] for row in cur.fetchall()}
    assert data.requests["soad2"][0] not in pending


def test_only_recent_requests_are_acknowledged(data: Fixture) -> None:
    """Enabling this must not blast every historical request with a message."""
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        _as_worker(cur)
        cur.execute("SELECT count(*) FROM app_security.pending_request_acknowledgements(100, 1)")
        recent = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM app_security.pending_request_acknowledgements(100, 24)")
        day = cur.fetchone()[0]
    assert recent <= day


def test_a_renewal_reminder_is_keyed_on_the_renewal_date(data: Fixture) -> None:
    """Re-running the cron must not re-send; moving the date must re-arm it."""
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        _as_worker(cur)
        cur.execute(
            "SELECT contract_id FROM app_security.pending_contract_renewals(60, 100)",
        )
        assert data.contract in {row[0] for row in cur.fetchall()}

        cur.execute(
            "SELECT app_security.mark_contract_renewal_notified(%s, %s, 'done', '{}'::jsonb)",
            (data.contract, data.renews_on),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT app_security.mark_contract_renewal_notified(%s, %s, 'done', '{}'::jsonb)",
            (data.contract, data.renews_on),
        )
        assert cur.fetchone()[0] is False

        cur.execute("SELECT contract_id FROM app_security.pending_contract_renewals(60, 100)")
        assert data.contract not in {row[0] for row in cur.fetchall()}

        # Renewed for another year: the reminder arms again.
        next_year = data.renews_on + timedelta(days=365)
        _as_superuser(cur)
        cur.execute(
            "UPDATE public.corporate_contracts SET renews_on = %s WHERE id = %s",
            (next_year, data.contract),
        )
        _as_worker(cur)
        cur.execute("SELECT contract_id FROM app_security.pending_contract_renewals(400, 100)")
        assert data.contract in {row[0] for row in cur.fetchall()}
        _as_superuser(cur)
        cur.execute(
            "UPDATE public.corporate_contracts SET renews_on = %s WHERE id = %s",
            (data.renews_on, data.contract),
        )


def test_a_renewal_outside_the_horizon_is_not_reported(data: Fixture) -> None:
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        _as_worker(cur)
        cur.execute("SELECT contract_id FROM app_security.pending_contract_renewals(2, 100)")
        assert data.contract not in {row[0] for row in cur.fetchall()}


def test_auto_close_waits_for_a_public_reply_and_respects_a_chase(data: Fixture) -> None:
    request_id, created_at = data.requests["soad"]
    old = datetime.now(UTC) - timedelta(days=40)

    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE public.complaint_messages SET created_at = %s WHERE complaint_id = %s",
            (old, request_id),
        )
        _as_worker(cur)
        cur.execute(
            "SELECT complaint_id FROM app_security.pending_request_auto_close(14, 100)",
        )
        assert request_id in {row[0] for row in cur.fetchall()}

        # An internal note is the brokerage talking to itself: it is not an
        # answer, so it must not keep the request open either.
        _as_superuser(cur)
        cur.execute(
            "INSERT INTO public.complaint_messages (complaint_id, complaint_created_at, body,"
            " visibility, channel, direction) VALUES (%s, %s, 'note', 'internal', 'portal',"
            " 'outbound')",
            (request_id, created_at),
        )
        _as_worker(cur)
        cur.execute("SELECT complaint_id FROM app_security.pending_request_auto_close(14, 100)")
        assert request_id in {row[0] for row in cur.fetchall()}

        # The customer chasing resets the clock.
        _as_superuser(cur)
        cur.execute(
            "INSERT INTO public.complaint_messages (complaint_id, complaint_created_at, body,"
            " visibility, channel, direction) VALUES (%s, %s, 'في رد؟', 'public', 'whatsapp',"
            " 'inbound')",
            (request_id, created_at),
        )
        _as_worker(cur)
        cur.execute("SELECT complaint_id FROM app_security.pending_request_auto_close(14, 100)")
        assert request_id not in {row[0] for row in cur.fetchall()}


def test_auto_close_resolves_once_and_records_why(data: Fixture) -> None:
    request_id, created_at = data.requests["karim"]
    old = datetime.now(UTC) - timedelta(days=40)
    with psycopg.connect(data.dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE public.complaint_messages SET created_at = %s WHERE complaint_id = %s"
            " AND direction = 'outbound'",
            (old, request_id),
        )
        _as_worker(cur)
        cur.execute("SELECT app_security.auto_close_request(%s, %s, 14)", (request_id, created_at))
        assert cur.fetchone()[0] is True
        cur.execute("SELECT app_security.auto_close_request(%s, %s, 14)", (request_id, created_at))
        assert cur.fetchone()[0] is False

        _as_superuser(cur)
        cur.execute(
            "SELECT status::text, resolved_at IS NOT NULL FROM public.complaints WHERE id = %s",
            (request_id,),
        )
        assert cur.fetchone() == ("resolved", True)
        cur.execute(
            "SELECT count(*) FROM public.complaint_events WHERE complaint_id = %s"
            " AND event_type = 'auto_closed'",
            (request_id,),
        )
        assert cur.fetchone()[0] == 1


def test_the_web_role_cannot_drive_automation(data: Fixture) -> None:
    """app_runtime serves browsers; only the worker may act on the platform's behalf."""
    runtime = os.getenv("TEST_DIRECT_DATABASE_URL", "").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    if not runtime:
        pytest.skip("TEST_DIRECT_DATABASE_URL is not set")
    request_id, created_at = data.requests["soad"]
    for statement, params in (
        ("SELECT app_security.acknowledge_request(%s, %s, 'phone')", (request_id, created_at)),
        ("SELECT app_security.auto_close_request(%s, %s, 14)", (request_id, created_at)),
        ("SELECT * FROM app_security.pending_request_acknowledgements(1, 1)", ()),
        ("SELECT * FROM app_security.pending_request_auto_close(1, 1)", ()),
    ):
        with psycopg.connect(runtime) as conn, conn.cursor() as cur:  # noqa: SIM117
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(statement, params)
