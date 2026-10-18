"""Create the first staff administrator without loading any demo data.

``docker compose up`` and the Alembic migrations never create staff accounts:
an empty installation is intentionally sign-in-free until an operator runs this
manual, one-shot command. Unlike :mod:`app.scripts.seed` it creates **only** the
administrator account, the ``administrator`` role with the full permission
catalog and the global (all-companies / all-ownership) scope. It never creates
insurance companies, categories, teams, customers, complaints or any other
business/demo rows.

The command refuses to run when a staff account already exists, so it can never
silently add a second privileged identity to a provisioned database.

Usage (Docker Compose, interactive prompts; no password in shell history)::

    make bootstrap-admin

Usage (automation; never put a real password in a shell history or CI log)::

    BOOTSTRAP_ADMIN_NAME='...' BOOTSTRAP_ADMIN_EMAIL='...' \
    BOOTSTRAP_ADMIN_PASSWORD='...' \
        docker compose run --rm -T bootstrap_admin --non-interactive

The command reads ``DATABASE_ADMIN_URL`` (the migrator/superuser URL, exactly
like the seed command). Afterwards sign in at ``/staff/login`` with the chosen
credentials; the API records the session and the login timestamp on first use.


PowerShell usage (no make involved)::

    # interactive
    docker compose run --rm -T bootstrap_admin
    # or from a configured virtual environment
    python -m app.scripts.bootstrap_admin

    # automation: supply credentials through the environment only
    $env:BOOTSTRAP_ADMIN_NAME   = 'Platform Administrator'
    $env:BOOTSTRAP_ADMIN_EMAIL  = 'admin@example.com'
    $env:BOOTSTRAP_ADMIN_PASSWORD = Read-Host -AsSecureString 'Bootstrap password' |
      ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }
    docker compose run --rm -T bootstrap_admin --non-interactive
    Remove-Item Env:BOOTSTRAP_ADMIN_PASSWORD

Never commit, paste or log the password. See docs/INTEGRATIONS.md section 3.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.domain.permissions import PERMISSIONS, permission_parts
from app.security.passwords import StaffPasswords

ADMIN_ROLE_NAME = "administrator"
ADMIN_ROLE_DESCRIPTION = "Full platform administration (created by bootstrap_admin)"
ENV_NAME = "BOOTSTRAP_ADMIN_NAME"
ENV_EMAIL = "BOOTSTRAP_ADMIN_EMAIL"
ENV_PASSWORD = "BOOTSTRAP_ADMIN_PASSWORD"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BOOTSTRAP_LOCK_KEY = "insurance-platform:bootstrap-admin"


class AlreadyProvisionedError(RuntimeError):
    """A staff account exists, so another privileged identity must not be added."""


@dataclass(frozen=True)
class AdminCredentials:
    full_name: str
    email: str
    password: str


def normalize_full_name(raw: str) -> str:
    """Collapse whitespace and enforce the same bounds as staff invitation."""
    name = " ".join(raw.split())
    if not 2 <= len(name) <= 120:
        raise ValueError("Administrator name must be between 2 and 120 characters")
    return name


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if not 5 <= len(email) <= 254 or EMAIL_PATTERN.match(email) is None:
        raise ValueError("Enter a valid administrator e-mail address")
    return email


def resolve_credentials(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    interactive: bool = True,
    prompt: Callable[[str], str] | None = None,
    secret_prompt: Callable[[str], str] | None = None,
) -> AdminCredentials:
    """Collect credentials from CLI flags, environment variables or prompts."""
    read = prompt or input
    read_secret = secret_prompt or getpass.getpass

    name = (args.name or environ.get(ENV_NAME) or "").strip()
    email = (args.email or environ.get(ENV_EMAIL) or "").strip()
    password = environ.get(ENV_PASSWORD) or ""

    if not name:
        if not interactive:
            raise ValueError(f"{ENV_NAME} is required in non-interactive mode")
        name = read("Administrator full name: ")
    if not email:
        if not interactive:
            raise ValueError(f"{ENV_EMAIL} is required in non-interactive mode")
        email = read("Administrator e-mail: ")
    if not password:
        if not interactive:
            raise ValueError(f"{ENV_PASSWORD} is required in non-interactive mode")
        first = read_secret("Administrator password (12+ characters, letters and digits): ")
        second = read_secret("Repeat password: ")
        if first != second:
            raise ValueError("Passwords do not match")
        password = first

    credentials = AdminCredentials(
        full_name=normalize_full_name(name),
        email=normalize_email(email),
        password=password,
    )
    # Same policy and Argon2 parameters as the API; never log the password.
    StaffPasswords.validate(credentials.password)
    return credentials


def create_initial_administrator(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    credentials: AdminCredentials,
    *,
    passwords: StaffPasswords | None = None,
) -> UUID:
    """Insert exactly one active administrator and its access in one transaction.

    Callers must commit or roll back the surrounding connection. Raises
    :class:`AlreadyProvisionedError` before touching any row when a staff
    account (active, invited or disabled) already exists.
    """
    full_name = normalize_full_name(credentials.full_name)
    email = normalize_email(credentials.email)
    StaffPasswords.validate(credentials.password)
    hasher = passwords or StaffPasswords()

    # Serialize concurrent bootstrap attempts and re-check inside the same
    # transaction, so two operators cannot both observe an empty database.
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (BOOTSTRAP_LOCK_KEY,))
    if cursor.execute("SELECT 1 FROM staff_users LIMIT 1").fetchone() is not None:
        raise AlreadyProvisionedError(
            "A staff account already exists; bootstrap_admin only provisions an empty database"
        )

    cursor.executemany(
        "INSERT INTO permissions(code, resource, action) VALUES (%s, %s, %s) "
        "ON CONFLICT (code) DO NOTHING",
        [(code, *permission_parts(code)) for code in sorted(PERMISSIONS)],
    )

    row = cursor.execute("SELECT id FROM roles WHERE name = %s", (ADMIN_ROLE_NAME,)).fetchone()
    if row is None:
        role_id = uuid4()
        cursor.execute(
            "INSERT INTO roles(id, name, description, is_system) VALUES (%s, %s, %s, true)",
            (role_id, ADMIN_ROLE_NAME, ADMIN_ROLE_DESCRIPTION),
        )
    else:
        role_id = row[0]
        cursor.execute(
            "UPDATE roles SET description = %s, is_system = true WHERE id = %s",
            (ADMIN_ROLE_DESCRIPTION, role_id),
        )
    cursor.execute(
        "INSERT INTO role_permissions(role_id, permission_code) "
        "SELECT %s, code FROM permissions ON CONFLICT DO NOTHING",
        (role_id,),
    )

    staff_id = uuid4()
    cursor.execute(
        "INSERT INTO staff_users(id, email, full_name, password_hash, status, "
        "must_change_password) VALUES (%s, %s, %s, %s, 'active', false)",
        (staff_id, email, full_name, hasher.hash_sync(credentials.password)),
    )
    cursor.execute("INSERT INTO user_roles(user_id, role_id) VALUES (%s, %s)", (staff_id, role_id))
    # Global scope is required for the empty-database dashboard and for every
    # later company. No insurance-type rows means "unrestricted".
    cursor.execute(
        "INSERT INTO user_company_scope(user_id, company_id, all_companies) "
        "VALUES (%s, NULL, true)",
        (staff_id,),
    )
    cursor.execute(
        "INSERT INTO user_ownership_level(user_id, level) VALUES (%s, 'all')",
        (staff_id,),
    )
    cursor.execute(
        "INSERT INTO access_change_log(actor_staff_id, target_user_id, change_type, "
        "before_state, after_state) VALUES (%s, %s, 'bootstrap_admin', '{}'::jsonb, %s)",
        (
            staff_id,
            staff_id,
            Jsonb(
                {
                    "email": email,
                    "roles": [ADMIN_ROLE_NAME],
                    "all_companies": True,
                    "insurance_types": [],
                    "ownership": "all",
                }
            ),
        ),
    )
    return staff_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap_admin",
        description="Create the first administrator in an empty database (no demo data).",
        allow_abbrev=False,
    )
    parser.add_argument("--name", help=f"administrator full name (or {ENV_NAME})")
    parser.add_argument("--email", help=f"administrator e-mail (or {ENV_EMAIL})")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="fail instead of prompting; requires all BOOTSTRAP_ADMIN_* variables",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        credentials = resolve_credentials(
            args,
            env,
            interactive=not args.non_interactive,
        )
    except (EOFError, KeyboardInterrupt):
        print("bootstrap_admin: cancelled", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"bootstrap_admin: {exc}", file=sys.stderr)
        return 2

    database_admin_url = env.get("DATABASE_ADMIN_URL")
    if not database_admin_url:
        print(
            "bootstrap_admin: DATABASE_ADMIN_URL is required "
            "(the migrator/superuser connection URL)",
            file=sys.stderr,
        )
        return 2
    dsn = database_admin_url.replace("postgresql+psycopg://", "postgresql://", 1)

    try:
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            staff_id = create_initial_administrator(cursor, credentials)
    except AlreadyProvisionedError as exc:
        print(f"bootstrap_admin: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        # Connection errors may echo the host but never the password.
        print(f"bootstrap_admin: database error: {exc}", file=sys.stderr)
        return 1

    print("Created the initial administrator account (no demo data was inserted).")
    print(f"  name:   {credentials.full_name}")
    print(f"  email:  {credentials.email}")
    print(f"  user:   {staff_id}")
    print("Sign in at /staff/login with the password you chose.")
    print(
        "The database contains no companies, customers or complaints; add real "
        "business data from the administration console."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
