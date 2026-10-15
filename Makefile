.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
BOOTSTRAP_PYTHON ?= python3
COMPOSE ?= docker compose
PROD_COMPOSE ?= $(COMPOSE) --env-file .env.production -f docker-compose.prod.yml
IMAGE_OWNER ?= $(shell git config --get remote.origin.url | sed -E 's#.+[:/]([^/]+)/[^/]+(\.git)?$$#\1#' | tr 'A-Z' 'a-z')
IMAGE_TAG ?= $(shell git rev-parse HEAD)

.PHONY: help init install up down logs migrate partitions seed test test-integration lint format ci \
	images prod-config deploy release
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
	$(PYTHON) -m ruff check backend scripts
	$(PYTHON) -m ruff format --check backend scripts
	cd backend && ../$(PYTHON) -m mypy app
	cd frontend && npm run lint && npm run typecheck
format:
	$(PYTHON) -m ruff check --fix backend scripts
	$(PYTHON) -m ruff format backend scripts
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
