"""Seed synthetic CRM actors and records for disposable UI-parity CI runs only."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from argon2 import PasswordHasher

from app.domain.permissions import PERMISSIONS, permission_parts
from app.scripts.seed import ensure_system_roles

TENANT_ID = "00000000-0000-4000-8000-0000000000a1"


def main() -> None:
    admin_url = os.environ["DATABASE_ADMIN_URL"].replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    password = os.environ["DEMO_STAFF_PASSWORD"]
    if len(password) < 12:
        raise ValueError("DEMO_STAFF_PASSWORD must be at least 12 characters")

    now = datetime.now(UTC)
    manager_id, seller_a_id, seller_b_id = uuid4(), uuid4(), uuid4()
    client_id, converted_lead_id, seller_b_lead_id = uuid4(), uuid4(), uuid4()
    hasher = PasswordHasher()
    staff_rows = (
        (manager_id, "manager@ui-parity.example.test", "UI Parity Manager", "account_manager"),
        (seller_a_id, "seller-a@ui-parity.example.test", "Seller A", "sales"),
        (seller_b_id, "seller-b@ui-parity.example.test", "Seller B", "sales"),
    )

    with psycopg.connect(admin_url) as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO permissions(code, resource, action) VALUES (%s, %s, %s) "
            "ON CONFLICT (code) DO NOTHING",
            [(code, *permission_parts(code)) for code in sorted(PERMISSIONS)],
        )
        roles = ensure_system_roles(cursor)
        cursor.executemany(
            "INSERT INTO staff_users(id, email, full_name, password_hash, status, "
            "must_change_password) VALUES (%s, %s, %s, %s, 'active', false)",
            [
                (staff_id, email, full_name, hasher.hash(password))
                for staff_id, email, full_name, _role in staff_rows
            ],
        )
        cursor.executemany(
            "INSERT INTO user_roles(user_id, role_id) VALUES (%s, %s)",
            [(staff_id, roles[role]) for staff_id, _email, _name, role in staff_rows],
        )
        cursor.executemany(
            "INSERT INTO user_company_scope(user_id, company_id, all_companies) "
            "VALUES (%s, NULL, true)",
            [(staff_id,) for staff_id, *_ in staff_rows],
        )
        cursor.executemany(
            "INSERT INTO user_ownership_level(user_id, level) VALUES (%s, %s)",
            [(manager_id, "all"), (seller_a_id, "own"), (seller_b_id, "own")],
        )
        cursor.execute(
            "INSERT INTO corporate_clients(id, slug, name_ar, name_en, contact_name, "
            "contact_email, contact_mobile) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                client_id,
                "ui-parity-converted-client",
                "شركة اختبار التحويل",
                "UI Parity Converted Client",
                "Seller A Contact",
                "converted-client@ui-parity.example.test",
                "+201011111111",
            ),
        )
        cursor.execute(
            "INSERT INTO crm_leads(id, tenant_id, lead_no, full_name, company_name, phone, "
            "email, source, stage_code, assigned_to, converted_corporate_client_id, "
            "converted_at, closed_at, created_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual', 'converted', "
            "%s, %s, %s, %s, %s, %s, %s)",
            (
                converted_lead_id,
                TENANT_ID,
                "LEAD-CI-CONVERTED-A",
                "Seller A Contact",
                "UI Parity Converted Client",
                "+201011111111",
                "converted-client@ui-parity.example.test",
                seller_a_id,
                client_id,
                now,
                now,
                seller_a_id,
                now,
                now,
            ),
        )
        cursor.execute(
            "INSERT INTO crm_leads(id, tenant_id, lead_no, full_name, company_name, phone, "
            "email, source, stage_code, assigned_to, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual', 'new', %s, %s)",
            (
                seller_b_lead_id,
                TENANT_ID,
                "LEAD-CI-SELLER-B",
                "Seller B Contact",
                "Seller B Prospect",
                "+201022222222",
                "seller-b-prospect@ui-parity.example.test",
                seller_b_id,
                seller_b_id,
            ),
        )
        cursor.executemany(
            "INSERT INTO crm_activities(lead_id, type, description, created_by) "
            "VALUES (%s, 'assignment', %s, %s)",
            [
                (converted_lead_id, "Assigned to Seller A for UI parity CI", manager_id),
                (seller_b_lead_id, "Assigned to Seller B for UI parity CI", manager_id),
            ],
        )

    print("Seeded CI-only manager, sellers A/B, two leads, and one converted client")


if __name__ == "__main__":
    main()
