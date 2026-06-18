# Phase 2 Cloud Readiness Plan

This plan starts from the current Docker baseline:

- one FastAPI app container
- local file-backed data in `data/`
- Docker Compose for local and single-host operation
- GitHub Actions smoke test for Docker build and startup

The goal of phase 2 is not to make the system "cloud native" immediately. The
goal is to make each next operational step understandable, reversible, and tied
to a real need.

## Current State

The application is a modular monolith. The CV pipeline, opportunity pipeline,
dashboard, source collector, matcher, and learning module are separate code
areas, but they run inside one FastAPI app and share the same data folders.

This is the right first deployment shape because:

- one container is easier to understand and operate
- the modules share data, so splitting containers would not yet split ownership
- the pilot workload is small
- Docker Compose already gives a repeatable runtime

## Phase 2A: Local Docker Hardening

Do this before choosing a cloud provider.

1. Add a scripted backup command for `data/`.
2. Add a restore drill using a copied `data/` folder.
3. Add a non-secret `.env.example` check to CI.
4. Decide whether Playwright collection should run inside Docker or stay manual.
5. Add a production-style CORS setting before LAN or cloud exposure.
6. Decide how users will identify themselves at launch.

Success looks like:

- the app can be rebuilt without losing data
- the operator can stop, start, inspect logs, and recover from a bad deploy
- the app can run for a full workday from Docker without manual fixes

## Phase 2B: Optional Service Split

Only split services when there is a real runtime boundary.

Good candidates later:

| Service | Split when |
|---|---|
| `app` | Already exists as the FastAPI web service |
| `collector` | Browser collection needs its own schedule, logs, or dependencies |
| `worker` | CV generation becomes too slow or unreliable in request/response flow |
| `postgres` | File-backed JSON/YAML becomes hard to query, migrate, or back up |

Avoid splitting now just because the code has multiple modules. Microservices
help when services have independent runtime needs, data ownership, release
cycles, or scaling pressure. This project does not have those pressures yet.

## Phase 2C: Cloud Readiness

Before moving to a cloud host, answer these questions:

1. Who needs access outside the office?
2. What data must be protected under GDPR?
3. Where will secrets live?
4. Where will persistent data live?
5. How will backups be created and restored?
6. Who is responsible for operating the service?
7. What is the acceptable downtime during the pilot?

Minimum cloud-ready requirements:

- Docker image builds in CI
- app starts without `--reload`
- secrets are not committed
- persistent data is outside the container
- health endpoint is monitored
- logs are accessible
- backup and restore process is documented
- CORS is restricted for the deployed URL

## Deployment Options

### Option A: Office Host With Docker Compose

Best first production-like deployment for the current ADR.

Use when:

- users are on the same trusted LAN
- remote access is not required
- the operator can manage the host

Shape:

- one host machine
- `docker compose up -d --build`
- local `data/` folder
- LAN URL such as `http://host-ip:8000/`

### Option B: Small EU VPS With Docker Compose

Good next step when remote access matters but the system is still simple.

Use when:

- users need access away from the office
- you want an always-on host
- you are comfortable managing Linux updates, firewall, backups, and Docker

Shape:

- one EU-region VM
- Docker Compose
- HTTPS reverse proxy
- encrypted backups
- restricted firewall

### Option C: Managed App Platform

Good when operating servers becomes a distraction.

Use when:

- you want easier deploys
- the budget can support managed hosting
- file-backed data has been moved to a managed database or durable volume

Shape:

- managed app service
- managed Postgres or durable storage
- platform-managed logs and deploys
- provider secrets manager

## Recommended Next Decisions

Do these in order:

1. Keep the current Docker Compose app as the baseline.
2. Add backup and restore commands for `data/`.
3. Decide whether browser collection should be containerized.
4. Restrict CORS before any LAN or cloud exposure.
5. Add simple authentication when users need remote access.
6. Move to Postgres only when file-backed storage becomes painful.
7. Choose a cloud provider only after backup, secrets, and access requirements are clear.

## Not Yet

These are intentionally deferred:

- Kubernetes
- Docker Swarm
- multi-tenant architecture
- separate microservices for every module
- managed Postgres before the data model is ready
- public internet exposure without auth

Deferring these is not avoiding engineering. It keeps the system teachable and
operable while the product is still finding its real usage pattern.
