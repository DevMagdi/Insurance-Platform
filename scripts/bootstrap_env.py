"""Create a local-only .env without ever checking credentials into Git."""

from __future__ import annotations

import base64
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".env"


def bootstrap() -> Path:
    if TARGET.exists():
        raise FileExistsError(f"{TARGET} already exists; refusing to overwrite secrets")

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(36),
        "APP_DB_PASSWORD": secrets.token_urlsafe(36),
        "APP_WORKER_DB_PASSWORD": secrets.token_urlsafe(36),
        "APP_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode(),
        "APP_BLIND_INDEX_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode(),
        "JWT_SIGNING_KEY": secrets.token_urlsafe(48),
    }
    for name, value in values.items():
        example = "\n".join(
            f"{name}={value}" if line.startswith(f"{name}=") else line
            for line in example.split("\n")
        )
    TARGET.write_text(example, encoding="utf-8")
    TARGET.chmod(0o600)
    return TARGET


if __name__ == "__main__":
    print(f"Created {bootstrap()} (mode 0600). Set DEMO_STAFF_PASSWORD before seeding.")
