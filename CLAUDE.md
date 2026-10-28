@AGENTS.md

# CV Adapter — Context for Claude Code

This file is a **router**, not the encyclopedia. It tells you what to load and
what to know before you work on any part of this codebase. Current subsystem
behavior lives in `docs/architecture/areas/`, and each entry below says which
contract to read. The product's posture, limitations and open items are in
`docs/product.md`.

---

## Architecture in two sentences

Two independent pipelines share one relational store (Postgres) plus a
per-consultant profile volume. The **CV Prep Pipeline** imports consultant CVs,
generates Application-tailored CVs, and publishes read-only share links. The
**Opportunity Matching Pipeline** records Jobs, scores them per Consultant, and
tracks Apply/Reject decisions. The only shared artefact is the
`(consultant, job)` pair (ADR-001, invariant 1).

The cloud backend (`backend/`) owns everything: Jobs, matching, CV import,
generation and publishing, and auth, on Postgres and the Claude API. There is no
local half, no browser scraping, no local triage and no scheduler. Jobs are
created by hand in the cloud UI (`+ Job`). `shared/` holds three stdlib-only
primitives and nothing else.

---

## Environment

### Docker / WSL on this machine
- Docker runs as native Docker Engine inside WSL. NEVER launch Docker Desktop, never delete `/var/run/docker.sock`, and never kill `dockerd`.
- The known fix for "daemon unavailable" or a stale socket: the user runs `wsl --shutdown` from Windows, then reopens WSL. Suggest this first.
- Claude cannot run `sudo` interactively (it hangs). Hand `sudo` commands to the user instead of running them.
- Commands are sent through PowerShell. Wrap WSL commands in single quotes, e.g. `wsl -e bash -c 'echo $DOCKER_HOST'`, so PowerShell does not interpolate `$?` and `$VAR`.

---

## What to ignore

`TemporaryInspiration/` is an archive of prior MVPs and prototyping work. Do not load or reference it unless the user explicitly asks for historical context. It does not reflect the current architecture.

---

## Start here

For a bug report or a code change alike, **start at
`docs/architecture/areas/README.md`**. The symptom index and the
directory-to-contract map both live there.

---

## File map — load these files for each feature area

Each area's `docs/architecture/areas/*.md` holds the full Contract and Guards.
This table is a compass, not a summary.

| Area | One line | Read |
|---|---|---|
| CV import | Durable PDF/DOCX/Markdown import into the **base** profile only, never a tailored CV | [cv-import.md](docs/architecture/areas/cv-import.md) |
| CV artifacts | The base CV and each Application's tailored CV are separate artifacts; tailoring never writes base state | [cv-artifacts.md](docs/architecture/areas/cv-artifacts.md) |
| CV publishing | Read-only, capability-linked share link for a hiring manager | [cv-publish.md](docs/architecture/areas/cv-publish.md) |
| SuperCV | Lossless, revision-checked editing of `profile.md` | [super-cv.md](docs/architecture/areas/super-cv.md) |
| Jobs, scoring, Reject | Postgres is the only Job store; scores are a lazy per-pair cache; there is no Maybe | [jobs.md](docs/architecture/areas/jobs.md) |
| Applications | Apply is a first-class row; one live Application per (Job, Consultant) | [applications.md](docs/architecture/areas/applications.md) |
| Identity, roles, authz | All access decisions live in `authz/policy.py`; Site gates Jobs and matching only | [authz.md](docs/architecture/areas/authz.md) |
| Durable background work | At-least-once invocation and exactly-once effect behind every long-running operation | [workflows.md](docs/architecture/areas/workflows.md) |
| Shared and dormant subsystems | What is left of `shared/`; Learning has no live writer; there is no `verdicts` table | [shared-and-legacy.md](docs/architecture/areas/shared-and-legacy.md) |

Data-store and router-prefix quick-reference tables are in
`docs/architecture/areas/README.md` §"Quick reference".

### Configuration and startup
- `backend/main.py` holds **router composition, lifespan, static mounts and the health check only**. It must not own Job, Application, CV, workflow or authorization logic.
- `backend/config.py` holds all env vars and path constants. **Start here when tracing a path issue.** It also holds the production-auth guard: `AUTH_ENFORCED` defaults to **true** and cannot be turned off when `APP_ENV=production`. It can be turned off only in a development or test process that also sets `CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS=true`.

---

## Retired — do not reintroduce

Reintroducing any of these needs a deliberate product decision, not just a
route definition. A regression test pins each one.

| Retired | Why it must not return |
|---|---|
| `POST /api/import-cv` | Known 504 path that wrote the base profile regardless of the open artifact. The *feature* lives at `POST /api/profiles/{username}/cv-import`, not at this route |
| `POST /api/generate-cv` | Synchronous generation into whatever CV the editor held, the base overwrite that invariant 7b forbids |
| `GET/POST /api/profiles/{u}/cv-state` | Unversioned base CV with no stale-write refusal |
| `POST /api/profiles/{u}/accepted-positions/{job}/status` | The last `verdicts.application_status` writer |
| `GET /api/profiles/{username}/accepted-positions` | Dead reader; publish state comes from `GET /api/publications` |
| `/api/opportunities/ingest`, `/api/opportunities/run-matching` | Belonged to the retired review-batch matcher |
| `/api/dashboard/*` (positions, verdict, complete-cycle, confirm-config, score-jobs) | The Maybe/verdict dashboard |
| `/api/ta/job-store/*`, `/opportunity-dashboard.html` | The JSON Job Store and its page |
| Bearer API-token auth, and its dashboard cards | Existed only for the discarded email-ingestion feature |

---

## Key design invariants — do not break these

> These are the **rules**. Each one's mechanism, reasoning and regression tests
> live in the subsystem contract under `docs/architecture/areas/`. Read that
> before you change anything an invariant covers. Numbering is **stable**:
> source comments cite "CLAUDE.md invariant N" (`backend/matching/service.py`,
> `backend/jobs/routes.py`, `backend/jobs/service.py`), so renumber only
> together with those comments.

**1. Pipeline separation (ADR-001).** The CV pipeline and the matching pipeline are independent; the only shared artefact is the `(consultant, job)` pair. Never route matching logic through the CV pipeline or vice versa.

**2. Profile = facts only.** `profile.md` holds structured facts; presentation belongs in `style.md`. → [shared-and-legacy.md](docs/architecture/areas/shared-and-legacy.md) G7.

**3. LLM as configurator (ADR-003).** Change tuning behavior through the prompt or schema, never through hardcoded rule code. → [shared-and-legacy.md](docs/architecture/areas/shared-and-legacy.md) G3.

**4. Matching-config generation is human-triggered, derived from `profile.md` alone, and never destroys a valid config.** A person presses "Update match configurator", and the LLM generates a whole config from the profile. A generation that fails schema validation leaves the previous valid config in place. The verdict-fed learning loop and its delta guard are removed. Do not rebuild feedback learning without a product decision. → [shared-and-legacy.md](docs/architecture/areas/shared-and-legacy.md), [workflows.md](docs/architecture/areas/workflows.md).

**5. Schema-first.** The `project_context/*.schema.md` files are the LLM/system contract. Update the schema first, then verify the consumer. → [shared-and-legacy.md](docs/architecture/areas/shared-and-legacy.md) G6.

**6. Username normalisation.** Strip `@domain` suffixes consistently, and do not introduce raw email lookups against the profile directory. → [authz.md](docs/architecture/areas/authz.md) "Accepted limitations" (two call sites, not one).

**6a. One authorization policy, role membership, Site only where Jobs need it.** All access decisions live in `backend/authz/policy.py`. Roles are memberships, never `primary_role`. Site is an authorization predicate **only** for Jobs and matching (and for keeping TA↔Consultant assignment within one Site). Consultant-owned records are never gated by Site. → [authz.md](docs/architecture/areas/authz.md) G1–G3.

**7. Scores belong to the Consultant–Job relationship, never to the Job.** The score is computed by deterministic code and cached lazily in one row per pair (`job_consultant_matches`), never as a column on `jobs`. An unscorable pair returns `scored: false` with a **null** score. A low, missing or failed score never hides, rejects or makes a Job ineligible: the Job stays listed, fetchable, editable and rejectable. → [jobs.md](docs/architecture/areas/jobs.md) G2.

**7a. There is no Maybe, and a pair holds one live decision.** A Reject marker and a live Application exclude each other. Do not add a compatibility translation from V1's verdict vocabulary. → [jobs.md](docs/architecture/areas/jobs.md) G4/G5, [applications.md](docs/architecture/areas/applications.md) G3.

**7b. The base CV and each Application-tailored CV are separate artifacts, and tailoring never writes the base.** They have different storage, revisions and editor modes, and the client claims its mode before the first `await`. Writes to base *profile* material (photo, chat patch, import) are gated by one named function, `_baseProfileWritesAllowed()`. → [cv-artifacts.md](docs/architecture/areas/cv-artifacts.md) G1/G3/G12/G13.

**8. No browser scraping, local triage pool or resident scheduler (ADR-010 superseding ADR-008).** → [shared-and-legacy.md](docs/architecture/areas/shared-and-legacy.md) G5.

**9. Human authority over outward and employment-process actions is a hard design rule.** No score or LLM output may submit a Consultant, choose Apply/Reject, publish or send a CV, or terminally delete or anonymize data without a human command. There is **one** narrow exception, and it is not outward-facing: anonymous `link_opened` view bookkeeping. Automatic Site-wide opportunity eligibility is not an exception, because it lists a Job for a Consultant and decides nothing. This is a GDPR Art 22 / automation-bias control, not a claimed AI Act Article 6(3) exemption. The launch gate is `docs/legal/ai-act-assessment.md`.

---

## Running the app

**Everything Python runs inside WSL (Ubuntu):** Docker/Postgres, alembic,
uvicorn and pytest. The Windows interpreter in this checkout is not usable.

```bash
source .venv/bin/activate
make dev          # Postgres + migrations + uvicorn.  http://127.0.0.1:8000/health
```

Seeding, migration policy, the dev container, and the `make migrate-check`
"drift" report that looks like a missing migration but is not are all in
**`docs/running.md`**.

---

## Deployment

Read **`DEPLOY.md`** before any deploy.

### AWS Deploy Checklist
- Before deploying, locate the real alembic migrations directory (verify the path exists and list pending revisions). Never assume the path.
- Always run migrations via the migrate task definition BEFORE rolling the service.
- After `update-service`, confirm the service is actually running the NEW task-def revision (`describe-services`) before debugging app errors.
- Do not touch the infra repo (`terraform plan`/`apply`) unless the user explicitly asks.
- For DB user admin (password resets, user re-creation), use the project's official command via ECS `run-task`, and confirm the target account with the user first.

---

## Testing — read `docs/testing.md` before writing a test

There are four gates, selected by marker: `make test-fast`, `make test-static`,
`make test-integration` (disposable Postgres 16) and `make test-complete`
(everything). `test-complete` already runs the other three, so running all four
runs the suite twice; don't. **`docs/testing.md`** has:

- the full ladder;
- the two load-bearing rules: never read `data/` from a test, and use the
  `actors` fixture for anything authorization-shaped;
- why the suite hashes passwords at `BCRYPT_ROUNDS=4`.

Do not weaken a gate to unblock a change.

---

## Git

- Never run `git init` on a folder that already has a `.git` directory, and never reinitialize without asking.
- The default branch is `main`. Check `git branch --show-current` before giving push instructions.
- When another session owns some paths, exclude them from your commits and confirm with `git status` before committing.
- Before recommending `.gitignore` entries for a directory (e.g. `.claude/`), inspect its contents first.

---

## General Working Rules

- When asked for descriptions, summaries or CV bullets about "the project", survey the whole repo (README, CLAUDE.md, `docs/product.md`, area docs, top-level structure). Do not rely only on the currently open file.
