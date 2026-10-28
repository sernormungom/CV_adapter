#!/usr/bin/env bash
# `make dev` for the dev container. Run inside the `dev` service:
#
#   docker compose -f .devcontainer/compose.yml exec dev scripts/devcontainer-app.sh
#
# then open http://127.0.0.1:8000 (or DEV_APP_PORT) in the Windows browser.
#
# Why not `make dev` itself: its db-up step runs `docker compose up db`, and
# the dev container deliberately has no Docker socket; and it binds uvicorn
# to 127.0.0.1, which inside a container is the container's own loopback,
# unreachable through the published port.
set -euo pipefail

cd "$(dirname "$0")/.."

# The app gets its own database on the same disposable server. Seeding the
# pilot cast into `cvtest` would make the integration gate refuse it — it
# rejects a target whose business tables hold rows, correctly.
APP_DB=cvadapter
ADMIN_URL="postgresql://cvtest:cvtest@localhost:5432/postgres"
export DATABASE_URL="postgresql+psycopg://cvtest:cvtest@localhost:5432/${APP_DB}"

# `db` cannot be a healthy-dependency of `dev` (it joins dev's network
# namespace, so it starts after it); right after `up` it may still be booting.
for _ in $(seq 1 60); do
  pg_isready -q -h localhost -p 5432 -U cvtest && break
  sleep 1
done
pg_isready -h localhost -p 5432 -U cvtest >/dev/null || { echo "Postgres not ready on localhost:5432" >&2; exit 1; }

if ! psql "$ADMIN_URL" -tAc "SELECT 1 FROM pg_database WHERE datname = '${APP_DB}'" | grep -q 1; then
  psql "$ADMIN_URL" -qc "CREATE DATABASE ${APP_DB}"
fi
alembic upgrade head

# --reload sees edits made inside the container. Edits made from Windows to a
# /mnt/c checkout raise no inotify event through the bind mount; restart then.
exec python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
