# CV Adapter

Local tool for importing consultant CVs, scoring job opportunities, and generating position-tailored CVs using Anthropic.

## Quick launch

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/ — health check at http://127.0.0.1:8000/health.

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

- [GUIDE.md](GUIDE.md) — for Consultants and Talent Advisors
- [ADMIN.md](ADMIN.md) — for system administrators
- [docs/deployment/phase-2-cloud-readiness.md](docs/deployment/phase-2-cloud-readiness.md) — Docker phase 2 and cloud readiness plan
- [CLAUDE.md](CLAUDE.md) — for Claude Code and AI assistants working on this codebase

## Continuous integration

GitHub Actions runs `.github/workflows/docker.yml` on pushes to `main` and pull
requests. The workflow builds the Docker image, starts the app with a dummy
Anthropic key, checks `/health`, and then stops the container. It is a smoke
test only; it does not deploy the app.
