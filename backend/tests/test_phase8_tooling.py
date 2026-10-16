"""Phase 8 operational tooling: load-test session minting and backup scripts."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.scripts import loadtest_sessions
from app.security.crypto import NationalIdProtector, decode_key
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
NID = "29901010112345"


def _key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode()


def _settings(environment: str = "development") -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=_key(b"e"),
        APP_BLIND_INDEX_KEY=_key(b"b"),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT=environment,
    )


class _FakeAuth:
    def __init__(self) -> None:
        self.issued: list[tuple[Any, int]] = []

    async def issue_session(self, actor: Any, version: int, ip: str, agent: str) -> Any:
        self.issued.append((actor, version))
        return SimpleNamespace(access=f"token-{actor.scope}-{len(self.issued)}")


def _install(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, rows: dict[str, Any]
) -> _FakeAuth:
    auth = _FakeAuth()
    closed: list[bool] = []

    async def fake_install_state(app: FastAPI, _: Settings) -> Any:
        app.state.auth = auth

        async def close() -> None:
            closed.append(True)

        return close

    monkeypatch.setattr(loadtest_sessions, "get_settings", lambda: settings)
    monkeypatch.setattr(loadtest_sessions, "_fixtures", lambda url, c, s: rows)
    monkeypatch.setattr("app.main.install_state", fake_install_state)
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql+psycopg://admin@localhost/test")
    auth.closed = closed  # type: ignore[attr-defined]
    return auth


def test_loadtest_sessions_mints_real_sessions_and_skips_foreign_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    protector = NationalIdProtector(
        settings.encryption_keyring(), decode_key(settings.app_blind_index_key)
    )
    good, company, staff_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = {
        "customers": [
            (
                good,
                3,
                protector.encrypt(NID),
                "IC-1",
                company,
                "POL-12345",
                "01012345678",
                "medical",
            ),
            # Written under a different key (e.g. leftover test data): skipped, not fatal.
            (uuid.uuid4(), 1, b"\x00" * 40, "IC-2", company, None, "01012345679", "car"),
        ],
        "staff": [(staff_id, 7, "admin@demo.example")],
    }
    auth = _install(monkeypatch, settings, rows)
    out = io.StringIO()
    with redirect_stdout(out):
        assert loadtest_sessions.main(["--customers", "2", "--staff", "1"]) == 0
    payload = json.loads(out.getvalue())
    assert payload["tracking"] == [{"ticket_no": "IC-1", "national_id": NID}]
    [customer] = payload["customers"]
    assert customer["company_id"] == str(company)
    assert customer["national_id"] == NID and customer["insurance_type"] == "medical"
    assert payload["staff"] == [{"access": "token-staff-2", "email": "admin@demo.example"}]
    assert [(a.scope, v) for a, v in auth.issued] == [("customer", 3), ("staff", 7)]
    assert auth.closed == [True]  # type: ignore[attr-defined]


def test_loadtest_sessions_refuses_production(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(is_production=True)
    monkeypatch.setattr(loadtest_sessions, "get_settings", lambda: settings)
    with pytest.raises(SystemExit, match="production"):
        loadtest_sessions.main([])


@pytest.mark.parametrize("script", ["pg_backup.sh", "pg_restore_drill.sh"])
def test_backup_scripts_are_posix_and_fail_fast(script: str) -> None:
    path = ROOT / "scripts" / "backup" / script
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n") and "set -eu" in text
    subprocess.run(["sh", "-n", str(path)], check=True)


def test_restore_drill_verifies_rls_and_revision() -> None:
    drill = (ROOT / "scripts/backup/pg_restore_drill.sh").read_text()
    assert "SET LOCAL ROLE app_runtime" in drill
    assert "rolbypassrls" in drill and "relrowsecurity" in drill
    assert "EXPECTED_REVISION" in drill and "sha256sum -c" in drill
    backup = (ROOT / "scripts/backup/pg_backup.sh").read_text()
    assert "pg_restore --list" in backup  # an unlistable dump fails the backup
    assert "umask 077" in backup


def test_ci_runs_load_smoke_restore_drill_scans_and_helm() -> None:
    import yaml

    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = ci["jobs"]
    assert {"backend", "frontend", "containers", "security", "helm", "postgres-rls"} <= set(jobs)
    rls = yaml.dump(jobs["postgres-rls"])
    assert "loadtest_sessions" in rls and "grafana/k6" in rls and "PROFILE=smoke" in rls
    assert "pg_restore_drill.sh" in rls
    security = yaml.dump(jobs["security"])
    assert "pip_audit" in security and "npm audit" in security and "trivy fs" in security
    containers = yaml.dump(jobs["containers"])
    assert "nginx -t" in containers and "trivy image" in containers
    assert "sha256sum -c" in yaml.safe_dump(jobs["containers"], width=10_000)
    helm = yaml.dump(jobs["helm"])
    assert "helm lint" in helm and "kubeconform" in helm and "values-production.yaml" in helm
    assert "lint_migrations" in yaml.dump(jobs["backend"])


@pytest.fixture
async def privacy_api(monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    from app.api import privacy as privacy_api_module
    from app.api.deps import RequestContext, authenticated_context
    from app.db import Actor
    from app.domain.access import AccessScope
    from app.domain.enums import OwnershipLevel
    from app.main import app

    holder: dict[str, Any] = {}
    repo = SimpleNamespace(
        latest_consents=AsyncMock(return_value=[{"purpose": "notifications", "granted": True}]),
        record_consent=AsyncMock(return_value=uuid.uuid4()),
        own_complaints=AsyncMock(return_value=[{"ticket_no": "IC-1"}]),
        consent_history=AsyncMock(return_value=[]),
        requests=AsyncMock(return_value=[{"kind": "access", "status": "pending"}]),
        create_request=AsyncMock(return_value=uuid.uuid4()),
        decide=AsyncMock(return_value=True),
    )
    profile = SimpleNamespace(profile=AsyncMock(return_value={"full_name": "Test"}))
    monkeypatch.setattr(privacy_api_module, "PrivacyRepository", lambda: repo)
    monkeypatch.setattr(privacy_api_module, "CustomerProfileRepository", lambda: profile)
    monkeypatch.setattr(app.state, "settings", _settings(), raising=False)

    def context(scope: str, permissions: frozenset[str] = frozenset()) -> RequestContext:
        actor = Actor(uuid.uuid4(), scope)  # type: ignore[arg-type]
        access = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
        return RequestContext(
            SimpleNamespace(), actor, uuid.uuid4(), 1, access, permissions, {}, SimpleNamespace()
        )

    async def override() -> Any:
        yield holder["ctx"]

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "value"},
        ) as client:
            yield client, holder, context, repo
    finally:
        app.dependency_overrides.clear()


async def test_privacy_endpoints_bind_to_customer_and_require_csrf(privacy_api: Any) -> None:
    client, holder, context, repo = privacy_api
    csrf = {"X-CSRF-Token": "value"}
    holder["ctx"] = context("customer")
    assert (await client.get("/api/customer/privacy/consents")).status_code == 200
    body = {"purpose": "marketing", "granted": False}
    assert (await client.post("/api/customer/privacy/consents", json=body)).status_code == 403
    created = await client.post("/api/customer/privacy/consents", json=body, headers=csrf)
    assert created.status_code == 201 and created.json()["policy_version"]
    exported = (await client.get("/api/customer/privacy/export")).json()
    assert exported["complaints"] == [{"ticket_no": "IC-1"}] and "generated_at" in exported
    assert (await client.get("/api/customer/privacy/requests")).status_code == 200
    req = await client.post(
        "/api/customer/privacy/requests", json={"kind": "erasure"}, headers=csrf
    )
    assert req.status_code == 202
    repo.create_request.return_value = None
    denied = await client.post(
        "/api/customer/privacy/requests", json={"kind": "access"}, headers=csrf
    )
    assert denied.status_code == 403
    repo.record_consent.return_value = None
    refused = await client.post("/api/customer/privacy/consents", json=body, headers=csrf)
    assert refused.status_code == 403
    # Customers never reach the staff queue.
    assert (await client.get("/api/staff/privacy/requests")).status_code in (401, 403)


async def test_staff_privacy_queue_requires_settings_manage(privacy_api: Any) -> None:
    client, holder, context, repo = privacy_api
    csrf = {"X-CSRF-Token": "value"}
    rid = uuid.uuid4()
    holder["ctx"] = context("staff")
    assert (await client.get("/api/staff/privacy/requests")).status_code == 403
    holder["ctx"] = context("staff", frozenset({"settings.manage"}))
    queue = await client.get("/api/staff/privacy/requests", params={"status": "pending"})
    assert queue.status_code == 200
    decision = {"status": "completed", "resolution": "done"}
    url = f"/api/staff/privacy/requests/{rid}"
    assert (await client.post(url, json=decision)).status_code == 403
    assert (await client.post(url, json=decision, headers=csrf)).json() == {"updated": True}
    repo.decide.return_value = False
    assert (await client.post(url, json=decision, headers=csrf)).status_code == 404
