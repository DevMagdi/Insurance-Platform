"""The demo seed must cooperate with an already provisioned database.

``bootstrap_admin`` creates the administrator role (and its permissions)
before the demo data is seeded. The seed therefore has to reuse the existing
system roles instead of inserting a second row with the same name.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.scripts.seed import ROLE_PERMISSIONS, ensure_system_roles


class FakeCursor:
    """Minimal stand-in that records the statements the seed runs."""

    def __init__(self, existing: dict[str, UUID] | None = None) -> None:
        self.existing = dict(existing or {})
        self.inserted: list[tuple[str, UUID, str]] = []
        self.role_permissions: list[tuple[UUID, str]] = []
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        statement = " ".join(sql.split())
        self.executed.append(statement)
        if statement == "SELECT id FROM roles WHERE name = %s":
            role_id = self.existing.get(params[0])
            self._row = (role_id,) if role_id else None
        elif statement.startswith("INSERT INTO roles("):
            role_id, name, description = params
            self.inserted.append((name, role_id, description))
            self.existing[name] = role_id
            self._row = None
        else:  # pragma: no cover - the helper only issues these statements
            raise AssertionError(f"unexpected statement: {statement}")
        return self

    def executemany(self, sql: str, params: Any) -> None:
        statement = " ".join(sql.split())
        self.executed.append(statement)
        if statement.startswith("INSERT INTO role_permissions("):
            self.role_permissions.extend(params)
        else:  # pragma: no cover - the helper only issues these statements
            raise AssertionError(f"unexpected statement: {statement}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


def test_the_seed_creates_the_missing_system_roles() -> None:
    cursor = FakeCursor()
    roles = ensure_system_roles(cursor)
    assert set(roles) == set(ROLE_PERMISSIONS)
    assert len(cursor.inserted) == len(ROLE_PERMISSIONS)
    assert len(cursor.role_permissions) == sum(len(grants) for grants in ROLE_PERMISSIONS.values())


def test_the_seed_reuses_a_role_that_already_exists() -> None:
    """The administrator role of ``bootstrap_admin`` must not be duplicated."""
    existing = {"administrator": uuid4()}
    cursor = FakeCursor(existing)
    roles = ensure_system_roles(cursor)
    assert roles["administrator"] == existing["administrator"]
    assert "administrator" not in {name for name, _, _ in cursor.inserted}
    assert len(cursor.inserted) == len(ROLE_PERMISSIONS) - 1


def test_the_role_permissions_are_not_duplicated() -> None:
    cursor = FakeCursor()
    ensure_system_roles(cursor)
    assert any(
        "ON CONFLICT (role_id, permission_code) DO NOTHING" in sql for sql in cursor.executed
    )
