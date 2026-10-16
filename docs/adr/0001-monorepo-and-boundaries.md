# ADR 0001: Modular monorepo, stateless API and asynchronous jobs

**Status:** accepted (Phase 1)

**Decision.** Keep React/Vite/TypeScript in `frontend`, FastAPI/SQLAlchemy 2 in `backend`, runtime wiring in `infra`, and contracts/ADRs in `docs`. Only `/api` crosses the browser boundary. Nginx proxies to the API and Vite; Redis/ARQ handles deferred SLA/notifications/integrations. Local Compose has isolated Postgres, PgBouncer, Redis, migration and maintenance containers; revision 24 removed the SeaweedFS S3 gateway because private binaries are encrypted rows in PostgreSQL. Build API/worker images once and scale replicas independently later.

**Consequences.** Phase 1's UI is a shell and the business OpenAPI file is a draft; no mock ticket routes are presented as working. Production needs TLS, secrets management, an encryption-key KMS/escrow procedure, observability and a Kubernetes deployment. Docker Compose is development-only and is not evidence of million-user capacity.
