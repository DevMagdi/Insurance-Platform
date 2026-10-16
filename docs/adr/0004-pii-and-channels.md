# ADR 0004: PII, attachments and provider integrations

**Status:** accepted (Phase 1 data contract; auth/upload/call services are next-phase work)

**Decision.** Validate the 14-digit Egyptian national ID (ASCII digits, century, date, governorate) at application entry; that is not authoritative identity verification. Encrypt with AES-256-GCM and random 96-bit nonces, binding the `customers:national-id:v1` context as AAD. Use an **independent** HMAC-SHA-256 key as a blind index; never use unsalted SHA-256, plaintext logs, FTS, or response bodies. Runtime SELECT excludes encrypted bytes and the blind index; `read_national_id_ciphertext` is gated by `complaints.view_full_national_id` plus customer scope, and the server must audit each reveal before decrypting. Customer enrollment/lookup will need a purpose-bound, rate-limited, audited DB routine; the Phase 1 runtime role has no blanket INSERT/SELECT on the blind index. Secrets and key rotation belong in a KMS/vault in production, not `.env`.

Attachments and recordings use private storage, checksums, malware scan states and short-lived one-use URLs. Only clean public attachments may be seen by customers, never internal notes. Preserve consent timestamp for AI voice calls; encrypt transcripts and AI-generated insights, show cross-complaint duplicate candidates only when both complaints remain in scope, and require lawful retention/deletion workflows under Egyptian PDPL 151/2020. Provider credentials are encrypted or referenced by a vault; webhooks need signature/replay checks and outbound URL allowlists to prevent SSRF. Rate-limit OTP, submissions, uploads, exports and authentication; these services are *not* yet implemented.

**Superseded in part (revision `20261016_24`).** The private store is no longer an
S3-compatible service: attachment bytes, generated export artifacts and call
recordings are encrypted (AES-256-GCM, versioned `APP_ENCRYPTION_KEY` keyring,
row- and purpose-bound AAD) and written to columns on their owning PostgreSQL
rows. Uploads keep the plan → one-use `PUT` token flow described above, and
downloads keep short-lived single-use tokens, but no bucket, endpoint or
storage credential exists. The scan gate, checksum, scope and audit decisions in
this ADR are unchanged.
