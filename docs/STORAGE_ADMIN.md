# First-party storage inventory

The staff application includes a native storage browser at **`/staff/storage`**.
It is part of the Insurance Platform React app and its FastAPI API; it does not
embed, proxy, or depend on MinIO Console or another third-party administration
UI. It uses the platform's existing S3-compatible client, so it works with the
configured private bucket whether the endpoint is SeaweedFS, Silo, or an
S3-compatible provider.

## Access and setup

- The signed-in staff member needs the explicit **`storage.manage`** permission
  and a global administration scope: all companies, all insurance types and
  ownership `all`. The API checks both on every request; hiding the menu alone
  is not considered a security boundary.
- Migration `20261016_23` registers the permission and grants it to the seeded
  administrator system role on existing installations. New demo installs also
  seed it on that role. Custom roles can be given the permission by a global
  role administrator.
- The API reads only the configured `S3_BUCKET` and calls the S3 ListObjectsV2
  interface with a folder delimiter. The storage endpoint and credentials stay
  server-side. The browser communicates only with same-origin `/api` routes.
- In Compose, set `S3_ENDPOINT_URL` to the S3 API address reachable by the API
  container (for example `http://minio:9000`). Open the application normally,
  then visit `http://localhost:8080/staff/storage`. No separate console port is
  needed; do not publish port `9001` for this feature.

## Inventory API

`GET /api/staff/storage?prefix=&limit=100&continuation_token=...` is a bounded,
permission-protected page request. `limit` is capped at 500. The response
contains the configured bucket name, provider kind, folder prefixes, object
keys, byte sizes, last-modified timestamps, ETags and an opaque continuation
token. The browser can navigate folders, search loaded results and load the
next page. Search is intentionally local to the currently loaded page; it does
not trigger an unbounded full-bucket scan.

Each successful inventory request writes a `storage_inventory_viewed` audit
event with the bucket, requested prefix and result counts, but not the storage
credentials or object contents. Responses use `Cache-Control: no-store`.

## Deliberate safety limits

This is an operational **read-only inventory**, not an alternate file-sharing
or data-deletion path. Insurance attachments may contain sensitive data and
are linked to complaint records, scan verdicts, export manifests, or encrypted
recordings. The storage browser therefore does not return object bodies, create
or delete bucket data, change access policies, or bypass scan/scope checks.

- Clean attachment downloads continue through the existing scoped and audited
  complaint/attachment APIs.
- Pending or rejected attachments remain unavailable until the scan workflow
  authorizes them.
- Export files and call recordings continue through their own owner/scope,
  expiry, decryption and audit checks.

No extra UI package or storage-console product is added. The console reuses the
existing staff shell, permission system, S3 SDK and design system, and has
backend unit/API tests plus frontend component and route tests. The generated
OpenAPI contract is checked by CI together with the frontend build and the
PostgreSQL migration/RLS suite.
