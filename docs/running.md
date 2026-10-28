# Running the app locally

The app depends on Postgres for auth and storage. Run the local stack inside
**WSL (Ubuntu)** — Docker/Postgres, alembic, and uvicorn all run there.
Starting uvicorn without Postgres up makes sign-in hang (the login request
opens a DB connection that never completes).

```bash
source .venv/bin/activate          # WSL venv (OS-specific — create it inside WSL)
make dev                           # starts Postgres + applies migrations + runs uvicorn
```

`make dev` is the only command needed day to day. Health check:
<http://127.0.0.1:8000/health> → `{"status":"ok"}`.

## Dev container

`.devcontainer/` defines the **workshop** image. A maintainer in VS Code, any
coding agent they run, and a release check can all use the same environment. It holds Python 3.12 and the dev requirements, Node 24 (so the
`tests/client/*.js` behaviour probes run rather than skip), and a Playwright
Chromium with its system libraries. It also has `psql`/`pg_isready`, and pinned
`claude` and `codex` CLIs. The repository-root `Dockerfile` is still the
shipped **production** artefact, with the app only and no test tooling. Do not
add test tooling to it.

The stack is the dev container plus a disposable Postgres 16. Postgres shares
the dev container's network namespace, so it is on the container's
`localhost:5432`, which is the only kind of host the integration gate's
allowlist accepts. Its data lives in memory, so every `up` starts empty. The
checkout is bind-mounted at `/workspace`. The stack gets no Docker socket, no
home directory and no `~/.ssh`. Run these commands from WSL, in the checkout:

```bash
export DEV_UID=$(id -u) DEV_GID=$(id -g)      # files written in the container stay yours
docker compose -f .devcontainer/compose.yml up -d --build
docker compose -f .devcontainer/compose.yml exec dev make test-complete   # or any gate
docker compose -f .devcontainer/compose.yml exec dev scripts/devcontainer-smoke.sh
docker compose -f .devcontainer/compose.yml down -v
```

In the container, `python`, `pytest` and `alembic` are the image's own
interpreter, so there is no venv to activate. `CV_ADAPTER_REQUIRE_INTEGRATION=1`
is set, so a missing database fails the run instead of skipping.
`scripts/devcontainer-smoke.sh` proves the whole environment and treats
**any** skipped test as a failure.

- **VS Code:** open the checkout from a WSL window (`code .` in WSL), then run
  *Dev Containers: Reopen in Container*. You can also set
  `"dev.containers.executeInWSL": true`. Either way the extension drives WSL's
  native Docker, and Docker Desktop is not needed.
- **The app:** `make dev` cannot run in the container. It starts Postgres through
  a Docker socket the container does not have, and it binds a loopback that
  the published port cannot reach. Run
  `docker compose -f .devcontainer/compose.yml exec dev scripts/devcontainer-app.sh`
  instead, then open <http://127.0.0.1:8000>. The app gets its own `cvadapter`
  database, so seeding it never touches `cvtest`. Pass a real key for live LLM
  calls only to that process:
  `... exec -e ANTHROPIC_API_KEY dev scripts/devcontainer-app.sh`.
- **Agent CLIs:** credentials come from the environment of the shell that runs
  `docker compose up`. They are never stored in the image or a volume:
  - Claude Code: run `claude setup-token` once on the host, then
    `export CLAUDE_CODE_OAUTH_TOKEN=...`.
  - Codex: `export CODEX_API_KEY=...` (or `OPENAI_API_KEY`).

  A variable set at `up` stays in the container's config (`docker inspect`)
  until `down`. To keep it out of the config, pass it per command:
  `docker compose ... exec -e CLAUDE_CODE_OAUTH_TOKEN dev claude`.

  `ANTHROPIC_API_KEY` is deliberately **not** passed through, because the test
  suite reads that name too. Inside the container, run `claude` or `codex` with
  their own sandboxes relaxed. The container is the sandbox:
  `claude --dangerously-skip-permissions` works because the user is not root,
  and `codex --sandbox danger-full-access`.
- **One stack per checkout:** to run a second stack beside this one, use
  `-p <name>` and `DEV_APP_PORT=<port>`. Give each agent run its own clone. A
  `git worktree` checkout's `.git` file points at a host path that is not
  mounted, so `git` does not work inside the container for a worktree.

Known limits:

- Through the `/mnt/c` mount, uvicorn `--reload` sees no edits made from
  Windows.
- The checkout's own `.env`, if it has one, is visible inside the container.
- CI's `docker` gate (build the production image and smoke `/health`) needs
  a Docker socket, so it does not run here.
- A checkout on the WSL-native filesystem is faster: `test-complete` took
  158s there against 204s on `/mnt/c`. Git and `--reload` also work there.
  See `docs/testing.md` for the timings.
- The image is about 4.6 GB on disk. A cold build takes about 6 minutes, and
  a cached rebuild only reruns the layers after a changed requirements file.

## First-time setup

On a fresh database, seed the pilot cast once (see `ADMIN.md` → First-time
setup). Migrations create schema only — they never create accounts:

```bash
make seed-pilot PILOT_SEED_PASSWORD="<min-15-chars>"   # patrik.artman, norberto.munoz, mo.chen
```

`make create-admin EMAIL=... PASSWORD=...` still exists for a one-off admin
outside the pilot cast. The seed is idempotent: a rerun validates the
three-account role/relationship shape and refuses to mix with non-pilot users
(`docs/architecture/areas/authz.md` G7).

Useful sub-targets: `make bootstrap` (DB + migrations, no app), `make migrate`,
`make migrate-check` (**`alembic check` — fails on model/migration drift**),
`make db-up` / `make db-down`. Postgres data persists in the `pgdata` Docker
volume across restarts.

## Migrations

The schema is the baseline (`alembic/versions/0001_v2_pilot_baseline.py`) plus
ordinary forward revisions (`0003_…` to `0005_…` so far). The cloud database
has been deployed, so **add a new revision; do not edit an applied one**. Run
`make migrate-check` after every model change. The data is disposable, so a
destructive revision is acceptable, but it still runs through the migrate task
before the service rolls (`DEPLOY.md`).

### The drift report that is not a bug

**Editing a revision that a database has already applied does not update that
database, and `make migrate-check` reports this as drift.** An existing database is already
stamped `0001_v2_pilot_baseline`, so `alembic upgrade head` is a no-op and the
columns you just added to the baseline are never created; `alembic check` then
compares the models against that stale schema and fails with
`New upgrade operations detected: [('add_column', ...)]`.

**This names your new columns, so it reads exactly like a missing migration —
but the repository is fine and a fresh database passes.** Confirm which one you
have *before* changing any code. Build a throwaway database and run the
baseline against it:

```bash
docker compose exec -T db psql -U cvadapter -d postgres -c 'CREATE DATABASE drift_check;'
DATABASE_URL=…/drift_check alembic upgrade head && DATABASE_URL=…/drift_check alembic check
```

If that passes, the baseline and the models agree and only your local database
is behind. Bring it into line, either by recreating it —

```bash
make db-down && docker volume rm cv_adapter_pgdata && make bootstrap   # then re-seed
```

— the pilot's data is disposable — or, to keep the seeded cast and demo data,
by applying the same additive DDL the baseline would have created. Either way
re-run `make migrate-check`; that is the check that the two now agree.

This is a consequence of editing an applied revision in your local
environment, **not** a missing migration. Prefer a new revision next time.
