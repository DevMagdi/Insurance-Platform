"""Shared test configuration."""

from __future__ import annotations

import pytest

from app.settings import Settings

# Unit tests must not inherit the developer's local `.env`; disabling it keeps
# the suite hermetic for developers and CI alike.
Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never resolve real DNS: outbound hosts look like a public address.

    Tests of the SSRF guard override ``resolve_host`` with hostile answers.
    """

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr("app.security.ssrf.resolve_host", fake_resolve)
