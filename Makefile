.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
BOOTSTRAP_PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: help init install up down logs migrate partitions seed test test-integration lint format ci
help:
	@echo 'init                Generate ignored .env with unique local credentials'
	@echo 'install             Create .venv; install backend dev and frontend dependencies'
	@echo 'up/down/logs        Manage the development Docker Compose stack'
	@echo 'migrate/partitions  Apply Alembic revisions / pre-create monthly partitions'
	@echo 'seed                Create demo data (set DEMO_STAFF_PASSWORD in .env first)'
	@echo 'test/test-integration/lint/format/ci'
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
