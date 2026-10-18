"""Opt-in, transactionally idempotent demo data; NEVER run in production.

DEMO_STAFF_PASSWORD (>=12 characters), DATABASE_ADMIN_URL, and the independent
32-byte base64 encryption/blind-index keys are required in the environment.
All identifiers are synthetic; no real national IDs or passwords are committed.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from argon2 import PasswordHasher
from faker import Faker
from psycopg.types.json import Jsonb

from app.domain.permissions import PERMISSIONS, permission_parts
from app.security.crypto import NationalIdProtector, decode_key
from app.security.national_id import GOVERNORATE_CODES

DEMO_ADMIN_EMAIL = "admin@demo.example"
COMPANIES = (
    ("nile-assurance", "Nile Assurance", "تأمين النيل", "claims@nile.example"),
    ("pyramid-insurance", "Pyramid Insurance", "تأمين الأهرام", "claims@pyramid.example"),
    ("delta-care", "Delta Care", "دلتا للرعاية", "care@delta.example"),
    ("cairo-shield", "Cairo Shield", "درع القاهرة", "support@cairo.example"),
)
ISSUES = (
    (
        "Delayed claim reimbursement",
        "My reimbursement claim has not been paid after the expected date.",
    ),
    ("Hospital approval pending", "The hospital is still waiting for an approval code."),
    ("Incorrect policy details", "The coverage information on my policy appears incorrect."),
    ("Renewal premium discrepancy", "The renewal quote does not match the agreed premium."),
    ("Repair estimate disputed", "The approved vehicle repair amount does not match the estimate."),
    ("Provider network question", "The listed provider could not verify my coverage."),
    ("Missing claim update", "I have not received an update on my claim status."),
    ("مراجعة مطالبة طبية", "أرغب في مراجعة تفاصيل المطالبة الطبية والتغطية التأمينية."),
    ("تأخر الموافقة", "لم تصل الموافقة المطلوبة من شركة التأمين حتى الآن."),
)
ROLE_PERMISSIONS = {
    "administrator": PERMISSIONS,
    "supervisor": frozenset(
        {
            "complaints.view",
            "complaints.create",
            "complaints.edit",
            "complaints.assign",
            "complaints.reassign",
            "complaints.change_status",
            "complaints.reply_public",
            "complaints.add_internal_note",
            "complaints.escalate",
            "complaints.export",
            "complaints.view_attachments",
            "analytics.view",
            "analytics.export",
            "users.view",
            "templates.manage",
            "reports.manage",
            "integrations.send_email",
        }
    ),
    "agent": frozenset(
        {
            "complaints.view",
            "complaints.create",
            "complaints.edit",
            "complaints.change_status",
            "complaints.reply_public",
            "complaints.add_internal_note",
            "complaints.view_attachments",
        }
    ),
    "analyst": frozenset({"complaints.view", "analytics.view", "analytics.export"}),
}


def synthetic_national_id(rng: random.Random) -> str:
    """Syntactically valid, randomly generated demo-only identifier."""
    born = date(1960, 1, 1) + timedelta(days=rng.randrange(18_000))
    century = "2" if born.year < 2000 else "3"
    gov = rng.choice(sorted(GOVERNORATE_CODES))
    return f"{century}{born:%y%m%d}{gov}{rng.randrange(10000):04}{rng.randrange(10)}"


def seed(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    protector: NationalIdProtector,
    password: str,
    *,
    count: int = 5000,
) -> bool:
    """Return False if the (atomic) demo seed already exists."""
    if count < 1:
        raise ValueError("count must be positive")
    if len(password) < 12:
        raise ValueError("DEMO_STAFF_PASSWORD must be at least 12 characters")
    if cursor.execute("SELECT 1 FROM staff_users WHERE email = %s", (DEMO_ADMIN_EMAIL,)).fetchone():
        return False

    rng = random.Random(20261015)
    faker = Faker("ar_EG")
    faker.seed_instance(20261015)
    now = datetime.now(UTC)
    hasher = PasswordHasher()

    companies: list[UUID] = []
    for slug, english, arabic, email in COMPANIES:
        company_id = uuid4()
        cursor.execute(
            "INSERT INTO insurance_companies(id, slug, name_en, name_ar, contact_emails) "
            "VALUES (%s, %s, %s, %s, %s)",
            (company_id, slug, english, arabic, [email]),
        )
        companies.append(company_id)

    cursor.executemany(
        "INSERT INTO permissions(code, resource, action) VALUES (%s, %s, %s) "
        "ON CONFLICT (code) DO NOTHING",
        [(code, *permission_parts(code)) for code in sorted(PERMISSIONS)],
    )
    roles = {name: uuid4() for name in ROLE_PERMISSIONS}
    cursor.executemany(
        "INSERT INTO roles(id, name, description, is_system) VALUES (%s, %s, %s, true)",
        [(role_id, name, f"Seeded {name} role") for name, role_id in roles.items()],
    )
    cursor.executemany(
        "INSERT INTO role_permissions(role_id, permission_code) VALUES (%s, %s)",
        [(roles[name], code) for name, grants in ROLE_PERMISSIONS.items() for code in grants],
    )

    staff_specs = (
        ("admin@demo.example", "Demo Administrator", "administrator"),
        ("supervisor@demo.example", "Demo Supervisor", "supervisor"),
        ("agent@demo.example", "Demo Agent", "agent"),
        ("analyst@demo.example", "Demo Analyst", "analyst"),
    )
    staff = {email: uuid4() for email, _, _ in staff_specs}
    cursor.executemany(
        "INSERT INTO staff_users(id, email, full_name, password_hash, status, "
        "must_change_password) VALUES (%s, %s, %s, %s, 'active', true)",
        [(staff[email], email, name, hasher.hash(password)) for email, name, _ in staff_specs],
    )
    cursor.executemany(
        "INSERT INTO user_roles(user_id, role_id) VALUES (%s, %s)",
        [(staff[email], roles[role]) for email, _, role in staff_specs],
    )
    admin = staff["admin@demo.example"]
    supervisor = staff["supervisor@demo.example"]
    agent = staff["agent@demo.example"]
    analyst = staff["analyst@demo.example"]
    team_a, team_b = uuid4(), uuid4()
    cursor.executemany(
        "INSERT INTO teams(id, name, lead_staff_id) VALUES (%s, %s, %s)",
        [(team_a, "Nile claims", supervisor), (team_b, "Regional claims", admin)],
    )
    cursor.executemany(
        "INSERT INTO team_members(team_id, user_id) VALUES (%s, %s)",
        [(team_a, supervisor), (team_a, agent), (team_b, admin)],
    )
    cursor.executemany(
        "INSERT INTO user_company_scope(user_id, company_id, all_companies) VALUES (%s, %s, %s)",
        [
            (admin, None, True),
            (analyst, None, True),
            (supervisor, companies[0], False),
            (supervisor, companies[1], False),
            (agent, companies[0], False),
        ],
    )
    cursor.execute(
        "INSERT INTO user_insurance_type_scope(user_id, insurance_type) VALUES (%s, 'health')",
        (agent,),
    )
    cursor.executemany(
        "INSERT INTO user_ownership_level(user_id, level) VALUES (%s, %s)",
        [(admin, "all"), (supervisor, "team"), (agent, "own"), (analyst, "all")],
    )
    cursor.execute(
        "INSERT INTO platform_super_admin(singleton, staff_user_id) VALUES (true, %s) "
        "ON CONFLICT (singleton) DO NOTHING",
        (admin,),
    )
    cursor.execute(
        "INSERT INTO user_permission_overrides(user_id, permission_code, effect) "
        "VALUES (%s, 'analytics.export', 'deny')",
        (supervisor,),
    )
    cursor.execute(
        "INSERT INTO access_change_log(actor_staff_id, target_user_id, change_type, "
        "before_state, after_state) VALUES (%s, %s, 'initial_scope', %s, %s)",
        (
            admin,
            agent,
            Jsonb({}),
            Jsonb(
                {
                    "companies": [str(companies[0])],
                    "insurance_types": ["health"],
                    "ownership": "own",
                }
            ),
        ),
    )

    categories = []
    for english, arabic in (
        ("Claims", "المطالبات"),
        ("Authorizations", "الموافقات"),
        ("Policy details", "تفاصيل الوثيقة"),
        ("Billing", "الفواتير"),
        ("Provider network", "شبكة مقدمي الخدمة"),
        ("Other", "أخرى"),
    ):
        category_id = uuid4()
        cursor.execute(
            "INSERT INTO categories(id, name_en, name_ar) VALUES (%s, %s, %s)",
            (category_id, english, arabic),
        )
        categories.append(category_id)

    customer_count = max(250, count // 4)
    customers: list[UUID] = []
    seen_ids: set[str] = set()
    customer_rows = []
    for _ in range(customer_count):
        national_id = synthetic_national_id(rng)
        while national_id in seen_ids:
            national_id = synthetic_national_id(rng)
        seen_ids.add(national_id)
        customer_id = uuid4()
        customers.append(customer_id)
        customer_rows.append(
            (
                customer_id,
                faker.name(),
                protector.encrypt(national_id),
                protector.blind_index(national_id),
                national_id[-4:],
                f"+201{rng.choice('0125')}{rng.randrange(10**8):08}",
                rng.random() < 0.7,
                None,
            )
        )
    cursor.executemany(
        "INSERT INTO customers(id, full_name, national_id_ciphertext, national_id_blind_index, "
        "national_id_last4, mobile, whatsapp_available, email) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        customer_rows,
    )

    policies: list[tuple[UUID, UUID, UUID, str, str]] = []
    for index, customer_id in enumerate(customers):
        company_id = companies[rng.randrange(len(companies))]
        insurance_type = rng.choice(("health", "general", "both"))
        policy_id = uuid4()
        policy_number = f"POL-{index + 1:07d}"
        policies.append((policy_id, customer_id, company_id, insurance_type, policy_number))
    cursor.executemany(
        "INSERT INTO policies(id, customer_id, company_id, insurance_type, policy_number) "
        "VALUES (%s, %s, %s, %s, %s)",
        policies,
    )

    # One global sequence enforces uniqueness, including across UTC year boundaries.
    sequences = [
        row[0]
        for row in cursor.execute(
            "SELECT nextval('complaint_ticket_sequence') FROM generate_series(1, %s)", (count,)
        ).fetchall()
    ]
    keys = []
    complaints = []
    events: list[
        tuple[UUID, datetime, datetime, str, str | None, str, UUID | None, UUID | None]
    ] = []
    messages = []
    audit = []
    statuses = ("new", "assigned", "in_review", "resolved", "reopened", "transferred")
    for index in range(count):
        policy_id, customer_id, company_id, insurance_type, policy_number = rng.choice(policies)
        created = now - timedelta(
            days=rng.randrange(0, 540), hours=rng.randrange(24), minutes=rng.randrange(60)
        )
        complaint_id = uuid4()
        ticket = f"CMP-{created.year}-{sequences[index]:06d}"
        status = rng.choices(statuses, weights=(12, 14, 28, 28, 8, 10), k=1)[0]
        priority = rng.choices(("low", "normal", "high", "urgent"), weights=(10, 62, 23, 5), k=1)[0]
        team = team_a if company_id in companies[:2] else team_b
        assigned = (
            None
            if status == "new"
            else agent
            if company_id == companies[0] and insurance_type == "health" and rng.random() < 0.7
            else supervisor
            if company_id in companies[:2]
            else admin
        )
        subject, description = rng.choice(ISSUES)
        resolved = (
            min(created + timedelta(hours=5), now) if status in {"resolved", "reopened"} else None
        )
        keys.append((complaint_id, created, ticket))
        complaints.append(
            (
                complaint_id,
                created,
                ticket,
                customer_id,
                company_id,
                insurance_type,
                policy_id,
                policy_number,
                subject,
                description,
                status,
                priority,
                rng.choice(categories),
                rng.choice(("negative", "neutral", "positive")),
                "portal",
                assigned,
                team,
                created + timedelta(days=3),
                resolved,
                1 if status == "reopened" else 0,
                rng.randrange(1, 6) if status == "resolved" else None,
            )
        )
        events.append((complaint_id, created, created, "created", None, "new", None, customer_id))
        paths = {
            "new": (),
            "assigned": ("assigned",),
            "in_review": ("assigned", "in_review"),
            "resolved": ("assigned", "in_review", "resolved"),
            "reopened": ("assigned", "in_review", "resolved", "reopened"),
            "transferred": ("assigned", "in_review", "transferred"),
        }
        previous = "new"
        for step, next_status in enumerate(paths[status], start=1):
            changed_at = min(created + timedelta(hours=step + 2), now)
            customer_actor = customer_id if next_status == "reopened" else None
            staff_actor = None if customer_actor else assigned
            events.append(
                (
                    complaint_id,
                    created,
                    changed_at,
                    "status_changed",
                    previous,
                    next_status,
                    staff_actor,
                    customer_actor,
                )
            )
            previous = next_status
        if index % 3 == 0:
            messages.append((complaint_id, created, description, customer_id, created))
        if index % 10 == 0:
            audit.append((complaint_id, created, "complaint.created", customer_id, created))

    cursor.executemany(
        "INSERT INTO complaint_keys(id, created_at, ticket_no) VALUES (%s, %s, %s)", keys
    )
    cursor.executemany(
        "INSERT INTO complaints(id, created_at, ticket_no, customer_id, company_id, "
        "insurance_type, policy_id, policy_number, subject, description, status, priority, "
        "category_id, sentiment, channel, assigned_agent_id, team_id, sla_due_at, resolved_at, "
        "reopened_count, csat_score) VALUES ("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        complaints,
    )
    cursor.executemany(
        "INSERT INTO complaint_events(complaint_id, complaint_created_at, occurred_at, "
        "event_type, from_status, to_status, actor_staff_id, actor_customer_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        events,
    )
    cursor.executemany(
        "INSERT INTO complaint_messages(complaint_id, complaint_created_at, body, "
        "sender_customer_id, created_at) VALUES (%s, %s, %s, %s, %s)",
        messages,
    )
    cursor.executemany(
        "INSERT INTO audit_logs(complaint_id, complaint_created_at, action, resource_type, "
        "resource_id, created_at) VALUES (%s, %s, %s, 'complaint', %s, %s)",
        [
            (complaint_id, created, action, complaint_id, created)
            for complaint_id, created, action, _customer_id, _ in audit
        ],
    )
    return True


def main() -> None:
    url = os.environ["DATABASE_ADMIN_URL"]
    password = os.environ.get("DEMO_STAFF_PASSWORD", "")
    protector = NationalIdProtector(
        decode_key(os.environ["APP_ENCRYPTION_KEY"]),
        decode_key(os.environ["APP_BLIND_INDEX_KEY"]),
    )
    with (
        psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn,
        conn.cursor() as cursor,
    ):
        created = seed(cursor, protector, password)
    print(
        "Seeded 5,000 synthetic complaints and scoped demo staff"
        if created
        else "Demo seed already present; no changes made"
    )


if __name__ == "__main__":
    main()
