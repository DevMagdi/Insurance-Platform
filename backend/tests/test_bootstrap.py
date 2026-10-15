"""Local setup must generate new independent secrets without touching real .env."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.security.crypto import decode_key

ROOT = Path(__file__).resolve().parents[2]


def test_bootstrap_refuses_overwrite_and_uses_separate_strong_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "bootstrap_env", ROOT / "scripts/bootstrap_env.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text())
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "TARGET", tmp_path / ".env")
    generated = module.bootstrap()
    values = dict(
        line.split("=", maxsplit=1)
        for line in generated.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert (generated.stat().st_mode & 0o777) == 0o600
    assert decode_key(values["APP_ENCRYPTION_KEY"]) != decode_key(values["APP_BLIND_INDEX_KEY"])
    assert len(values["POSTGRES_PASSWORD"]) > 32
    assert values["POSTGRES_PASSWORD"] != values["APP_DB_PASSWORD"]
    with pytest.raises(FileExistsError):
        module.bootstrap()
