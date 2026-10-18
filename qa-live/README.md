# Live QA harness (browser-less E2E against the running stack)

These scripts were used for the rigorous live end-to-end pass documented in
[`QA_REPORT.md`](../QA_REPORT.md). They are committed here so the run is fully
reproducible; the recorded evidence files (`results_*.json`) are the exact
output of the runs quoted in the report.

## What is in here

| File | Purpose |
| --- | --- |
| `qa_lib.py` | Shared HTTP client (manual cookie jar, X-Forwarded-For rotation, CSRF/Idempotency helpers), mail-sink reader, OTP extractor, check `Recorder`. |
| `mail_sink.py` | aiosmtpd STARTTLS sink on `0.0.0.0:2525` that writes every delivered message to `mail/*.eml` next to the script. |
| `e2e_core.py` | Part 1 (S1–S4): platform health, **customer registration/OTP/identification (highest priority)**, complaint creation/idempotency/attachments/CSAT, body limits, XSS/SQLi/Origin abuse. 74 checks. |
| `e2e_staff.py` | Part 2 (S5–S12): staff login + forced password change, invitation onboarding (e-mail token, profile, avatar), permission matrix, RLS scope enforcement, session security (rotation/replay/family revocation/force-logout), TOTP 2FA, full complaint lifecycle (assign → reply → note → escalate → resolve → reopen → CSAT), NID reveal + audit, CSV export, WebSocket, metrics, abuse detector. 85 checks. |
| `e2e_resilience.py` | Part 3 (R1–R4): SMTP provider down during registration, Redis down, Postgres down (with recovery), JWT hardening (expired / `alg=none` / HS256 confusion / kid attacks / type confusion). 34 checks. |
| `results_core.json`, `results_staff.json`, `results_resilience.json` | Recorded evidence of the final green runs (2026-10-18). |

## How to run

The scripts assume the canonical working location **`.local-stack/qa/`** inside
the repository checkout (mail directory, `../../.env`, and repo-root discovery
are relative to it):

```bash
cp qa-live/*.py .local-stack/qa/          # once
make init && make local-up && make local-bootstrap-admin && make local-seed
.venv/bin/python .local-stack/qa/mail_sink.py &   # STARTTLS sink on :2525
.venv/bin/python .local-stack/qa/e2e_core.py
.venv/bin/python .local-stack/qa/e2e_staff.py
.venv/bin/python .local-stack/qa/e2e_resilience.py
```

Prerequisites / environment notes (all documented in the report):

- `.env` must point `SMTP_HOST/SMTP_PORT` at the sink (an IP literal —
  `SMTPDelivery` refuses hostname-based local sinks by design), enable
  STARTTLS, and pin the sink CA via `SSL_CERT_FILE`. The sink needs a
  server certificate whose SAN includes the sink IP (`169.254.0.21` was used
  here; regenerate with `openssl` — CA + server cert + combined CA bundle).
- `DEMO_STAFF_PASSWORD` must satisfy the 12-char minimum (seed requirement).
- Extra venv packages used by the harness: `websockets`, `pyotp`, `Pillow`.
- **State carryover between runs** is real (national-ID blind index, mobile
  rate windows, Redis buckets): the scripts randomize identities/IPs per run.
  After a staff run, passwords stay rotated (`staff_pw_rotated.flag`) and the
  admin TOTP secret persists (`totp_admin_secret.txt`); delete both files and
  re-seed for a clean-room pass.
- `e2e_resilience.py` kills and restarts the SMTP sink, Redis and Postgres and
  self-heals the arq worker (which exits when its Redis broker disappears —
  finding L-8 in the report).
