"""Real PostgreSQL tests: direct and PgBouncer transaction-pooling endpoints.

These are skipped without TEST_* URLs. CI starts PostgreSQL 16 and PgBouncer,
runs Alembic, then executes every parametrized test against both connections.
Never point the admin URL at a non-disposable database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Actor, make_engine, scoped_transaction
from app.models import Base

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Data:
    admin_url: str
    staff: dict[str, UUID]
    customers: dict[str, UUID]
    companies: dict[str, UUID]
    complaints: dict[str, tuple[UUID, datetime]]
    messages: dict[str, UUID]
    attachments: dict[str, UUID]
    notifications: dict[str, UUID]
    ai: dict[str, UUID]
    duplicates: dict[str, UUID]
    csat: dict[str, UUID]
    assignments: dict[str, UUID]
    schedules: dict[str, UUID]
    exports: dict[str, UUID]
    audit: dict[str, UUID]
    roles: list[UUID]
    teams: list[UUID]
    policies: list[UUID]
    ciphertext: bytes


def admin_dsn() -> str:
    url = os.getenv("TEST_DATABASE_ADMIN_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_ADMIN_URL and runtime URLs for real PostgreSQL tests")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(params=["direct", "pgbouncer"])
def runtime_url(request: pytest.FixtureRequest) -> str:
    key = f"TEST_{request.param.upper()}_DATABASE_URL"
    url = os.getenv(key)
    if not url:
        pytest.skip(f"{key} is not set")
    return url


@pytest.fixture(scope="module")
def data() -> Data:
    url = admin_dsn()
    unique = uuid4().hex[:10]
    staff = {name: uuid4() for name in ("admin", "agent", "other", "manager", "disabled")}
    customers = {name: uuid4() for name in ("one", "two")}
    companies = {name: uuid4() for name in ("a", "b")}
    roles = [uuid4() for _ in range(2)]
    teams = [uuid4(), uuid4()]
    policies = [uuid4() for _ in range(3)]
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = month + timedelta(seconds=1)
    last_month = month - timedelta(days=3)
    complaints = {
        "a_health": (uuid4(), this_month),
        "a_general": (uuid4(), month),
        "b_health": (uuid4(), last_month),
    }
    messages = {name: uuid4() for name in ("public", "internal", "b")}
    attachments = {name: uuid4() for name in ("public", "internal", "pending", "b")}
    notifications = {name: uuid4() for name in ("staff_a", "staff_b", "customer")}
    ai = {name: uuid4() for name in ("a", "b")}
    duplicates = {name: uuid4() for name in ("a_pair", "cross")}
    csat = {name: uuid4() for name in ("a", "b")}
    assignments = {name: uuid4() for name in ("a", "b")}
    schedules = {name: uuid4() for name in ("admin", "manager")}
    exports = {"agent": uuid4(), "manager": uuid4()}
    audit = {name: uuid4() for name in ("a", "b", "global")}
    ciphertext = b"encrypted-test-only" + os.urandom(16)
    fixture = Data(
        url,
        staff,
        customers,
        companies,
        complaints,
        messages,
        attachments,
        notifications,
        ai,
        duplicates,
        csat,
        assignments,
        schedules,
        exports,
        audit,
        roles,
        teams,
        policies,
        ciphertext,
    )

    with psycopg.connect(url, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO insurance_companies (id, slug, name_en, name_ar) VALUES (%s, %s, %s, %s)",
            [
                (companies[name], f"test-{unique}-{name}", f"Test insurer {name}", "تأمين تجريبي")
                for name in companies
            ],
        )
        cur.executemany(
            "INSERT INTO customers(id, full_name, national_id_ciphertext, "
            "national_id_blind_index, mobile) VALUES (%s, %s, %s, %s, %s)",
            [
                (customer_id, f"Fixture {name}", ciphertext, os.urandom(32), f"+201{index:09d}")
                for index, (name, customer_id) in enumerate(customers.items())
            ],
        )
        cur.executemany(
            "INSERT INTO staff_users(id, email, full_name, password_hash, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            [
                (
                    user_id,
                    f"{unique}-{name}@example.test",
                    f"Fixture {name}",
                    "test-only-no-login",
                    "disabled" if name == "disabled" else "active",
                )
                for name, user_id in staff.items()
            ],
        )
        cur.executemany(
            "INSERT INTO roles(id, name) VALUES (%s, %s)",
            [(roles[0], f"test-view-{unique}"), (roles[1], f"test-admin-{unique}")],
        )
        view_grants = (
            "complaints.view",
            "complaints.edit",
            "complaints.view_attachments",
            "integrations.ai_call",
            "analytics.view",
            "analytics.export",
            "audit.view",
        )
        cur.executemany(
            "INSERT INTO role_permissions(role_id, permission_code) VALUES (%s, %s)",
            [(roles[0], key) for key in view_grants]
            + [
                (roles[1], key)
                for key in (
                    "complaints.view",
                    "complaints.view_attachments",
                    "integrations.ai_call",
                    "audit.view",
                    "users.manage_access",
                    "complaints.view_full_national_id",
                    "analytics.view",
                    "analytics.export",
                    "complaints.export",
                    "complaints.assign",
                    "reports.manage",
                    "users.view",
                )
            ],
        )
        cur.executemany(
            "INSERT INTO user_roles(user_id, role_id) VALUES (%s, %s)",
            [(uid, roles[1] if name == "admin" else roles[0]) for name, uid in staff.items()],
        )
        cur.executemany(
            "INSERT INTO teams(id, name, lead_staff_id) VALUES (%s, %s, %s)",
            [
                (teams[0], f"test-team-a-{unique}", staff["manager"]),
                (teams[1], f"test-team-b-{unique}", staff["admin"]),
            ],
        )
        cur.executemany(
            "INSERT INTO team_members(team_id, user_id) VALUES (%s, %s)",
            [(teams[0], staff["manager"]), (teams[0], staff["agent"]), (teams[1], staff["other"])],
        )
        cur.executemany(
            "INSERT INTO user_company_scope(user_id, company_id, all_companies) "
            "VALUES (%s, %s, %s)",
            [
                (staff["admin"], None, True),
                (staff["agent"], companies["a"], False),
                (staff["other"], companies["b"], False),
                (staff["manager"], companies["a"], False),
                (staff["manager"], companies["b"], False),
                (staff["disabled"], companies["a"], False),
            ],
        )
        cur.execute(
            "INSERT INTO user_insurance_type_scope(user_id, insurance_type) VALUES (%s, 'health')",
            (staff["agent"],),
        )
        cur.executemany(
            "INSERT INTO user_ownership_level(user_id, level) VALUES (%s, %s)",
            [
                (staff["admin"], "all"),
                (staff["agent"], "own"),
                (staff["other"], "all"),
                (staff["manager"], "team"),
                (staff["disabled"], "all"),
            ],
        )
        cur.executemany(
            "INSERT INTO user_permission_overrides(user_id, permission_code, effect) "
            "VALUES (%s, %s, %s)",
            [
                (staff["manager"], "analytics.export", "deny"),
                (staff["agent"], "complaints.export", "allow"),
                (staff["manager"], "complaints.assign", "allow"),
                (staff["manager"], "reports.manage", "allow"),
            ],
        )
        cur.executemany(
            "INSERT INTO policies(id, customer_id, company_id, insurance_type, policy_number) "
            "VALUES (%s, %s, %s, %s, %s)",
            [
                (policies[0], customers["one"], companies["a"], "health", f"TEST-H-{unique}"),
                (policies[1], customers["one"], companies["a"], "general", f"TEST-G-{unique}"),
                (policies[2], customers["two"], companies["b"], "health", f"TEST-B-{unique}"),
            ],
        )
        for name, (complaint_id, created) in complaints.items():
            ticket = cur.execute(
                "SELECT app_security.issue_ticket_number(%s)", (created,)
            ).fetchone()[0]
            cur.execute(
                "INSERT INTO complaint_keys(id, created_at, ticket_no) VALUES (%s, %s, %s)",
                (complaint_id, created, ticket),
            )
            a_company = name != "b_health"
            health = name != "a_general"
            customer = customers["one" if a_company else "two"]
            company = companies["a" if a_company else "b"]
            assigned = (
                staff["manager"]
                if name == "a_general"
                else staff["agent" if a_company else "other"]
            )
            policy_id = policies[1 if name == "a_general" else 0 if a_company else 2]
            policy_suffix = {"a_health": "H", "a_general": "G", "b_health": "B"}[name]
            cur.execute(
                "INSERT INTO complaints(id, created_at, ticket_no, customer_id, company_id, "
                "insurance_type, policy_id, policy_number, subject, description, status, "
                "assigned_agent_id, team_id) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_review', %s, %s)",
                (
                    complaint_id,
                    created,
                    ticket,
                    customer,
                    company,
                    "health" if health else "general",
                    policy_id,
                    f"TEST-{policy_suffix}-{unique}",
                    f"Fixture subject {name}",
                    "Fixture description without private data.",
                    assigned,
                    teams[0] if a_company else teams[1],
                ),
            )
        a_id, a_created = complaints["a_health"]
        b_id, b_created = complaints["b_health"]
        cur.executemany(
            "INSERT INTO complaint_events(complaint_id, complaint_created_at, occurred_at, "
            "event_type, visibility) VALUES (%s, %s, %s, %s, %s)",
            [
                (a_id, a_created, a_created, "public_status", "public"),
                (a_id, a_created, a_created, "internal_note", "internal"),
                (b_id, b_created, b_created, "other_status", "public"),
            ],
        )
        cur.executemany(
            "INSERT INTO complaint_messages(id, complaint_id, complaint_created_at, "
            "body, visibility) VALUES (%s, %s, %s, %s, %s)",
            [
                (messages["public"], a_id, a_created, "Public reply", "public"),
                (messages["internal"], a_id, a_created, "Secret internal note", "internal"),
                (messages["b"], b_id, b_created, "Other insurer", "public"),
            ],
        )
        # Revision 24 stores bytes on the row itself; a stored blob is opaque ciphertext.
        stored_blob = b"KR" + bytes([1]) + b"0" * 12 + b"ciphertext-and-tag".ljust(16, b"0")
        cur.executemany(
            "INSERT INTO attachments(id, complaint_id, complaint_created_at, message_id, "
            "visibility, original_filename, mime_type, size_bytes, "
            "checksum_sha256, scan_status, content) VALUES "
            "(%s, %s, %s, %s, %s, 'fixture.txt', 'text/plain', 100, %s, %s, %s)",
            [
                (
                    attachments[name],
                    a_id if name != "b" else b_id,
                    a_created if name != "b" else b_created,
                    messages[
                        "internal" if name == "internal" else "b" if name == "b" else "public"
                    ],
                    "internal" if name == "internal" else "public",
                    "a" * 64,
                    "pending" if name == "pending" else "clean",
                    stored_blob,
                )
                for name in attachments
            ],
        )
        cur.executemany(
            "INSERT INTO notifications(id, recipient_staff_id, recipient_customer_id, "
            "complaint_id, complaint_created_at, kind) VALUES (%s, %s, %s, %s, %s, 'status')",
            [
                (notifications["staff_a"], staff["agent"], None, a_id, a_created),
                (notifications["staff_b"], staff["agent"], None, b_id, b_created),
                (notifications["customer"], None, customers["one"], a_id, a_created),
            ],
        )
        cur.executemany(
            "INSERT INTO call_logs(complaint_id, complaint_created_at, direction, "
            "consent_at, transcript_ciphertext) VALUES (%s, %s, 'outbound', %s, %s)",
            [
                (a_id, a_created, a_created, b"private-voice-data"),
                (b_id, b_created, b_created, b"other-voice-data"),
            ],
        )
        cur.executemany(
            "INSERT INTO ai_insights(id, complaint_id, complaint_created_at, kind, "
            "payload_ciphertext, confidence) VALUES (%s, %s, %s, 'summary', %s, 0.89)",
            [
                (ai["a"], a_id, a_created, b"encrypted-ai-summary"),
                (ai["b"], b_id, b_created, b"encrypted-ai-other"),
            ],
        )
        pair_rows = []
        for key, names in (
            ("a_pair", ("a_health", "a_general")),
            ("cross", ("a_health", "b_health")),
        ):
            first, second = sorted(
                (complaints[names[0]], complaints[names[1]]), key=lambda row: row[0].int
            )
            pair_rows.append((duplicates[key], first[0], first[1], second[0], second[1]))
        cur.executemany(
            "INSERT INTO duplicate_candidates(id, first_complaint_id, "
            "first_complaint_created_at, second_complaint_id, "
            "second_complaint_created_at, confidence) "
            "VALUES (%s, %s, %s, %s, %s, 0.73)",
            pair_rows,
        )
        cur.executemany(
            "INSERT INTO csat_responses(id, complaint_id, complaint_created_at, "
            "customer_id, score) VALUES (%s, %s, %s, %s, %s)",
            [
                (csat["a"], a_id, a_created, customers["one"], 4),
                (csat["b"], b_id, b_created, customers["two"], 2),
            ],
        )
        cur.executemany(
            "INSERT INTO assignment_rules(id, company_id, insurance_type, "
            "target_team_id) VALUES (%s, %s, 'health', %s)",
            [
                (assignments["a"], companies["a"], teams[0]),
                (assignments["b"], companies["b"], teams[1]),
            ],
        )
        cur.executemany(
            "INSERT INTO report_schedules(id, owner_staff_id, name, cron_expression) "
            "VALUES (%s, %s, %s, '0 9 * * 1')",
            [
                (schedules["admin"], staff["admin"], f"admin-report-{unique}"),
                (schedules["manager"], staff["manager"], f"manager-report-{unique}"),
            ],
        )
        cur.executemany(
            "INSERT INTO export_jobs(id, owner_staff_id, kind) VALUES (%s, %s, %s)",
            [
                (exports["agent"], staff["agent"], "complaints"),
                (exports["manager"], staff["manager"], "analytics"),
            ],
        )
        cur.executemany(
            "INSERT INTO audit_logs(id, complaint_id, complaint_created_at, "
            "actor_staff_id, action, resource_type) VALUES (%s, %s, %s, %s, 'view', 'complaint')",
            [
                (audit["a"], a_id, a_created, staff["admin"]),
                (audit["b"], b_id, b_created, staff["admin"]),
                (audit["global"], None, None, staff["admin"]),
            ],
        )
        cur.execute(
            "INSERT INTO access_change_log(actor_staff_id, target_user_id, change_type) "
            "VALUES (%s, %s, 'fixture')",
            (staff["admin"], staff["agent"]),
        )
    try:
        yield fixture
    finally:
        with psycopg.connect(url, prepare_threshold=None) as conn, conn.cursor() as cur:
            ids = [row[0] for row in complaints.values()]
            cur.execute("DELETE FROM audit_logs WHERE id = ANY(%s)", (list(audit.values()),))
            cur.execute(
                "DELETE FROM access_change_log WHERE target_user_id = %s", (staff["agent"],)
            )
            cur.execute("DELETE FROM complaints WHERE id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM complaint_keys WHERE id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM policies WHERE id = ANY(%s)", (policies,))
            cur.execute(
                "DELETE FROM assignment_rules WHERE id = ANY(%s)", (list(assignments.values()),)
            )
            cur.execute(
                "DELETE FROM report_schedules WHERE id = ANY(%s)", (list(schedules.values()),)
            )
            cur.execute("DELETE FROM export_jobs WHERE id = ANY(%s)", (list(exports.values()),))
            cur.execute("DELETE FROM teams WHERE id = ANY(%s)", (teams,))
            cur.execute("DELETE FROM staff_users WHERE id = ANY(%s)", (list(staff.values()),))
            cur.execute("DELETE FROM roles WHERE id = ANY(%s)", (roles,))
            cur.execute("DELETE FROM customers WHERE id = ANY(%s)", (list(customers.values()),))
            cur.execute(
                "DELETE FROM insurance_companies WHERE id = ANY(%s)", (list(companies.values()),)
            )


async def visible_ids(session: AsyncSession, table: str) -> set[UUID]:
    # The table name is a test constant, never request input.
    assert table in {
        "complaints",
        "complaint_messages",
        "attachments",
        "notifications",
        "audit_logs",
        "customers",
        "policies",
        "complaint_keys",
        "ai_insights",
        "duplicate_candidates",
        "csat_responses",
        "assignment_rules",
        "report_schedules",
        "export_jobs",
    }
    # The access-change trigger also emits staff-only *non-complaint* notices.
    # These must remain visible after complaint permission/scope is revoked.
    where = " WHERE complaint_id IS NOT NULL" if table == "notifications" else ""
    return {row[0] for row in (await session.execute(text(f"SELECT id FROM {table}{where}"))).all()}


async def test_company_type_ownership_and_child_rls(data: Data, runtime_url: str) -> None:
    engine = make_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with scoped_transaction(factory, Actor(data.staff["agent"], "staff")) as session:
            assert await visible_ids(session, "complaints") == {data.complaints["a_health"][0]}
            assert await visible_ids(session, "complaint_keys") == {data.complaints["a_health"][0]}
            assert await visible_ids(session, "customers") == {data.customers["one"]}
            assert await visible_ids(session, "policies") == {data.policies[0]}
            assert await visible_ids(session, "complaint_messages") == {
                data.messages["public"],
                data.messages["internal"],
            }
            assert await visible_ids(session, "attachments") == {
                data.attachments["public"],
                data.attachments["internal"],
                data.attachments["pending"],
            }
            assert await visible_ids(session, "notifications") == {data.notifications["staff_a"]}
            assert await visible_ids(session, "audit_logs") == {data.audit["a"]}
            assert await visible_ids(session, "ai_insights") == {data.ai["a"]}
            assert await visible_ids(session, "duplicate_candidates") == set()
            assert await visible_ids(session, "csat_responses") == {data.csat["a"]}
            assert await visible_ids(session, "assignment_rules") == set()
            assert await visible_ids(session, "export_jobs") == {data.exports["agent"]}
            assert (await session.execute(text("SELECT count(*) FROM call_logs"))).scalar_one() == 1
            assert (
                await session.execute(text("SELECT count(*) FROM complaint_events"))
            ).scalar_one() == 2
            assert (
                await session.execute(
                    text(
                        "SELECT app_security.has_permission('complaints.export'), "
                        "app_security.has_permission('complaints.view_full_national_id')"
                    )
                )
            ).one() == (True, False)
            assert (
                await session.execute(
                    text("SELECT app_security.read_national_id_ciphertext(:customer)"),
                    {"customer": data.customers["one"]},
                )
            ).scalar_one() is None
        async with scoped_transaction(factory, Actor(data.staff["manager"], "staff")) as session:
            assert await visible_ids(session, "complaints") == {
                data.complaints["a_health"][0],
                data.complaints["a_general"][0],
            }
            assert await visible_ids(session, "ai_insights") == {data.ai["a"]}
            assert await visible_ids(session, "duplicate_candidates") == {data.duplicates["a_pair"]}
            assert await visible_ids(session, "assignment_rules") == set(data.assignments.values())
            assert await visible_ids(session, "report_schedules") == {data.schedules["manager"]}
            assert await visible_ids(session, "export_jobs") == set()  # explicit deny
            assert (
                await session.execute(
                    text("SELECT app_security.has_permission('analytics.export')")
                )
            ).scalar_one() is False  # deny > role grant
        async with scoped_transaction(factory, Actor(data.staff["other"], "staff")) as session:
            assert await visible_ids(session, "complaints") == {data.complaints["b_health"][0]}
            assert await visible_ids(session, "ai_insights") == {data.ai["b"]}
            assert await visible_ids(session, "duplicate_candidates") == set()
        async with scoped_transaction(factory, Actor(data.staff["admin"], "staff")) as session:
            # Admin's global grant also sees any optional demo seed already present.
            assert {row[0] for row in data.complaints.values()} <= await visible_ids(
                session, "complaints"
            )
            assert set(data.audit.values()) <= await visible_ids(session, "audit_logs")
            assert set(data.ai.values()) <= await visible_ids(session, "ai_insights")
            assert set(data.duplicates.values()) <= await visible_ids(
                session, "duplicate_candidates"
            )
            assert set(data.csat.values()) <= await visible_ids(session, "csat_responses")
            assert await visible_ids(session, "report_schedules") == {data.schedules["admin"]}
            assert (
                await session.execute(
                    text("SELECT app_security.read_national_id_ciphertext(:customer)"),
                    {"customer": data.customers["one"]},
                )
            ).scalar_one() == data.ciphertext
            assert (
                await session.execute(
                    text("SELECT count(*) FROM access_change_log WHERE target_user_id = :id"),
                    {"id": data.staff["agent"]},
                )
            ).scalar_one() == 1
        async with scoped_transaction(factory, Actor(data.staff["disabled"], "staff")) as session:
            assert await visible_ids(session, "complaints") == set()
            assert await visible_ids(session, "notifications") == set()
        async with scoped_transaction(factory, Actor(data.customers["one"], "customer")) as session:
            assert await visible_ids(session, "complaints") == {
                data.complaints["a_health"][0],
                data.complaints["a_general"][0],
            }
            assert await visible_ids(session, "complaint_messages") == {data.messages["public"]}
            assert await visible_ids(session, "attachments") == {data.attachments["public"]}
            assert await visible_ids(session, "ai_insights") == set()
            assert await visible_ids(session, "duplicate_candidates") == set()
            assert await visible_ids(session, "csat_responses") == {data.csat["a"]}
            assert await visible_ids(session, "notifications") == {data.notifications["customer"]}
            assert (
                await session.execute(text("SELECT count(*) FROM complaint_events"))
            ).scalar_one() == 1
            assert (await session.execute(text("SELECT count(*) FROM call_logs"))).scalar_one() == 0
        async with scoped_transaction(factory, Actor(data.customers["two"], "customer")) as session:
            assert await visible_ids(session, "complaints") == {data.complaints["b_health"][0]}
            assert await visible_ids(session, "complaint_messages") == {data.messages["b"]}
            assert await visible_ids(session, "csat_responses") == {data.csat["b"]}
    finally:
        await engine.dispose()


async def test_context_cannot_leak_between_transactions_or_actors(
    data: Data, runtime_url: str
) -> None:
    engine = make_engine(runtime_url)
    factory = async_sessionmaker(engine)
    try:
        # No GUC -> zero rows. Same engine/pooler, different actors, then no GUC.
        async with factory() as session:
            assert await visible_ids(session, "complaints") == set()
            assert (
                await session.execute(text("SELECT app_security.actor_id()"))
            ).scalar_one() is None
        for name, expected in (("agent", "a_health"), ("other", "b_health"), ("agent", "a_health")):
            async with scoped_transaction(factory, Actor(data.staff[name], "staff")) as session:
                assert await visible_ids(session, "complaints") == {data.complaints[expected][0]}
                assert (
                    await session.execute(text("SELECT app_security.actor_id()"))
                ).scalar_one() == data.staff[name]
        async with factory() as session:
            assert await visible_ids(session, "complaints") == set()
            assert (
                await session.execute(text("SELECT app_security.actor_id()"))
            ).scalar_one() is None
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.user_id','invalid-uuid',true), "
                    "set_config('app.scope','staff',true)"
                )
            )
            assert await visible_ids(session, "complaints") == set()
    finally:
        await engine.dispose()


async def test_instant_scope_revocation_hides_old_child_rows(data: Data, runtime_url: str) -> None:
    engine = make_engine(runtime_url)
    factory = async_sessionmaker(engine)
    uid = data.staff["agent"]
    company = data.companies["a"]
    try:
        async with scoped_transaction(factory, Actor(uid, "staff")) as session:
            assert await visible_ids(session, "complaints") == {data.complaints["a_health"][0]}
        with psycopg.connect(data.admin_url) as conn:
            conn.execute(
                "DELETE FROM user_company_scope WHERE user_id = %s AND company_id = %s",
                (uid, company),
            )
        async with scoped_transaction(factory, Actor(uid, "staff")) as session:
            for table in (
                "complaints",
                "complaint_messages",
                "attachments",
                "notifications",
                "audit_logs",
                "customers",
                "policies",
                "ai_insights",
                "duplicate_candidates",
                "csat_responses",
            ):
                assert await visible_ids(session, table) == set(), table
    finally:
        with psycopg.connect(data.admin_url) as conn:
            conn.execute(
                "INSERT INTO user_company_scope(user_id, company_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (uid, company),
            )
        await engine.dispose()


async def test_out_of_scope_write_fts_and_customer_internal_note_denied(
    data: Data, runtime_url: str
) -> None:
    engine = make_engine(runtime_url)
    factory = async_sessionmaker(engine)
    try:
        async with scoped_transaction(factory, Actor(data.staff["agent"], "staff")) as session:
            result = await session.execute(
                text(
                    "SELECT id FROM complaints WHERE search_vector @@ "
                    "plainto_tsquery('simple', 'Fixture')"
                )
            )
            assert {row[0] for row in result.all()} == {data.complaints["a_health"][0]}
            mutation = await session.execute(
                text("UPDATE complaints SET subject = subject WHERE id = :other RETURNING id"),
                {"other": data.complaints["b_health"][0]},
            )
            assert mutation.all() == []  # UPDATE policy is scoped, not merely the API GET path.
        with pytest.raises(DBAPIError):
            async with scoped_transaction(factory, Actor(data.staff["agent"], "staff")) as session:
                await session.execute(text("SELECT national_id_ciphertext FROM customers LIMIT 1"))
        with pytest.raises(DBAPIError):
            async with scoped_transaction(factory, Actor(data.staff["agent"], "staff")) as session:
                await session.execute(
                    text("SELECT contact_emails FROM insurance_companies LIMIT 1")
                )
        with pytest.raises(DBAPIError):
            async with scoped_transaction(
                factory, Actor(data.customers["one"], "customer")
            ) as session:
                await session.execute(
                    text(
                        "INSERT INTO complaint_messages(complaint_id, complaint_created_at, "
                        "body, visibility, sender_customer_id) "
                        "VALUES (:id, :created, 'Forbidden internal note', 'internal', :customer)"
                    ),
                    {
                        "id": data.complaints["a_health"][0],
                        "created": data.complaints["a_health"][1],
                        "customer": data.customers["one"],
                    },
                )
    finally:
        await engine.dispose()


async def test_deny_override_is_immediate(data: Data, runtime_url: str) -> None:
    engine = make_engine(runtime_url)
    factory = async_sessionmaker(engine)
    uid = data.staff["agent"]
    try:
        with psycopg.connect(data.admin_url) as conn:
            conn.execute(
                "INSERT INTO user_permission_overrides(user_id, permission_code, effect) "
                "VALUES (%s, 'complaints.view', 'deny')",
                (uid,),
            )
        async with scoped_transaction(factory, Actor(uid, "staff")) as session:
            assert await visible_ids(session, "complaints") == set()
            assert await visible_ids(session, "attachments") == set()
            assert await visible_ids(session, "notifications") == set()
            assert await visible_ids(session, "ai_insights") == set()
    finally:
        with psycopg.connect(data.admin_url) as conn:
            conn.execute(
                "DELETE FROM user_permission_overrides "
                "WHERE user_id = %s AND permission_code = 'complaints.view'",
                (uid,),
            )
        await engine.dispose()


def test_partition_placement_catalog_and_runtime_role(data: Data) -> None:
    with psycopg.connect(data.admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM permissions").fetchone()[0] >= 34
        db_columns = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(Base.metadata.tables),),
        ).fetchall()
        by_table: dict[str, set[str]] = {}
        for table_name, column_name in db_columns:
            by_table.setdefault(table_name, set()).add(column_name)
        assert by_table == {
            name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()
        }
        flags = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'complaints'::regclass"
        ).fetchone()
        assert flags == (True, True)
        role = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'app_runtime'"
        ).fetchone()
        assert role == (False, False)
        placed = [
            conn.execute(
                "SELECT tableoid::regclass::text FROM complaints WHERE id = %s", (complaint_id,)
            ).fetchone()[0]
            for complaint_id, _ in data.complaints.values()
        ]
        assert placed[0].startswith("complaints_") and "default" not in placed[0]
        assert placed[0] != placed[-1]  # month-specific physical partitions
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'complaints' "
            "AND indexname = 'ix_complaints_fts'"
        ).fetchone()


async def test_complaint_policy_link_and_directory(runtime_url: str, data: Data) -> None:
    """Real complaint service, restricted runtime policy writes, and admin directory."""
    from app.domain.access import AccessScope
    from app.domain.schemas import ComplaintCreate
    from app.repositories.complaints import ComplaintRepository
    from app.security.crypto import NationalIdProtector
    from app.services.complaints import ComplaintService

    engine = make_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    customer = data.customers["one"]
    company = data.companies["a"]
    number = f"LINK-{uuid4().hex}"
    protector = NationalIdProtector(b"e" * 32, b"b" * 32)
    with psycopg.connect(data.admin_url) as conn:
        conn.execute(
            "UPDATE customers SET national_id_blind_index = %s, mobile = %s WHERE id = %s",
            (protector.blind_index("29901010112345"), "+201123456789", customer),
        )
    try:
        actor = Actor(customer, "customer")
        async with scoped_transaction(factory, actor) as db:
            item = ComplaintCreate(
                national_id="29901010112345",
                mobile="+201123456789",
                company_id=company,
                insurance_type="health",
                policy_number=number,
                subject="Policy linking test",
                description="Customer-visible complaint description.",
            )
            repo = ComplaintRepository()
            service = ComplaintService(repo, protector, "k" * 64)
            scope = AccessScope.for_customer(actor)
            first, _ = await service.create(db, scope, item, uuid4().hex)
            second, _ = await service.create(db, scope, item, uuid4().hex)
            third, _ = await service.create(
                db, scope, item.model_copy(update={"policy_number": number + "-2"}), uuid4().hex
            )
            assert first.policy_id is not None
            assert second.policy_id == first.policy_id
            assert third.policy_id != first.policy_id
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM policies WHERE policy_number IN (:one, :two)"),
                    {"one": number, "two": number + "-2"},
                )
                == 2
            )
            # Simulate a historical complaint created before policy linking was fixed.
            legacy = item.model_copy(update={"company_id": data.companies["b"]})
            reserved = await repo.reserve_idempotency(
                db, customer, uuid4().bytes * 2, uuid4().bytes * 2
            )
            await repo.create(
                db, customer_id=customer, item=legacy, policy_id=None, idempotency=reserved
            )
            await db.execute(text("SELECT set_config('app.scope', 'staff', true)"))
            await db.execute(
                text("SELECT set_config('app.user_id', :id, true)"),
                {"id": str(data.staff["admin"])},
            )
            rows = await db.scalar(text("SELECT app_security.customer_directory()"))
            row = next(r for r in rows if r["id"] == str(customer))
            assert {str(company), str(data.companies["b"])} <= {c["id"] for c in row["companies"]}
            assert number in {p["policy_number"] for p in row["policies"]}
            # Directory may carry last4 only; never the full ID or ciphertext.
            assert "29901010112345" not in str(row)
            assert "national_id_ciphertext" not in str(row)
            assert set(row) >= {"national_id_last4"}
            await db.rollback()  # Leave fixture cleanup and other tests unaffected.
    finally:
        await engine.dispose()
