# Integration provider setup (PowerShell-first)

This guide covers the **real** provider connections of the Insurance Platform:
e-mail (SMTP / Gmail OAuth / Microsoft 365 Graph / SendGrid / SES / IMAP),
WhatsApp Cloud API, SMS (Twilio), AI analysis (OpenAI), voice follow-up calls
(Twilio + Vonage) with STT/TTS (OpenAI Speech), and malware scanning (ClamAV).

Every example below is PowerShell. Nothing here requires `make`; every `make`
target has an equivalent plain command. No command sends a message or places a
call to a real customer — sending is limited to the sandbox flow with explicit
consent.

---

## 1. Security model

| Rule | Implementation |
| --- | --- |
| Secrets never shown again | The admin API returns `has_credentials`, never credential values. Secret fields are partial updates: a blank field keeps the stored value. |
| Secrets never logged | `SecretCipher` encrypts JSON credentials with AES-256-GCM (`APP_ENCRYPTION_KEY`, per-row AAD `integration_config:{company_id}:{provider}`). Worker failures record sanitized codes only. |
| Secrets at rest | Single encrypted column per row. There is **no** external secret manager integration; anything claiming `vault://` storage would be fiction. |
| Production refuses mocks | The worker raises `mock_provider_forbidden_in_production` when `ENVIRONMENT=production` and a job targets `mock_llm`/`mock_voice`. |
| CSRF + RBAC | All connection endpoints require the `integrations.manage` permission and double-submit CSRF (same as the rest of the staff API). |
| OAuth state | `base64url(payload) + HMAC-SHA256` (key: `APP_IDEMPOTENCY_SECRET`), bound to actor + provider + company, 10-minute expiry, redirect clamped to same-origin `/staff/...`. |
| Webhooks | Provider signatures verified (Twilio SHA-256 HMAC / Meta `X-Hub-Signature-256` / timestamped HMAC for mail events). Unknown senders are rejected before any business processing. |

### Connection states

| State | Arabic label | Meaning |
| --- | --- | --- |
| `unconfigured` | غير مضبوط | No stored credentials. |
| `configured` | مضبوط ولم يُختبر | Credentials stored, no successful test yet. |
| `connected` | متصل | Last probe succeeded (stored + audit event `integration_connection_test`). |
| `failed` | فشل | Last probe or send failed; `last_error_code` carries a log-safe code. |

Scopes: **global** rows apply to every company (`company_id IS NULL`, filled
down at read time); **company** rows override global ones. Gmail/Outlook/IMAP
and WhatsApp are company-only; ClamAV is global-only; everything else supports
both scopes.

---

## 2. One-time environment (PowerShell)

```powershell
# From the repository root. create .env once (docker compose reads it).
Copy-Item deploy\env.example .env   # or: notepad .env
```

Required keys (generate strong values; never commit `.env`):

| Key | Purpose |
| --- | --- |
| `APP_ENCRYPTION_KEY` | 32-byte base64; encrypts integration credentials, queued PII, recordings. Must be identical for API and worker. |
| `APP_IDEMPOTENCY_SECRET` | Signs OAuth state and idempotency keys. |
| `PUBLIC_APP_URL` | External HTTPS base URL; OAuth redirects and webhook callback URLs derive from it. |
| `ENVIRONMENT` | `production` enables the mock-provider refusal and the strictest checks. |
| `INTEGRATION_ENCRYPTION_KEY_B64` | Legacy Phase-4 credential key (still used by ticket channel payloads). |

Generate values without `make`:

```powershell
# 32 random bytes, base64
[Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]])
# or cryptographically:
[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

Start the stack without `make` (equivalent of `make up`):

```powershell
docker compose up -d --build
docker compose run --rm api python -m app.scripts.init_db      # migrations
docker compose run --rm api python -m app.scripts.init_partitions
```

---

## 3. First administrator (PowerShell, no make)

`backend/app/scripts/bootstrap_admin.py` creates **only the first
administrator**: it refuses to run when any staff account exists, prompts for
name/e-mail/12+ character password (letters+digits), stores an Argon2id hash,
grants the `administrator` role the full permission catalog and
all-companies scope, and inserts **no** demo data.

Interactive (local or container):

```powershell
docker compose run --rm -T bootstrap_admin
# equivalent without compose, from a configured virtual environment:
#   python -m app.scripts.bootstrap_admin
```

Automated (CI/CD — supply secrets through the environment; never commit or
log a real password):

```powershell
$env:BOOTSTRAP_ADMIN_NAME   = 'Platform Administrator'
$env:BOOTSTRAP_ADMIN_EMAIL  = 'admin@example.com'
$env:BOOTSTRAP_ADMIN_PASSWORD = Read-Host -AsSecureString 'Bootstrap password' |
  ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }
docker compose run --rm -T bootstrap_admin --non-interactive
Remove-Item Env:BOOTSTRAP_ADMIN_PASSWORD
```

Sign in at `/staff/login`. The account must change nothing else; configure
providers through the staff console (section 5).

---

## 4. Provider credentials (where they come from)

Configure each provider in the staff console: **إعدادات مساحة العمل →
تكاملات**. The UI shows one form per provider with the exact keys below.

| Provider | Keys (secret keys marked •) | Console/vendor notes |
| --- | --- | --- |
| SMTP (send) | `host`, `port`, `start_tls`, `username`, `password`•, `from_email` | Any SMTP relay. Local dev: Mailpit at `mailpit:1025`, no TLS/auth. |
| Gmail (send+inbox) | `client_id`, `client_secret`•, `refresh_token`• (or the OAuth button), `from_email`, `mailbox` | Google Cloud Console → OAuth client (web) + `https://www.googleapis.com/auth/gmail.send` + `gmail.modify` scopes. App-passwords are **not** supported. |
| Outlook (send+inbox) | `client_id`, `client_secret`•, `refresh_token`• (or the OAuth button), `from_email`, `tenant`, `mailbox` | Entra ID app registration, `Mail.Send` + `Mail.Read` delegated scopes, public-client refresh tokens. |
| SendGrid | `api_key`•, `from_email` | API key with `mail.send`. |
| SES | `region`, `from_email`, `smtp_username`, `smtp_password`• | SES SMTP credentials (IAM-generated). |
| IMAP (inbox) | `host`, `port`, `username`, `password`•, `mailbox`, `use_ssl` | Company-scoped generic IMAP. Gmail/Outlook must use their OAuth providers instead. |
| WhatsApp Cloud | `phone_number_id`, `access_token`•, `app_secret`•, `verify_token`•, `api_version` | Meta app → WhatsApp → API Setup. `app_secret` verifies webhooks; `verify_token` is your own random string for subscription checks. |
| Twilio SMS | `account_sid`, `auth_token`•, `from_number`, `status_callback_url`, `allowed_country_codes`, `sandbox_recipient` | Twilio Console. **You** must confirm the countries where Twilio SMS service is available and list only those (`+20,+966`); the adapter refuses any destination outside the list. |
| OpenAI (analysis) | `api_key`•, `model`, `base_url`, `timeout_seconds`, `max_retries`, `max_tokens`, `daily_request_limit` | Platform key. Requests are budget-capped (default 500/day, `daily_request_limit`) and PII-minimized before egress. |
| Twilio Voice | `account_sid`, `auth_token`•, `from_number`, `status_callback_url` | Voice-capable number. Follow-up calls require the complaint's recorded consent. |
| Vonage Voice | `application_id`, `private_key`•, `from_number`, `status_callback_url` | Vonage application with voice capability + private key. |
| OpenAI Speech | `api_key`•, `stt_model`, `tts_model`, `voice`, `base_url` | Used for call transcription/synthesis. |
| ClamAV | `host`, `port`, `timeout_seconds` | Global-only. `clamd` reachable over TCP (e.g. the `clamav` compose service). Fail-closed: an unreachable scanner never marks a file clean. |

> **Not implemented on purpose** (shown as disabled in the UI): external
> secret vaults, IMAP password access to Gmail/Outlook, non-Twilio SMS
> providers. Do not configure anything expecting those to work — the adapters
> do not exist and nothing fabricates them.

Secret fields are write-only. After saving you can only replace a value or
clear **all** credentials of a row («فصل ومسح الأسرار»).

---

## 5. Connection testing and sandbox sends

### Probe test (never sends anything)

```powershell
$session = Invoke-WebRequest -Uri 'http://localhost:8080/staff/login' -SessionVariable web
# ... sign in through the console, then reuse $web (or an API key) ...
$headers = @{ 'X-CSRF-Token' = $session.Cookies.GetCookies('http://localhost:8080')['csrf_token'].Value }
Invoke-RestMethod -Method Post -Uri 'http://localhost:8080/api/staff/admin/integration-connections/test' `
  -WebSession $web -Headers $headers -ContentType 'application/json' `
  -Body (@{ provider = 'smtp'; company_id = $null } | ConvertTo-Json)
```

Response: `{ provider, company_id, connection_status, tested_at, error_code, message }`
— one of the four states, plus a sanitized message. The result is stored on the
row and audited (`integration_connection_test`).

### Sandbox send (explicit consent only)

```powershell
Invoke-RestMethod -Method Post -Uri 'http://localhost:8080/api/staff/admin/integration-connections/sandbox-test' `
  -WebSession $web -Headers $headers -ContentType 'application/json' `
  -Body (@{ provider = 'twilio_sms'; company_id = $null; confirm = $true } | ConvertTo-Json)
```

Rules: `confirm` must be `true` (the UI uses an explicit button per row); the
message goes **only** to `sandbox_recipient` (or the saved `test_recipient`),
never to a customer or arbitrary number; the response shows the masked
recipient (`********6789`) and the API never echoes the raw address. Customer
SMS (`POST /api/integrations/complaints/{id}/sms`) additionally requires
`integrations.send_sms` and the complaint's recorded consent.

---

## 6. OAuth connect (Gmail / Microsoft)

1. Save `client_id` + `client_secret` (and `from_email`) for the provider.
2. Click «ربط حساب Google/Microsoft» (or call the API):

```powershell
$body = @{ provider = 'gmail_oauth'; company_id = $null; redirect_path = '/staff/operations' } | ConvertTo-Json
$result = Invoke-RestMethod -Method Post `
  -Uri 'http://localhost:8080/api/staff/admin/integration-connections/oauth/gmail_oauth/authorize' `
  -WebSession $web -Headers $headers -ContentType 'application/json' -Body $body
Start-Process $result.authorization_url
```

3. The provider redirects to
   `GET /api/staff/admin/integration-connections/oauth/{provider}/callback`.
   The backend validates the HMAC state (actor + provider + company + expiry),
   exchanges the code, stores the **refresh token only** (the access token is
   short-lived and never persisted), and redirects to `/staff/operations` with
   `?integration_oauth=connected` or `?integration_oauth=failed`.

Manual callback testing must never be faked: a tampered/expired/replayed state
is rejected (`integration_oauth_state_invalid`). Rotation of the client secret
invalidates the refresh token silently — re-run the test probe to detect it.

---

## 7. Webhooks (inbound)

| Provider | Route | Verification |
| --- | --- | --- |
| Twilio SMS status | `POST /api/webhooks/twilio_sms/{config_id}` | Twilio SHA-256 HMAC over `url + params`, signed with the row's `auth_token`; replay window 10 minutes. |
| Twilio SMS inbound | same route (`MessageSid` inbound) | Same signature; body text → `extract_ticket_no`, else 422. |
| WhatsApp Cloud | `POST /api/webhooks/whatsapp_cloud` | Subscription GET (`hub.mode/hub.verify_token/hub.challenge`) then `X-Hub-Signature-256` HMAC-SHA256 with `app_secret`. |
| Mail delivery events | `POST /api/webhooks/email_webhook` | Timestamped HMAC with `inbound_email_webhook_secret`. |
| Voice status | `POST /api/webhooks/voice_status` | Timestamped HMAC with `inbound_voice_webhook_secret`. |

The migrations grant `POST` on these routes to the `anonymous` role **only**;
all other integration routes are denied to `anonymous` and granted to
`app_worker`. Worker dispatch also re-checks the signature before processing.

`PUBLIC_APP_URL` must be the HTTPS URL the providers can reach. For local
tunneling:

```powershell
# example: any HTTPS tunnel you trust, then set PUBLIC_APP_URL accordingly
ngrok http 8080
```

---

## 8. Operational notes

- **AI budget:** `daily_request_limit` (default 500) requests/day per company;
  exceeding it returns 429 and the worker defers. All prompts pass through
  `minimize_pii` (Egyptian national IDs, mobiles, e-mails, names).
- **Attachment scanning:** every inbound attachment is enqueued to
  `scan_attachment`. `ClamAVScanner` speaks `INSTREAM` to `clamd`; verdicts
  `clean` / `infected` / `unscanned` (unreachable scanner ⇒ **unscanned**,
  never clean).
- **Voice:** `recording_b64` from provider callbacks is encrypted at rest
  (`call_recordings`), `min_duration_seconds`/`max_duration_seconds` bound the
  call, and the transcript is `transcribe`d via the OpenAI Speech adapter.
  Follow-up calls require `voice_consent = true` on the complaint.
- **Worker failure codes** are enumerated in `docs/RUNBOOK.md`
  (`integration_payload_invalid`, `integration_rate_limited`, …). Payloads
  contain no credentials and no raw national IDs.

## 9. Verifying your change (no make)

```powershell
# backend
python -m pytest backend/tests -m "not integration" --cov=app --cov-fail-under=80
python -m ruff check backend scripts loadtest
python -m ruff format --check
python -m mypy app
python -m app.scripts.lint_migrations

# frontend
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build

# regenerate the API snapshot and TS types after endpoint changes
python scripts/export_openapi.py
npm --prefix frontend run api:types
```

All provider tests use mocked transports (`httpx.MockTransport`,
`aiosmtplib`/IMAP/ClamAV fakes). **Live provider runs are documented as not
performed**: `backend/tests/test_e2e_integrations_live.py` remains skipped
(`-m integration`) and needs real credentials; nothing in this repository was
verified against live Google/Microsoft/Meta/Twilio/OpenAI endpoints.

## 10. Architecture map (for maintainers)

| Concern | File |
| --- | --- |
| Schemas / provider catalog | `backend/app/domain/schemas.py` |
| Adapters + OAuth broker + probes | `backend/app/services/integration_providers.py` |
| Connection lifecycle (save/test/sandbox/OAuth/SMS) | `backend/app/services/integrations.py` |
| Encryption + idempotency | `backend/app/security/integration_crypto.py` |
| ClamAV scanner | `backend/app/services/providers.py` |
| Admin API | `backend/app/api/admin_integrations.py` |
| Webhooks | `backend/app/api/integration_webhooks.py` |
| Worker dispatch | `backend/app/worker.py` |
| Migration | `backend/alembic/sql/028_integration_connections.sql` |
| Console UI | `frontend/src/features/admin/IntegrationsSettings.tsx` |
| Tests | `backend/tests/test_integration_connections.py`, `backend/tests/test_phase4_integrations.py` |
