.PHONY: help dev test test-fast test-static test-integration test-complete bootstrap db-up db-down db-logs migrate migrate-check seed-pilot create-admin reset-password docker-build docker-up docker-down docker-restart docker-logs docker-ps docker-shell docker-health docker-config

help:
	@echo "CV Adapter commands"
	@echo ""
	@echo "Local development (run inside WSL, with the WSL venv active):"
	@echo "  make dev             Start Postgres, apply migrations, then run the app (uvicorn reload)"
	@echo "  make test            Run the complete test gate (pip install -r requirements-dev.txt first)"
	@echo "  make bootstrap       Start Postgres + apply migrations (no app)"
	@echo "  make seed-pilot      Seed the three fabricated V2 Pilot accounts (PILOT_SEED_PASSWORD=...)"
	@echo "  make reset-password  Reset any user's password (IDENTIFIER=email-or-username PASSWORD=...)"
	@echo ""
	@echo "Test gates (see docs/testing.md):"
	@echo "  make test-fast       No external service. The loop you run while editing."
	@echo "  make test-static     Static-client (HTML/JS/CSS) contract tests only"
	@echo "  make test-integration Postgres 16 semantics (starts a disposable container)"
	@echo "  make test-complete   Everything — the release gate"
	@echo ""
	@echo "Database:"
	@echo "  make db-up           Start Postgres (detached, wait for healthy)"
	@echo "  make db-down         Stop Postgres (data volume preserved)"
	@echo "  make db-logs         Follow Postgres logs"
	@echo "  make migrate         Apply database migrations (alembic upgrade head)"
	@echo "  make migrate-check   Fail if the models have drifted from the migrations"
	@echo ""
	@echo "Docker (full container stack):"
	@echo "  make docker-build    Build the Docker image"
	@echo "  make docker-up       Build and start the app container"
	@echo "  make docker-down     Stop and remove the app container"
	@echo "  make docker-restart  Restart the app container"
	@echo "  make docker-logs     Follow app container logs"
	@echo "  make docker-ps       Show Compose service status"
	@echo "  make docker-shell    Open a shell inside the app container"
	@echo "  make docker-health   Call the app health endpoint from inside the container"
	@echo "  make docker-config   Show Compose service names without expanding secrets"

# ── Local development ───────────────────────────────────────────────────────
# The app now depends on Postgres. `make dev` brings the DB up and applies
# migrations before launching uvicorn, so a single command gives a working app.
# `migrate` is a fast no-op when the schema is already at head.

dev: bootstrap
	python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# ── Test gates ──────────────────────────────────────────────────────────────
# Four markers, defined in pytest.ini and applied automatically by the
# repository-root conftest.py. Every gate redirects the data roots into a
# throwaway directory, so no test can reach ./data. See docs/testing.md.
#
# `fast` and `static` need no service (in-memory SQLite). `integration` needs a
# disposable Postgres 16 and starts one itself.

test: test-complete

test-fast:
	python -m pytest -m fast -q

test-static:
	python -m pytest -m static -q

test-integration:
	python -m pytest -m integration -q

test-complete:
	python -m pytest -m complete -q

bootstrap: db-up migrate

db-up:
	docker compose up -d --wait db

db-down:
	docker compose stop db

db-logs:
	docker compose logs -f db

migrate:
	alembic upgrade head

# Model/schema drift check (V2 M1). `alembic check` compares Base.metadata
# against the migrated database and exits non-zero when a model change has no
# migration. Requires the DB to be at head first, so it runs after `migrate`.
migrate-check: migrate
	alembic check

# Explicitly seed the approved fabricated V2 Pilot cast after migrations.
#
# The password is read from the *ambient environment* only — never as a make
# variable — so it never appears in an expanded recipe, `make -n` output, a CI
# log, or the argv of any process. Export it without echoing it first:
#
#   read -rs PILOT_SEED_PASSWORD && export PILOT_SEED_PASSWORD
#   make seed-pilot
#
# Every recipe line here is `@`-prefixed for the same reason: an un-prefixed
# line is echoed by make with its variables already expanded.
seed-pilot:
	@test -n "$$PILOT_SEED_PASSWORD" || { echo "PILOT_SEED_PASSWORD must be exported in the environment (see the Makefile comment; do not pass it as a make variable)"; exit 1; }
	@python -m scripts.seed_pilot

# One-time: create an admin account. Requires an active Site to already exist,
# so run `seed-pilot` (or otherwise provision the Site) first.
#   make create-admin EMAIL=you@example.com PASSWORD="<min-12, upper+number+symbol>"
# Recipe lines are `@`-prefixed so the password is not echoed. It is still
# visible in this process's argv and in shell history — prefer `seed-pilot`.
create-admin:
	@test -n "$(EMAIL)" || { echo "EMAIL is required: make create-admin EMAIL=you@example.com PASSWORD=..."; exit 1; }
	@test -n "$(PASSWORD)" || { echo "PASSWORD is required: make create-admin EMAIL=you@example.com PASSWORD=..."; exit 1; }
	@python -m scripts.create_admin --email "$(EMAIL)" --password "$(PASSWORD)"

# Reset a forgotten password for any user (admin recovery; no self-service
# flow). Also revokes that account's live sessions and clears a login lockout.
#   make reset-password IDENTIFIER=ta@example.com PASSWORD="<min-12, upper+number+symbol>"
# `@`-prefixed so the password is not echoed; see the create-admin caveat.
reset-password:
	@test -n "$(IDENTIFIER)" || { echo "IDENTIFIER is required: make reset-password IDENTIFIER=email-or-username PASSWORD=..."; exit 1; }
	@test -n "$(PASSWORD)" || { echo "PASSWORD is required: make reset-password IDENTIFIER=email-or-username PASSWORD=..."; exit 1; }
	@python -m scripts.reset_password --identifier "$(IDENTIFIER)" --password "$(PASSWORD)"

# ── Docker (full container stack) ────────────────────────────────────────────
docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-restart:
	docker compose restart app

docker-logs:
	docker compose logs -f app

docker-ps:
	docker compose ps

docker-shell:
	docker compose exec app sh

docker-health:
	docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read().decode())"

docker-config:
	docker compose config --services
