"""bootstrap_admin: one administrator, no demo data, refuse provisioned databases."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from argon2 import PasswordHasher

from app.domain.permissions import PERMISSIONS
from app.scripts import bootstrap_admin
from app.scripts.bootstrap_admin import (
    AdminCredentials,
    AlreadyProvisionedError,
    create_initial_administrator,
    normalize_email,
    normalize_full_name,
    resolve_credentials,
)

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct-horse-9"
FORBIDDEN_TABLES = (
    "insurance_companies",
    "customers",
    "complaints",
    "complaint_keys",
    "teams",
    "team_members",
    "categories",
    "policies",
    "sla_rules",
    "canned_replies",
)


class FakeCursor:
    """Minimal in-memory stand-in that fails loudly on any unexpected SQL."""

    def __init__(self, staff: list[dict[str, Any]] | None = None) -> None:
        self.permissions: dict[str, tuple[str, str]] = {}
        self.roles: list[dict[str, Any]] = []
        self.role_permissions: set[tuple[UUID, str]] = set()
        self.staff: list[dict[str, Any]] = list(staff or [])
        self.role_assignments: list[tuple[UUID, UUID]] = []
        self.company_scopes: list[tuple[UUID, bool]] = []
        self.ownership: list[tuple[UUID, str]] = []
        self.access_changes: list[dict[str, Any]] = []
        self.executed: list[str] = []
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        statement = " ".join(sql.split())
        self.executed.append(statement)
        values = tuple(params or ())
        if statement == "SELECT pg_advisory_xact_lock(hashtext(%s))":
            self._result = [(None,)]
        elif statement == "SELECT 1 FROM staff_users LIMIT 1":
            self._result = [(1,)] if self.staff else []
        elif statement.startswith("INSERT INTO permissions"):
            self.permissions.setdefault(values[0], (values[1], values[2]))
            self._result = []
        elif statement == "SELECT id FROM roles WHERE name = %s":
            matches = [role["id"] for role in self.roles if role["name"] == values[0]]
            self._result = [(matches[0],)] if matches else []
        elif statement.startswith("INSERT INTO roles"):
            self.roles.append(
                {
                    "id": values[0],
                    "name": values[1],
                    "description": values[2],
                    "is_system": True,
                }
            )
            self._result = []
        elif statement.startswith("UPDATE roles"):
            for role in self.roles:
                if role["id"] == values[1]:
                    role["description"] = values[0]
                    role["is_system"] = True
            self._result = []
        elif statement.startswith("INSERT INTO role_permissions"):
            self.role_permissions.update((values[0], code) for code in self.permissions)
            self._result = []
        elif statement.startswith("INSERT INTO staff_users"):
            self.staff.append(
                {
                    "id": values[0],
                    "email": values[1],
                    "full_name": values[2],
                    "password_hash": values[3],
                    "status": "active",
                    "must_change_password": False,
                }
            )
            self._result = []
        elif statement.startswith("INSERT INTO user_roles"):
            self.role_assignments.append((values[0], values[1]))
            self._result = []
        elif statement.startswith("INSERT INTO user_company_scope"):
            self.company_scopes.append((values[0], True))
            self._result = []
        elif statement.startswith("INSERT INTO platform_super_admin"):
            self.platform_super_admin = values[0]
            self._result = []
        elif statement.startswith("INSERT INTO user_ownership_level"):
            self.ownership.append((values[0], "all"))
            self._result = []
        elif statement.startswith("INSERT INTO access_change_log"):
            self.access_changes.append(
                {"actor": values[0], "target": values[1], "after": values[2]}
            )
            self._result = []
        else:
            raise AssertionError(f"unexpected SQL executed by bootstrap_admin: {statement}")
        return self

    def executemany(self, sql: str, params_seq: Any) -> FakeCursor:
        for params in params_seq:
            self.execute(sql, params)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor


def credentials() -> AdminCredentials:
    return AdminCredentials("Mona Admin", "Admin@Example.COM", PASSWORD)


def env(**overrides: str) -> dict[str, str]:
    values = {
        "DATABASE_ADMIN_URL": "postgresql+psycopg://admin:secret@postgres:5432/insurance",
        bootstrap_admin.ENV_NAME: "Mona Admin",
        bootstrap_admin.ENV_EMAIL: "admin@example.test",
        bootstrap_admin.ENV_PASSWORD: PASSWORD,
    }
    values.update(overrides)
    return values


def args(*, name: str | None = None, email: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(name=name, email=email, non_interactive=False)


def test_create_initial_administrator_creates_one_scoped_admin_without_demo_rows() -> None:
    cursor = FakeCursor()
    staff_id = create_initial_administrator(cursor, credentials())

    assert len(cursor.staff) == 1
    admin = cursor.staff[0]
    assert admin["id"] == staff_id
    assert admin["email"] == "admin@example.com"  # normalized
    assert admin["full_name"] == "Mona Admin"
    assert admin["status"] == "active"
    assert admin["must_change_password"] is False
    assert admin["password_hash"].startswith("$argon2id$")
    assert admin["password_hash"] != PASSWORD
    assert PasswordHasher().verify(admin["password_hash"], PASSWORD)

    [role] = cursor.roles
    assert role["name"] == "administrator"
    assert role["is_system"] is True
    assert cursor.role_permissions == {(role["id"], code) for code in PERMISSIONS}
    assert cursor.role_assignments == [(staff_id, role["id"])]

    # Global scope and unrestricted insurance types (no rows) make the empty
    # dashboard and every future company reachable without demo data.
    assert cursor.company_scopes == [(staff_id, True)]
    assert cursor.ownership == [(staff_id, "all")]

    [change] = cursor.access_changes
    assert change["actor"] == staff_id
    assert change["target"] == staff_id
    assert change["after"].obj == {
        "email": "admin@example.com",
        "roles": ["administrator"],
        "all_companies": True,
        "insurance_types": [],
        "ownership": "all",
    }

    executed = "\n".join(cursor.executed)
    for table in FORBIDDEN_TABLES:
        assert f"INTO {table}" not in executed
        assert f"UPDATE {table}" not in executed


def test_create_initial_administrator_refuses_an_existing_staff_account() -> None:
    existing = {"id": uuid4(), "email": "someone@example.com", "status": "disabled"}
    cursor = FakeCursor(staff=[existing])
    with pytest.raises(AlreadyProvisionedError, match="already exists"):
        create_initial_administrator(cursor, credentials())

    # Only the lock and the existence probe ran: no permission, role or account
    # row was touched, so a provisioned database cannot gain a second admin.
    assert cursor.executed == [
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        "SELECT 1 FROM staff_users LIMIT 1",
    ]
    assert cursor.staff == [existing]
    assert cursor.permissions == {}
    assert cursor.roles == []
    assert cursor.role_permissions == set()


def test_create_initial_administrator_reuses_an_existing_administrator_role() -> None:
    role_id = uuid4()
    cursor = FakeCursor()
    cursor.roles.append(
        {"id": role_id, "name": "administrator", "description": "old", "is_system": False}
    )

    staff_id = create_initial_administrator(cursor, credentials())

    [role] = cursor.roles
    assert role["id"] == role_id
    assert role["description"] == bootstrap_admin.ADMIN_ROLE_DESCRIPTION
    assert role["is_system"] is True
    assert cursor.role_permissions == {(role_id, code) for code in PERMISSIONS}
    assert cursor.role_assignments == [(staff_id, role_id)]


def test_normalizers_keep_server_side_bounds_and_lowercase_email() -> None:
    assert normalize_full_name("  Mona   Admin \n") == "Mona Admin"
    assert normalize_email(" Admin@Example.COM ") == "admin@example.com"
    with pytest.raises(ValueError, match="2 and 120"):
        normalize_full_name(" x ")
    with pytest.raises(ValueError, match="e-mail"):
        normalize_email("not-an-email")


def test_resolve_credentials_prefers_flags_then_environment_without_prompting() -> None:
    def fail(_: str) -> str:
        raise AssertionError("prompt must not be used when values are supplied")

    resolved = resolve_credentials(
        args(name="  Layla   Manager "),
        env(),
        interactive=False,
        prompt=fail,
        secret_prompt=fail,
    )
    assert resolved == AdminCredentials("Layla Manager", "admin@example.test", PASSWORD)


def test_resolve_credentials_prompts_and_confirms_password() -> None:
    answers = iter([PASSWORD, PASSWORD])
    resolved = resolve_credentials(
        args(),
        {},
        interactive=True,
        prompt=lambda _: "Mona Admin" if "name" in _.lower() else "admin@example.test",
        secret_prompt=lambda _: next(answers),
    )
    assert resolved.email == "admin@example.test"
    assert resolved.password == PASSWORD

    mismatched = iter([PASSWORD, f"{PASSWORD}-other"])
    with pytest.raises(ValueError, match="do not match"):
        resolve_credentials(
            args(),
            {},
            interactive=True,
            prompt=lambda _: "Mona Admin" if "name" in _.lower() else "admin@example.test",
            secret_prompt=lambda _: next(mismatched),
        )


@pytest.mark.parametrize(
    ("provided", "message"),
    [
        (env(**{bootstrap_admin.ENV_PASSWORD: "short1"}), "12-128"),
        (env(**{bootstrap_admin.ENV_EMAIL: "bad"}), "e-mail"),
        (env(**{bootstrap_admin.ENV_NAME: "x"}), "2 and 120"),
        ({}, "BOOTSTRAP_ADMIN_NAME"),
    ],
)
def test_resolve_credentials_rejects_invalid_non_interactive_input(
    provided: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_credentials(args(), provided, interactive=False)


def test_non_interactive_reports_each_missing_variable() -> None:
    with pytest.raises(ValueError, match="BOOTSTRAP_ADMIN_EMAIL"):
        resolve_credentials(args(name="Mona Admin"), {}, interactive=False)
    with pytest.raises(ValueError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        resolve_credentials(
            args(name="Mona Admin", email="admin@example.test"), {}, interactive=False
        )


def test_main_requires_the_admin_database_url(capsys: pytest.CaptureFixture[str]) -> None:
    without_url = {key: value for key, value in env().items() if key != "DATABASE_ADMIN_URL"}
    assert bootstrap_admin.main([], without_url) == 2
    assert "DATABASE_ADMIN_URL" in capsys.readouterr().err


def test_main_connects_with_plain_dsn_and_never_prints_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cursor = FakeCursor()
    seen: list[str] = []

    def fake_connect(dsn: str) -> FakeConnection:
        seen.append(dsn)
        return FakeConnection(cursor)

    monkeypatch.setattr(bootstrap_admin.psycopg, "connect", fake_connect)
    assert bootstrap_admin.main([], env()) == 0

    assert seen == ["postgresql://admin:secret@postgres:5432/insurance"]
    captured = capsys.readouterr()
    assert "admin@example.test" in captured.out
    assert PASSWORD not in captured.out + captured.err
    assert "no demo data" in captured.out
    assert "/staff/login" in captured.out
    assert len(cursor.staff) == 1


def test_main_reports_an_already_provisioned_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cursor = FakeCursor(staff=[{"id": uuid4(), "email": "someone@example.com"}])
    monkeypatch.setattr(bootstrap_admin.psycopg, "connect", lambda dsn: FakeConnection(cursor))

    assert bootstrap_admin.main(["--non-interactive"], env()) == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert len(cursor.staff) == 1
    assert cursor.roles == []


def test_main_reports_database_errors_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_connect(dsn: str) -> FakeConnection:
        raise bootstrap_admin.psycopg.OperationalError("connection failed")

    monkeypatch.setattr(bootstrap_admin.psycopg, "connect", failing_connect)
    assert bootstrap_admin.main([], env()) == 1
    captured = capsys.readouterr()
    assert "database error" in captured.err
    assert "secret" not in captured.err


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_command_is_opt_in_and_documented(filename: str) -> None:
    compose = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))
    service = compose["services"]["bootstrap_admin"]
    # A tools profile means `docker compose up` never creates the account.
    assert service["profiles"] == ["tools"]
    assert service["command"] == ["python", "-m", "app.scripts.bootstrap_admin"]
    assert "DATABASE_ADMIN_URL" in service["environment"]
    assert service["depends_on"]["migrate"]["condition"] == "service_completed_successfully"

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "bootstrap-admin:\n\t$(COMPOSE) run --rm bootstrap_admin" in makefile
    assert "local-bootstrap-admin:\n\t$(PYTHON) scripts/local_stack.py bootstrap-admin" in makefile
    module = (ROOT / "backend" / "app" / "scripts" / "bootstrap_admin.py").read_text(
        encoding="utf-8"
    )
    assert "from app.scripts.seed" not in module
    assert "import seed" not in module
    assert "INSERT INTO insurance_companies" not in module
