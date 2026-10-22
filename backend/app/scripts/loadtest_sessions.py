"""Mint short-lived sessions + fixtures for load tests (k6 / Locust).

Never runs in production. Sessions are issued through the real ``AuthService``
(so they are revocable rows in ``auth_sessions`` and RLS-scoped like any login),
which lets load tests exercise authenticated, scope-filtered paths without
driving OTP/TOTP flows at scale. Output (JSON on stdout)::

    {"customers": [{"access": "...", "company_id": "...", "policy_number": "...",
                    "insurance_type": "...", "mobile": "...", "national_id": "..."}],
     "tracking":  [{"ticket_no": "...", "national_id": "..."}],
     "staff":     [{"access": "...", "email": "..."}]}

Usage: ``python -m app.scripts.loadtest_sessions --customers 200 --staff 20``
(needs the API environment plus ``DATABASE_ADMIN_URL`` to pick fixtures).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import psycopg
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI

from app.db import Actor
from app.security.crypto import NationalIdProtector, decode_key
from app.settings import get_settings


def _fixtures(admin_url: str, customers: int, staff: int) -> dict[str, list[tuple[Any, ...]]]:
    url = admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, prepare_threshold=None) as conn:
        # Intake opens a request for somebody known only by the number they
        # rang or messaged from, so a customer need not have a national id at
        # all. Those rows cannot mint a session, and because the limit is
        # applied over an id ordering, whether any were caught varied from run
        # to run — which is what made this script fail intermittently.
        customer_rows = conn.execute(
            "SELECT DISTINCT ON (c.id) c.id, c.token_version, c.national_id_ciphertext, "
            "t.ticket_no, t.company_id, t.policy_number, c.mobile, t.insurance_type "
            "FROM customers c JOIN complaints t ON t.customer_id = c.id "
            "WHERE c.national_id_ciphertext IS NOT NULL "
            "ORDER BY c.id, t.created_at DESC LIMIT %s",
            (customers,),
        ).fetchall()
        staff_rows = conn.execute(
            "SELECT id, token_version, email FROM staff_users WHERE status = 'active' "
            "ORDER BY email LIMIT %s",
            (staff,),
        ).fetchall()
    return {"customers": customer_rows, "staff": staff_rows}


async def mint(customers: int, staff: int) -> dict[str, list[dict[str, str]]]:
    from app.main import install_state

    settings = get_settings()
    if settings.is_production:
        raise SystemExit("refusing to mint load-test sessions in production")
    fixtures = _fixtures(os.environ["DATABASE_ADMIN_URL"], customers, staff)
    protector = NationalIdProtector(
        settings.encryption_keyring(), decode_key(settings.app_blind_index_key)
    )
    app = FastAPI()
    close = await install_state(app, settings)
    out: dict[str, list[dict[str, str]]] = {"customers": [], "tracking": [], "staff": []}
    try:
        auth = app.state.auth
        for row in fixtures["customers"]:
            cid, version, cipher, ticket_no, company_id, policy, mobile, kind = row
            if cipher is None:
                continue
            try:
                national_id = protector.decrypt(bytes(cipher))
            except (InvalidTag, ValueError):
                continue  # rows written under foreign keys (e.g. test fixtures)
            tokens = await auth.issue_session(
                Actor(cid, "customer"), int(version), "198.51.100.1", "loadtest"
            )
            out["customers"].append(
                {
                    "access": tokens.access,
                    "company_id": str(company_id),
                    "policy_number": str(policy or "POL-LOAD-0001"),
                    "insurance_type": str(kind),
                    "mobile": str(mobile),
                    "national_id": national_id,
                }
            )
            out["tracking"].append({"ticket_no": ticket_no, "national_id": national_id})
        for sid, version, email in fixtures["staff"]:
            tokens = await auth.issue_session(
                Actor(sid, "staff"), int(version), "198.51.100.1", "loadtest"
            )
            out["staff"].append({"access": tokens.access, "email": email})
    finally:
        await close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mint load-test sessions (non-production)")
    parser.add_argument("--customers", type=int, default=200)
    parser.add_argument("--staff", type=int, default=20)
    args = parser.parse_args(argv)
    json.dump(asyncio.run(mint(args.customers, args.staff)), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
