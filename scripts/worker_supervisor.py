"""Supervise the ARQ worker so a transient Redis/DB outage does not kill it silently.

The local stack (`scripts/local_stack.py`) runs without Docker's `restart: unless-stopped`,
so when the Redis broker disappears the `arq` process exits with a ConnectionError
(finding L-8 in QA_REPORT.md). In production the Compose file already restarts the
worker; locally we need the same resilience or the next `deliver_notices` cycle
never runs and customers stop receiving e-mails.

This supervisor loops forever: it starts `arq app.worker.WorkerSettings` as a child,
streams its output to the same log file, and restarts it after a short backoff.
It exits only when it receives SIGTERM/SIGINT itself (via `make local-down`).

Usage:
    python scripts/worker_supervisor.py  # invoked by local_stack.py, not directly
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / ".local-stack" / "logs"


def main() -> int:
    env = os.environ.copy()
    log_path = LOGS / "worker.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    backoff = 2.0
    consecutive_failures = 0

    def _sigterm(_signum, _frame):  # type: ignore[no-untyped-def]
        # The child will be reaped on next loop iteration; exit cleanly.
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    while True:
        print(
            f"[worker-supervisor] starting arq worker (attempt {consecutive_failures + 1})",
            flush=True,
        )
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "arq", "app.worker.WorkerSettings"],
                cwd=str(ROOT / "backend"),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            ret = proc.wait()
        if ret == 0:
            print(
                "[worker-supervisor] worker exited cleanly (0); stopping supervisor",
                flush=True,
            )
            return 0
        consecutive_failures += 1
        # Exponential backoff capped at 30s, reset after a healthy 60s run would require timing;
        # simple fixed backoff is enough for a dev stack.
        sleep_for = min(backoff * (1.5 ** min(consecutive_failures - 1, 4)), 30.0)
        print(
            f"[worker-supervisor] worker exited with {ret}; restarting in {sleep_for:.1f}s",
            flush=True,
        )
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
