# ADR 0005: Transaction pooling and per-request RLS context

**Status:** accepted (Phase 1)

**Decision.** Send runtime connections through PgBouncer `pool_mode=transaction`. SQLAlchemy asyncpg uses `NullPool`, no per-connection prepared-statement cache, random per-statement names to avoid name collisions, and PgBouncer supports up to 100 protocol-level prepared statements. `scoped_transaction()` installs `set_config(..., true)` for **both** `app.user_id` and `app.scope` on the same transaction/connection before queries, then commits/rolls back. No session-scoped `SET`, temp tables, cursors surviving a commit, advisory locks spanning transactions, or server-side session affinity.

**Verification.** CI runs RLS integration tests both against PostgreSQL directly and through port 6432. Reuse the pool across a scoped staff query, a different staff query and a query without context: the last must see zero complaint rows and no residual ID. Production proxies/replicas must enforce the same runtime role, role grants and SET LOCAL discipline. Direct migrations bypass PgBouncer and use a distinct admin credential held only by tooling.
