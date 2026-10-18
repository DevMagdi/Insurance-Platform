"""Print a fresh Ed25519 JWT key pair as URL-safe base64 PEM environment values."""

from __future__ import annotations

import base64
import subprocess
import tempfile
from pathlib import Path


def generate() -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="insurance-jwt-") as directory:
        private = Path(directory) / "private.pem"
        public = Path(directory) / "public.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
            check=True,
            capture_output=True,
        )
        private.chmod(0o600)
        subprocess.run(
            ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            check=True,
            capture_output=True,
        )

        def encode(path: Path) -> str:
            return base64.urlsafe_b64encode(path.read_bytes()).decode()

        return encode(private), encode(public)


if __name__ == "__main__":
    private_key, public_key = generate()
    print(f"JWT_PRIVATE_KEY_B64={private_key}")
    print(f"JWT_PUBLIC_KEY_B64={public_key}")
