# CV Adapter

A web app for importing consultant CVs, scoring job opportunities per
consultant, and generating position-tailored CVs with the Claude API. It is
a cloud-deployed pilot running on fabricated data. Start with
[docs/product.md](docs/product.md).

## Quick launch

Everything Python runs inside WSL (Ubuntu); the Windows virtualenv is not
supported. See [docs/running.md](docs/running.md).

```bash
source .venv/bin/activate
make dev          # Postgres + migrations + uvicorn
```

Then open http://127.0.0.1:8000/ — health check at http://127.0.0.1:8000/health.

## Pilot bootstrap

Migrations never seed accounts. After applying migrations, explicitly seed the fabricated pilot cast
(`GBG`; `patrik.artman`, `norberto.munoz`, and `mo.chen`) with a temporary
password held only in your environment:

```powershell
$env:PILOT_SEED_PASSWORD = "a-private-temporary-password"
wsl make seed-pilot
```

The seed refuses to mix with other accounts and is a validation no-op only when
the exact approved role and TA↔Consultant shape already exists. It never logs or
stores the raw password. Authentication is enforced by default; disabling it is
accepted only for an explicitly marked development/test process and is rejected
at production startup.

## Docker launch

Docker runs the app in a container: a repeatable Linux runtime with the Python
dependencies installed inside it. The project data still lives on your machine
because `docker-compose.yml` mounts `./data` into the container as `/app/data`.

From PowerShell, when using Docker inside Ubuntu WSL:

```powershell
wsl docker compose up -d --build
```

Then open http://127.0.0.1:8000/ — health check at http://127.0.0.1:8000/health.

Useful commands:

```powershell
wsl docker compose ps
wsl docker compose logs -f app
wsl docker compose down
```

If you have `make` available inside Ubuntu WSL, the same workflow from
PowerShell is:

```powershell
wsl make docker-up
wsl make docker-logs
wsl make docker-down
```

From an Ubuntu WSL terminal opened in this repo, use the same targets without
the `wsl` prefix:

```bash
make docker-up
make docker-logs
make docker-down
```

## Documentation

- [docs/product.md](docs/product.md) — handover: posture, deliberate limitations, prerequisites before real data, open items
- [docs/architecture/areas/README.md](docs/architecture/areas/README.md) — subsystem contracts and guards, with a symptom index
- [docs/running.md](docs/running.md) — local stack, dev container, seeding, migrations
- [docs/testing.md](docs/testing.md) — the four development and release gates
- [DEPLOY.md](DEPLOY.md) — deploying to AWS
- [docs/legal/](docs/legal/) — privacy, DPIA, AI Act and retention records
- [GUIDE.md](GUIDE.md) — for Consultants and Talent Advisors
- [ADMIN.md](ADMIN.md) — for system administrators
- [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) — guidance for AI-assisted work

## Continuous integration

`.github/workflows/ci.yml` holds the gates:

| Gate | What it runs |
| --- | --- |
| `fast` | dependency resolution, `pip check`, and the fast test gate |
| `static` | the static-client (HTML/JS/CSS) contract tests |
| `integration` | `alembic upgrade head`, `alembic check`, and the Postgres 16 integration tests |
| `docker` | builds the image, starts it with a dummy Anthropic key, checks `/health` |

**How it is triggered.** `ci.yml` runs directly on pull requests (and on manual
dispatch). It does **not** run directly on pushes to `main`; a deployable push
reaches the same gates through `.github/workflows/build-push.yml`, which calls
`ci.yml` as a reusable workflow and declares `needs: gates` on the result. So a
red gate means no image is built or pushed to ECR, and every commit is gated
exactly once rather than twice.

Neither workflow deploys the app — see [DEPLOY.md](DEPLOY.md).

Local equivalents and the marker/fixture rules are in
[docs/testing.md](docs/testing.md).
