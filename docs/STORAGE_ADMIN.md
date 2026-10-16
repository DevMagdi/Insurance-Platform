# First-party storage inventory

The staff application includes a native storage browser at **`/staff/storage`**.
It is part of the Insurance Platform React app and its FastAPI API; it does not
embed, proxy, or depend on a third-party administration UI, and it does not talk
to an object store — because there is none. Private binaries (complaint
attachments, generated export artifacts and call recordings) are stored
**encrypted inside PostgreSQL**, so the inventory reads the platform's own rows
with the same database connection that already enforces RLS and scope.

## Access and setup

- The signed-in staff member needs the explicit **`storage.manage`** permission
  and a global administration scope: all companies, all insurance types and
  ownership `all`. The API checks both on every request; hiding the menu alone
  is not considered a security boundary.
- Migration `20261016_23` registers the permission and grants it to the seeded
  administrator system role on existing installations. Migration `20261016_24`
  moves the described bytes into `attachments.content`, `export_jobs.content`
  and `call_logs.recording_content` (AES-256-GCM under `APP_ENCRYPTION_KEY`).
- No configuration is required: there is no bucket, endpoint, access key or
  volume to set. Run `make up` (PostgreSQL, Redis and Mailpit only), open the
  application normally, then visit `http://localhost:8080/staff/storage`.

## Inventory API

`GET /api/staff/storage?prefix=&limit=100&continuation_token=...` is a bounded,
permission-protected page request. `limit` is capped at 200. The response
describes the platform's own storage:

| Field | Meaning |
| --- | --- |
| `backend` / `store` | `postgresql` / `insurance-platform` — the single store behind the console |
| `status` | `online` when the metadata query succeeded (never a bucket probe) |
| `prefix` | Logical folder: `attachments/`, `exports/`, `recordings/` or empty for the root |
| `folders` | The logical folders, returned only at the root |
| `objects[]` | `key` (`attachments/<uuid>`), `kind`, `size_bytes` (ciphertext length on the row), `last_modified`, `checksum_sha256` and row `details` (filename, scan status, visibility, ticket number, export format, call direction, …) |
| `totals` | Row/byte totals per kind plus `pending_scans` and `attachment_metadata_without_content` |
| `is_truncated` / `next_page_token` | Keyset pagination; the token is opaque, kind-bound and rejected when replayed against another folder |

The browser can navigate folders, search loaded results and load the next page.
Search is intentionally local to the currently loaded page; it does not trigger
an unbounded table scan. Cursors are opaque (`base64url("kind:uuid")`) and a
malformed or mismatched token returns `422` instead of guessing.

Each successful inventory request writes a `storage_inventory_viewed` audit
event with the store, requested prefix, requested limit and result count, but
never storage credentials or file contents. Responses use `Cache-Control:
no-store` and `X-Content-Type-Options: nosniff`.

## Deliberate safety limits

This is an operational **read-only inventory**, not an alternate file-sharing
or data-deletion path. Insurance attachments may contain sensitive data and are
linked to complaint records, scan verdicts, export manifests, or encrypted
recordings. The storage browser therefore never returns file bodies or
ciphertext, never mutates rows, and never bypasses scan/scope checks. It calls
bounded `SECURITY DEFINER` metadata functions (`app_security.storage_inventory`,
`app_security.storage_totals`) that are granted to `app_runtime` but expose no
`content` column.

- Clean attachment downloads continue through the existing scoped and audited
  complaint/attachment APIs, which decrypt a single authorized row.
- Pending or rejected attachments remain unavailable until the scan workflow
  authorizes them.
- Export files and call recordings continue through their own owner/scope,
  expiry, decryption and audit checks; the `export_jobs.content` column has no
  `SELECT` grant for the runtime role at all.
- A row whose `content` is `NULL` (created before revision 24, when the retired
  object store held the bytes) is reported in `totals` as
  `attachment_metadata_without_content` and is never served from a stale copy.

No extra UI package or storage-console product is added. The console reuses the
existing staff shell, permission system, database connection and design system,
and has backend unit/API tests plus frontend component and route tests. The
generated OpenAPI contract is checked by CI together with the frontend build and
the PostgreSQL migration/RLS suite.
