.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
BOOTSTRAP_PYTHON ?= python3
COMPOSE ?= docker compose
PROD_COMPOSE ?= $(COMPOSE) --env-file .env.production -f docker-compose.prod.yml
IMAGE_OWNER ?= $(shell git config --get remote.origin.url | sed -E 's#.+[:/]([^/]+)/[^/]+(\.git)?$$#\1#' | tr 'A-Z' 'a-z')
IMAGE_TAG ?= $(shell git rev-parse HEAD)

.PHONY: help init install up down logs migrate partitions seed test test-integration lint format ci \
	images prod-config deploy release backup restore-drill loadtest-sessions loadtest-smoke helm-lint \
	rotate-keys lint-migrations
help:
	@echo 'init                Generate ignored .env with unique local credentials'
	@echo 'install             Create .venv; install backend dev and frontend dependencies'
	@echo 'up/down/logs        Manage the development Docker Compose stack'
	@echo 'migrate/partitions  Apply Alembic revisions / pre-create monthly partitions'
	@echo 'seed                Create demo data (set DEMO_STAFF_PASSWORD in .env first)'
	@echo 'test/test-integration/lint/format/ci'
	@echo 'images              Build the deployable api and web images locally'
	@echo 'prod-config         Render docker-compose.prod.yml (needs .env.production)'
	@echo 'deploy              Trigger the CD workflow for production (requires gh auth)'
	@echo 'release             Tag the current commit and let CD publish images + release'
	@echo 'backup              pg_dump (custom format + sha256) into ./backups from the dev stack'
	@echo 'restore-drill       Restore DUMP=... into a scratch DB and verify revision + RLS'
	@echo 'loadtest-sessions   Mint non-production load-test sessions into loadtest/sessions.json'
	@echo 'loadtest-smoke      Run the k6 smoke profile (needs k6; BASE_URL defaults to localhost:8080)'
	@echo 'helm-lint           helm lint + template with default and production values'
	@echo 'rotate-keys         Re-encrypt PII with the current key (see docs/RUNBOOK.md)'
	@echo 'lint-migrations     Static zero-downtime checks for new SQL revisions'
init:
	$(BOOTSTRAP_PYTHON) scripts/bootstrap_env.py
install:
	$(BOOTSTRAP_PYTHON) -m venv .venv
	$(PYTHON) -m pip install -e './backend[dev,seed]'
	cd frontend && npm ci
up:
	$(COMPOSE) up --build -d
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs -f --tail=100
migrate:
	$(COMPOSE) run --rm migrate
partitions:
	$(COMPOSE) run --rm partitions
seed:
	$(COMPOSE) run --rm seed
test:
	cd backend && ../$(PYTHON) -m pytest -m 'not integration' --cov=app --cov-report=term-missing --cov-fail-under=80
test-integration:
	cd backend && ../$(PYTHON) -m pytest -m integration
lint:
	$(PYTHON) -m ruff check backend scripts loadtest
	$(PYTHON) -m ruff format --check backend scripts loadtest
	cd backend && ../$(PYTHON) -m app.scripts.lint_migrations
	cd backend && ../$(PYTHON) -m mypy app
	cd frontend && npm run lint && npm run typecheck
format:
	$(PYTHON) -m ruff check --fix backend scripts loadtest
	$(PYTHON) -m ruff format backend scripts loadtest
ci: lint test
	cd frontend && npm run test && npm run build

images:
	docker build -f infra/Dockerfile.backend -t ghcr.io/$(IMAGE_OWNER)/insurance-platform-api:$(IMAGE_TAG) .
	docker build -f infra/Dockerfile.web -t ghcr.io/$(IMAGE_OWNER)/insurance-platform-web:$(IMAGE_TAG) .
prod-config:
	$(PROD_COMPOSE) config --quiet
deploy:
	gh workflow run cd.yml -f environment=production -f deploy=true
release:
	@test -n "$(VERSION)" || (echo 'Usage: make release VERSION=v1.2.3' && exit 1)
	git tag -a "$(VERSION)" -m "Release $(VERSION)"
	git push origin "$(VERSION)"

backup:
	$(COMPOSE) cp scripts/backup postgres:/tmp/backup
	mkdir -p backups
	$(COMPOSE) exec -T -e BACKUP_DIR=/tmp/backups postgres sh -ec \
		'export PGUSER="$$POSTGRES_USER" PGDATABASE="$$POSTGRES_DB"; sh /tmp/backup/pg_backup.sh' > backups/latest.txt
	$(COMPOSE) cp postgres:/tmp/backups/. backups/
	@cat backups/latest.txt
restore-drill:
	@test -n "$(DUMP)" || (echo 'Usage: make restore-drill DUMP=/tmp/backups/insurance-....dump (path inside the postgres container)' && exit 1)
	$(COMPOSE) cp scripts/backup postgres:/tmp/backup
	$(COMPOSE) exec -T postgres sh -ec 'export PGUSER="$$POSTGRES_USER" PGDATABASE="$$POSTGRES_DB"; sh /tmp/backup/pg_restore_drill.sh "$(DUMP)"'
loadtest-sessions:
	$(COMPOSE) run --rm -T --no-deps -e DATABASE_ADMIN_URL="$$(grep -E '^(POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB)=' .env | sort | awk -F= '{v[$$1]=$$2} END {printf "postgresql+psycopg://%s:%s@postgres:5432/%s", v["POSTGRES_USER"], v["POSTGRES_PASSWORD"], v["POSTGRES_DB"]}')" \
		api python -m app.scripts.loadtest_sessions --customers 200 --staff 20 > loadtest/sessions.json
loadtest-smoke:
	k6 run -e BASE_URL=$${BASE_URL:-http://localhost:8080} -e SESSIONS=$(CURDIR)/loadtest/sessions.json -e PROFILE=$${PROFILE:-smoke} loadtest/k6/platform.js
helm-lint:
	helm lint --strict deploy/helm/insurance-platform
	helm lint --strict deploy/helm/insurance-platform -f deploy/helm/insurance-platform/values-production.yaml
	helm template ip deploy/helm/insurance-platform -f deploy/helm/insurance-platform/values-production.yaml > /dev/null
rotate-keys:
	$(COMPOSE) run --rm --no-deps -e DATABASE_ADMIN_URL="$$DATABASE_ADMIN_URL" api python -m app.scripts.rotate_keys
lint-migrations:
	cd backend && ../$(PYTHON) -m app.scripts.lint_migrations
