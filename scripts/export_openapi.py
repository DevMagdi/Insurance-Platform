"""Export the running API schema to docs/openapi.yaml.

Run after installing backend[dev]: PYTHONPATH=backend python scripts/export_openapi.py
The original future-route draft remains in docs/openapi-roadmap.yaml.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    destination = ROOT / "docs/openapi.yaml"
    destination.write_text(
        yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"Exported {destination}")
