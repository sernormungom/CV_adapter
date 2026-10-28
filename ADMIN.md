# CV Adapter — Admin Guide

For administrators configuring, deploying, or tuning the system.

---

## First-time setup

The local stack — Postgres, migrations, and the app — all run inside **WSL
(Ubuntu)**. Open a WSL shell in the repo and work from there. (Docker runs in
WSL on this machine; the Windows Docker daemon is not used.)

1. Create and activate a virtual environment, then install dependencies. The
   venv is OS-specific, so create it **inside WSL** — do not reuse a Windows
   `.venv`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

2. Create the secrets file from the template and edit it with your real values:

```bash
cp .env.example .env
```

```env
ANTHROPIC_API_KEY=sk-ant-...
# Postgres. The default matches the docker-compose `db` service; leave as-is
# for local dev, point at RDS in cloud.
DATABASE_URL=postgresql+psycopg://cvadapter:cvadapter@localhost:5432/cvadapter
DATA_DIR=data/profiles
JOB_STORE_DIR=data/job_store/jobs
APPLICATION_TRACKER_DIR=data/application_tracker
TA_CONFIG_DIR=data/ta_config
```

Never commit `.env`. The `.env.example` file is committed and contains no real secrets.

3. Start Postgres and apply the database schema:

```bash
make bootstrap        # starts the Postgres container (waits for healthy) + runs migrations
```

> **Upgrading a local database that predates V2?** V2 Step 1 replaced the twelve
> V1 migrations with a single baseline, so a database still stamped at a V1
> revision fails with `Can't locate revision identified by '<hash>'`. That is
> expected, and there is no migration path by design (P-001: V1 transactional
> data is disposable). Recreate the database instead:
>
> ```bash
> docker compose exec db psql -U cvadapter -d postgres \
>   -c 'DROP DATABASE cvadapter' -c 'CREATE DATABASE cvadapter'
> make migrate && make seed-pilot     # with PILOT_SEED_PASSWORD exported
> ```
>
> This destroys local V1 data. Do not run it against anything you care about,
> and note that a cloud reset is a separate, explicitly approved operation.

4. Seed the V2 Pilot accounts (one-time). Migrations create schema only and
   never accounts, so this explicit bootstrap is the sole place the approved
   fabricated cast is created — `patrik.artman` (TA), `norberto.munoz`
   (Admin + Consultant), `mo.chen` (Consultant), all on the `GBG` Site
   (P-004). The seed provisions all four Sites (GBG, MAL, STH, UPS); the three
   pilot accounts stay on GBG alone:

```bash
read -rs PILOT_SEED_PASSWORD && export PILOT_SEED_PASSWORD   # min 15 chars, not echoed
                                                             # (also: 1 upper, 1 number, 1 symbol)
make seed-pilot
```

The password is read from the **exported environment variable only**. Do not
pass it as `make seed-pilot PILOT_SEED_PASSWORD=...`: a make variable is
expanded into the echoed recipe (visible in terminal output and CI logs) and
into the process's argv, and the shell records it in history. The `read -rs`
form above avoids all three. A rerun of the seed is a validation no-op: it
checks the three-account role/relationship shape and refuses to mix the pilot
seed with any non-pilot account.

`make create-admin EMAIL=... PASSWORD=...` still exists for adding an admin to
an **already-provisioned** database; pass `--site` (`GBG`, `MAL`, `STH`, or
`UPS` — default `GBG`) to place it at a Site other than GBG. It is not an
alternative to step 4 on a fresh one: provisioning any account now requires an
active Site to exist, so on a database with no Site configured yet it fails
with "Choose one configured active Site (GBG, MAL, STH or UPS)"
(`backend/sites/service.py::require_active_site`). It also creates a fourth
account, which the pilot's exact three-account posture (P-004) does not
include — `seed_pilot` will refuse to validate a database it
has been used on. Prefer step 4. Its password is passed on the command line,
so it shares the history/argv exposure described above.

5. Run the app:

```bash
make dev              # ensures the DB is up + migrated, then starts uvicorn with reload
```

Health check: http://127.0.0.1:8000/health → `{"status":"ok"}`. Then open the
app in the browser and sign in as one of the accounts from step 4.

### Day to day

After first-time setup, `make dev` is the only command you need — it brings
Postgres up and applies any new migrations before launching the app (`migrate`
is a fast no-op when the schema is already current). Postgres data persists in
the `pgdata` Docker volume across restarts, so the admin account and all data
survive `make db-down` / reboots. Stop the database with `make db-down`.

> **Why this changed:** the app now depends on Postgres for auth and storage.
> Running `uvicorn` (or `make dev`) without Postgres up makes sign-in hang — the
> login request opens a DB connection that never completes. `make dev` removes
> that trap by starting the DB first.

---

## Environment variables

All variables are read by `backend/config.py` via `python-dotenv`. Directories are created automatically on startup if they do not exist.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | — | Anthropic API key for all LLM calls |
| `DATABASE_URL` | no | local compose Postgres | Postgres connection string (psycopg3). Point at RDS in cloud |
| `WORKFLOW_RUNNER_ENABLED` | no | `true` | Runs the durable background workflow loop in this process. Leadership is held by a Postgres advisory lock, so it is safe to leave on in every task; set `false` only to isolate the web process while debugging (accepted work then stays queued) |
| `WORKFLOW_POLL_SECONDS` | no | `2.0` | How long the runner idles when the queue is empty. It does not sleep between runs while a backlog exists |
| `APP_ENV` | **yes in the cloud** | `development` | `production` \| `development` \| `test`. **Set `production` in every deployed environment** — that is what makes `AUTH_ENFORCED=false` a hard startup failure rather than a merely-discouraged setting |
| `AUTH_ENFORCED` | no | `true` | Authentication/authorization enforcement. `false` is rejected outright when `APP_ENV=production`; a local/test exception also requires `APP_ENV=development` or `test` **and** `CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS=true` |
| `SESSION_COOKIE_SECURE` | no | `false` | Set `true` when served over HTTPS (cloud). Keep `false` for local http. Also gates the HSTS response header (Session 4 of `docs/legal/decisions.md`) |
| `CORS_ALLOWED_ORIGINS` | no | just `APP_BASE_URL` | Comma-separated CORS allowlist (Session 4) — replaces the old wildcard; add an origin here rather than reverting to `*` |
| `APP_BASE_URL` | no | `http://127.0.0.1:8000` | Base URL used to build invite links |
| `DATA_DIR` | no | `data/profiles` | Root directory for consultant profile folders |
| `JOB_STORE_DIR` | no | `data/job_store/jobs` | Unused since V2 Slice 1 (held the retired one-JSON-file-per-job store; Postgres `jobs` is now the only authoritative Job store). Kept only as an isolated test data root — see `tests/test_test_isolation.py` |
| `APPLICATION_TRACKER_DIR` | no | `data/application_tracker` | Legacy per-consultant verdict files; verdict history moved to Postgres and then, with no writer left, was dropped entirely |
| `TA_CONFIG_DIR` | no | `data/ta_config` | Unused since V2 Step 1 (held the retired Cockpit's source config and browser profiles). Kept only as an isolated test data root — see `tests/test_test_isolation.py` |
| `DEBUG_LLM_LOGGING` | no | `false` | When `true`, logs ~400-char previews of raw LLM responses across the CV/matching pipelines (Session 3) — off by default since these previews can contain consultant PII |
| `UPLOAD_RETENTION_DAYS` | no | `30` | Days an uploaded CV file survives after successful import before `purge_old_uploads` deletes it (Session 3) |
| `CV_PUBLISH_TTL_DAYS` | no | `28` | Expiry window for a published CV capability link (ADR-009); republish/extend resets it. Expiry blocks access but deletes nothing; the retention purge (`purge_revoked`) deletes **revoked** rows and their photo files only |

---

## Adding a new consultant

Create the folder under `DATA_DIR` using the consultant's username (no `@domain`):

```powershell
New-Item -ItemType Directory -Path "data\profiles\firstname.lastname"
New-Item -ItemType Directory -Path "data\profiles\firstname.lastname\uploads"
```

The remaining files (`profile.md`, `style.md`, `matching_config.yaml`) are created automatically on first use:

- `profile.md` — created by the durable **Import CV** flow (see
  [CV file upload — restored as durable async import](#cv-file-upload--restored-as-durable-async-import-v2-slice-4)),
  or seeded (`make seed-pilot`), or written by hand
- `style.md` — bootstrapped by the LLM after the first CV generation and correction cycle
- `matching_config.yaml` — generated by `config_bootstrapper.py` from `profile.md` alone when a person presses **Update match configurator** (durable background work). A failed or invalid generation leaves the previous valid config in place

The username must match exactly what the consultant types in the app (case-sensitive). The app normalises `@domain` suffixes, so `firstname.lastname@company.com` resolves to `firstname.lastname`.

---

## Resetting a password

There is no self-service "forgot password" flow. To reset a forgotten password
for any account (admin, TA, or consultant), an admin runs the recovery script
against the database from inside WSL:

```bash
make reset-password IDENTIFIER=ta@example.com PASSWORD="<min-12, upper+number+symbol>"
```

`IDENTIFIER` accepts an email or a username. The new password takes effect
immediately — tell the user out of band so they can sign in, then optionally
reset it again to a value only they know. The database must be up (`make
db-up`); this does not require the app to be running.

A reset also **revokes every live session** for that account and **clears an
active login lockout**. That makes one command the complete containment action
for a suspected compromise (previously a reset left an attacker's already-open
session cookie working until it expired), and it is also how you unlock an
account someone has locked out — see "Password policy and login lockout" below.

### Password policy and login lockout

Every password set through the app or these scripts must be **at least 12
characters and contain an uppercase letter, a number, and a symbol**, and must
not be the account's own username or email local part. This applies at both
places a password is ever set: an invite acceptance (`/html/setup-password.html`)
and this admin reset. `PILOT_SEED_PASSWORD` keeps its stricter 15-character
bootstrap requirement and must also satisfy the rules above, since the seed
sets its three passwords through the same invite path.

**Five consecutive failed logins lock an account for 15 minutes.** The lockout
is per account (not per IP), and a locked account answers exactly like a wrong
password — a generic `401 Invalid credentials` — so nothing tells a guesser
which usernames exist or which are locked. Login attempts made during an active
lockout do not extend it. Two ways out: wait for the window to pass, or run
`make reset-password` (above), which clears the lockout immediately.

A known, accepted trade-off at pilot scale: anyone who knows a real username can
deliberately fail five logins and hold that account out for 15 minutes. It
clears itself and an admin can clear it at once, so it is a nuisance rather than
an availability risk.

---

## Tuning the matching system

### matching_config.yaml

Each consultant has their own `data/profiles/<username>/matching_config.yaml`. It controls how the Pre-Filter Matcher scores job positions.

The full schema is in `project_context/matching-config.schema.md` — read it before editing manually.

Key sections to tune:

| Section | What it controls |
|---|---|
| `scoring_weights` | Relative importance of five scoring dimensions; must sum to 1.0 |
| `thresholds.keep` / `thresholds.maybe` | Score cut-offs for the keep / maybe / reject buckets |
| `term_catalogs` | Keyword lists per category (languages, tools, methods, domains…) with signal weights |
| `role_archetypes` | Target role types, their title/body patterns, and seniority bonus |
| `growth_signals` | Terms the consultant wants to move toward (primary, secondary) and away from (penalize) |
| `interest_signals` | Work-shape preferences (preferred) and things to avoid |
| `blockers.hard` | Job text patterns that force a reject regardless of score |
| `location_scores` | Maps city/work-mode combinations to a practical_fit score |

### The verdict-fed learning path is removed

V1 planned a Learning Module that proposed config updates from a consultant's
accept/reject verdict history, held large per-cycle changes for manual review
(a "delta guard"), and wrote them to a `matching_config.pending.yaml` file for
approval. None of that was ever wired to a live writer, and the whole
mechanism was deleted rather than built (V3-P-008 decision 5): verdict
aggregation, the config-update LLM call, the delta guard, the pending-config
writer, and the `GET /api/opportunities/pending-config` reader are gone, along
with the `verdicts` table itself. There is no pending-config file and no delta
guard to tune. `matching_config.yaml` is regenerated only by the explicit human
**Update match configurator** action, which calls `config_bootstrapper.py`
against `profile.md` alone and leaves the previous valid config in place on a
failed or invalid generation.

### Exploration slots

`matching_config.yaml` → `exploration.slots` is still required by config validation (it must be 1 or 2), but **nothing reads it**: the batch selection that used it (10 jobs per batch, 1–2 "exploration picks") was retired along with the review-batch matcher. Jobs are now scored one Consultant–Job pair at a time. The key can be dropped in a future schema change (`project_context/matching-config.schema.md` and `config_bootstrapper.py` together).

---

## Creating and managing jobs

Browser scraping and the local TA Cockpit are retired (ADR-010: superseded
ADR-008). Jobs are created and managed directly in the cloud UI
(`/html/jobs.html`, or **Jobs →** next to a consultant on the TA dashboard) by a
TA, Consultant, or Admin. Automatic email ingestion was discarded (P-018), so
manual entry is the only ingestion path.

A Job is a **row in Postgres** (`jobs`), not a JSON file, and:

- it is **Site-owned** — it takes its creator's Site, and every open Job
  reaches every active, actively-looking Consultant at that Site automatically.
  A TA reaches every Job at their own Site, and an Admin reaches every Job. An
  actor outside the Site gets a 404, not a 403, so a Job's existence is not
  disclosed (`docs/architecture/areas/jobs.md` G7/G10);
- **exact duplicates are refused by the database** within one Site while a Job
  is open, and a lightweight near-duplicate check runs first. Submitting the
  same posting again is not an error: the response says `duplicate` (or
  `near_duplicate`), points at the Job that already exists, and creates
  nothing;
- **closing is the end state.** There is no expire/restore/grace-period purge
  and no delete route — operational Delete versus hard delete is still an open
  product/legal decision (see `docs/product.md`). A closed Job stays listed
  with its state explicit in `GET /api/jobs` (the Jobs page still hides a Job,
  open or closed, from a Consultant who has rejected or applied to it);
- the **match score is advisory and cached per Consultant–Job pair**, never on
  the Job (`job_consultant_matches`, filled lazily on first read and deleted
  when the Job closes or the Consultant stops looking). A consultant with no
  imported profile or no matching config gets `scored: false` with a `reason`
  — never a zero, and never a hidden Job.

Job creation authenticates by session cookie only. The bearer API-token path
and `local_tools/push_client.py`, once kept for a possible unattended-forwarder
feature, were retired once that feature was discarded (STAB-6, P-018).

---

## Applying: Applications and the one-live-decision rule

Since V2 Slice 2, Apply creates an **Application** — a row in `applications`
with its own identity and lifecycle — rather than a status field on a verdict.
Reject is still a marker on the Job (`job_reviews`). Both are per
Consultant–Job pair, and the pilot rule is that a pair holds **one live
decision**:

- **Apply** (`POST /api/applications`) is refused with 409 while the pair holds
  a Reject marker. Withdraw the Reject first
  (`DELETE /api/jobs/{id}/reject?consultant_username=…`, or **Undo reject** on
  the Jobs page under **Show → Rejected jobs**, since a rejected Job leaves
  that Consultant's default list).
- **Reject** is refused with 409 while the pair holds a live Application. Close
  or withdraw the Application first.
- Both are refused on a **closed** Job, like scoring. A Job closing does *not*
  block finishing an Application that is already in flight.

Apply is safe to retry and to double-click: a repeat while an Application is
live returns the same row and creates nothing. This is enforced by a partial
unique index (`uq_application_live_pair`), not by a lookup, so two concurrent
requests cannot both insert.

**The four stages**, and the single rule governing them — a status may only
move to a strictly higher rank:

| Stage | Live? | Means |
| --- | --- | --- |
| `applied` | yes | Apply recorded; a tailored CV can now be generated for it |
| `submitted` | yes | the consultant has been put forward to the client |
| `closed` | terminal | the process ended |
| `withdrawn` | terminal | we pulled out |

`closed` and `withdrawn` share the top rank, so neither becomes the other and
nothing leaves a terminal state. Re-requesting the current stage is a no-op, not
an error. Once an Application is terminal the pair may be applied to again; that
creates a *new* Application and the old row is kept as history.

There is no automatic retention for Applications — the same open
operational-delete decision as Jobs (see `docs/product.md`). They are deleted
with the consultant on GDPR erasure.

Who may do what: a Consultant applies for themselves, their related TA applies
for them, an Admin reaches every Application. An unrelated TA gets 404 on
everything, including the create call — an Application's existence is itself
content.

---

## Tailored CVs

Each Application owns **one** tailored CV (`tailored_cvs`, unique on
`application_id`). It is a different artifact from the consultant's own CV,
which stays in `data/profiles/<username>/cv_state.json` — generating and editing
a tailored CV never touches it.

A `.cv_state.lock` file appears beside `cv_state.json` in each profile
directory. It is expected and holds nothing: it is the lock two backend tasks
take to make one consultant's save a single compare-and-write, so that two
editors at the same revision cannot both be told they saved while one CV is
lost. It is safe to leave, and safe to delete while nobody is saving (it is
recreated on the next write). Do not delete it while the app is serving.

**Generation is durable.** `POST /api/applications/{id}/cv/generate` returns
**202** and a `workflow_runs` row to poll; a full generation can exceed the
60-second edge, so nothing depends on the request staying open. Clicking again
while one is running answers **200** and hands back the same run. Clicking again
after one *finished* starts a genuine new attempt.

**What it generates from.** Every input — the profile YAML, the style rules, the
base CV's presentation and photo, and the job posting — is snapshotted onto the
row when a person asks. The handler reads that snapshot and opens no file, so
editing `profile.md` while it works cannot change the result, and a re-queued run
produces the same document.

**When a generation fails** the artifact keeps the previous version and records a
bounded reason (`error_code`/`error_message`, never upstream exception text).
The consultant still has an editable CV; **Regenerate** retries.

**Editing.** `html/cv-importer.html?application=<id>` opens the editor in
application mode (a green banner names the position). Add `&print=1` to go
straight to the print dialog — that is the PDF path. A save states the revision
it was based on and is refused with 409 if it is stale, if it names a different
application's CV, or if a generation is about to land. A save identical to what
is stored writes nothing, so the revision only moves on a real edit, not on
every 30-second autosave. The same applies to the consultant's own CV.

**Retention:** a tailored CV is deleted, and its public link revoked, in the
same commit that moves its Application to a terminal status
(`docs/architecture/areas/applications.md` G5). While the Application is live
there is no timed retention (see `docs/product.md`). It is also deleted with
the consultant on GDPR erasure, and disclosed in full (content included) by the
Art 15 export.

**Troubleshooting a stuck generation.** If a row says `generating` but nothing
is happening, check `workflow_runs` for a `queued`/`running` row whose
`idempotency_key` starts `tailored_cv:<artifact id>:`. If there is none, the
process died before enqueuing; asking for the generation again starts a fresh
attempt rather than being refused.

---

## Tuning the LLM prompt context

The AI behaviour for all four LLM-driven steps is controlled by schema files in `project_context/`. These files are injected as context when calling the LLM. Editing them changes what the LLM produces without touching application code.

| File | Controls | When to edit |
|---|---|---|
| `consultant-profile.schema.md` | Structure of the extracted profile facts (`profile.md`) | When adding or changing profile fields |
| `consultant-style.design.md` | Presentation rules per consultant (`style.md`) | When changing how CV style is captured or applied |
| `cv-content.schema.md` | Structure of the generated CV intermediate format | When changing what sections appear in generated CVs |
| `matching-config.schema.md` | Structure of scoring weights and catalogs | When adding new scoring dimensions or term categories |

Treat these files as interfaces. If you change a schema, verify that the downstream code that reads the produced YAML/text is still compatible.

---

## Background workflows (the durable runner)

Long work (tailored generation now; CV import from Slice 4) does not run inside the HTTP
request: CloudFront and the ALB both cut off at 60 seconds while the origin
keeps working, so a retry would duplicate the work. Instead a row is committed
to `workflow_runs` and a background runner executes it.

**Where it runs.** Inside the normal web process — no extra AWS service. Only
one process runs the loop at a time, enforced by a Postgres advisory lock, so
during a rolling deployment the old and new task do not both process the queue.
The task that loses the lock logs `runner disabled in this process` and serves
HTTP normally; that line is expected during a deploy, not a fault.

**Checking on it.** Query the table directly:

```sql
-- what is outstanding
SELECT status, count(*) FROM workflow_runs GROUP BY status;

-- anything failed, with the safe reason
SELECT id, workflow_type, error_code, error_message, attempts, finished_at
FROM workflow_runs WHERE status = 'failed' ORDER BY finished_at DESC LIMIT 20;

-- stuck? (running with no live leader — see below)
SELECT id, workflow_type, stage, started_at FROM workflow_runs
WHERE status = 'running' ORDER BY started_at;
```

`error_message` is deliberately terse. An unexpected exception records only its
type (e.g. `unexpected_error` / `RuntimeError`) because an upstream error text
can contain a prompt, a CV excerpt, or a token — look in the application log for
context, using the run id.

**A run stuck in `running`** means the process executing it died. Restarting the
app fixes it: the new leader re-queues every `running` row at startup
(`recover_interrupted`), and handlers are idempotent, so a run whose effect was
already committed is finalized rather than repeated. There is no lease to expire
and nothing to unstick by hand.

**A failed run stays failed.** Automatic retry with backoff is deliberately not
implemented for the pilot (P-006), and there is no admin
retry console yet: re-running work means enqueueing it again from whatever
feature owns it. A `failed` row is durable evidence — do not delete it to "clear"
a problem.

---

## Job lifecycle

Jobs live in the Postgres `jobs` table (V2 Slice 1). ADR-005's JSON-file store,
with its expire/restore states, grace period, and automatic purge, is retired —
do not follow that ADR's operational instructions.

The pilot lifecycle is deliberately short: a Job is **open** until a person
closes it, and a closed Job is kept. Nothing expires a Job on a timer and no
endpoint deletes one; the retention purge (below) covers uploads and revoked
publications only. Operational Delete versus hard delete is an open
product/legal decision (see `docs/product.md`), so the pilot does not guess at
one.

A Job's decision pairs (`job_decision_pairs`), cached scores, `job_reviews` markers, and `applications` rows
are deleted with the consultant on GDPR erasure; the shared Job itself survives
as business content, with its authorship reference nulled.

---

## Data-subject rights (GDPR) and retention

Full legal context: `docs/legal/decisions.md`. These are the operational runbooks for the two admin-only endpoints it introduced.

### Export request (Art 15/20)

A consultant (or someone on their behalf) asks what data is held on them, or asks for it in a portable format. Respond within **30 days**:

```
GET /api/privacy/consultants/{username}/export
```

Admin session required. Returns a ZIP: the `users` row (no `password_hash`), share-token metadata (never the rendered CV HTML), TA associations, `job_associations.json` (the consultant's Job links and their own Reject markers — decision and timestamp only; Reject collects no reason), `applications.json` (which jobs they were put forward for, each stage and when it last moved), and the full `data/profiles/<username>/` tree including `uploads/`. Neither of the two job-related files ever carries **another consultant's** link, review, or Application on the same shared Job.

### Erasure request (Art 17) / offboarding

Admin-triggered only — a consultant cannot self-serve this. Only resolves accounts with `role = consultant`; it will 404 on a TA or admin username by design (erasure is destructive and must never be able to target a non-consultant account).

```
DELETE /api/privacy/consultants/{username}
```

Ordered hard delete: share tokens → sessions/invites → TA associations → notifications → Applications → Job links and Reject markers → profile directory (incl. uploads) → the `users` row itself. Writes a minimal `AuditLog` row (who erased, when, a one-way hash of the username — never the username or any erased content).

The shared Jobs themselves survive an erasure — they are business content, not the consultant's data — with their authorship reference nulled. What goes is the consultant's link to each Job and their own review markers. Export and erasure cover the same set by design; a test asserts they cannot drift apart.

### Retention purges (Art 5(1)(c),(e))

Runs automatically — a daily lifespan background task in `backend/main.py`
(`_retention_purge_loop`) fires `run_retention_purges` once at startup and
every 24h — and on demand:

```
POST /api/admin/retention/run
```

Both call the same two purges (`backend/retention/service.py::run_retention_purges`):
original CV uploads older than `UPLOAD_RETENTION_DAYS` (default 30); and
**revoked** CV publications (snapshot + photo). Expiry alone blocks access but
deletes nothing (`backend/cv_publish/service.py::purge_revoked`). The admin
endpoint remains useful for running a purge on demand rather than waiting for
the next scheduled cycle. (A third purge, verdict rows older than
`VERDICT_RETENTION_MONTHS`, ran here until the `verdicts` table was dropped.)

The async importer's own transient upload is deleted directly by the handler
on every path (success, failure, and every superseded/reconciled case) rather
than by a scheduled purge — see "A stray file under `.cv_import_tmp/`" below.

---

## Dependency note

`requirements.txt` pins `httpx<0.28`. This is intentional — newer `httpx` versions are incompatible with the current `anthropic` SDK. Do not upgrade `httpx` without testing tailored generation end-to-end (and CV import too, once Slice 4 restores it).

If a dependency conflict appears after a fresh install, reinstall from the pinned file:

```powershell
python -m pip install -r requirements.txt
```

---

## Deployment notes

See `docs/architecture/adr/ADR-006-deployment-architecture.md` for the authoritative decisions.

Key points:

- Designed for single-machine, single-user local deployment
- CORS is restricted to an explicit allowlist (`CORS_ALLOWED_ORIGINS`, `backend/main.py`) since Session 4 of `docs/legal/decisions.md` — add an origin there rather than reopening the old `allow_origins=["*"]`
- The `--reload` flag in `uvicorn` is for development only; remove it in production
- If changing the backend port, update any hardcoded `localhost:8000` references in `html/cv-importer.html`

### Docker deployment basics

The first Docker setup is intentionally simple: one container runs the FastAPI
application, and the local `data/` folder is mounted into the container.

In Docker vocabulary:

| Term | Meaning in this project |
|---|---|
| Image | The packaged app runtime built from `Dockerfile` |
| Container | A running copy of that image |
| Compose service | The `app` service in `docker-compose.yml` |
| Port mapping | `8000:8000`, so Windows can open the app at `http://127.0.0.1:8000/` |
| Volume/bind mount | `./data:/app/data`, so profiles, jobs, and TA config persist |
| Env file | `.env`, loaded by Compose and used by `backend/config.py` |

The container itself is disposable. If it is rebuilt or removed, anything stored
only inside the container can disappear. The important application state belongs
in `data/`, so Compose mounts that folder from the host into `/app/data`.

First run from PowerShell when Docker is installed in Ubuntu WSL:

```powershell
wsl docker compose up -d --build
```

Check status:

```powershell
wsl docker compose ps
```

Follow logs:

```powershell
wsl docker compose logs -f app
```

Stop the app:

```powershell
wsl docker compose down
```

Verify the app:

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health).Content
```

Expected response:

```json
{"status":"ok"}
```

If `make` is available inside Ubuntu WSL, the equivalent commands from
PowerShell are:

```powershell
wsl make docker-up
wsl make docker-ps
wsl make docker-logs
wsl make docker-down
```

From an Ubuntu WSL terminal opened in this repo, use the same targets without
the `wsl` prefix:

```bash
make docker-up
make docker-ps
make docker-logs
make docker-down
```

Rebuild vs restart:

| Command | Use when |
|---|---|
| `wsl docker compose restart app` | You only changed runtime state or want a quick restart |
| `wsl docker compose up -d --build` | You changed dependencies, Dockerfile, or application code copied into the image |
| `wsl docker compose down` | You want to stop and remove the running container |

Current Docker scope:

| Included now | Deferred for later |
|---|---|
| FastAPI app container | Postgres |
| Mounted file-backed data | Cloud deployment target |
| `.env`-based secrets | |
| Health check | |

Browser scraping and a collection scheduler are **not** on this list, and never
will be: ADR-010 permanently retires website scraping in favor of manual `+ Job`
entry (and, later, email ingestion). This matches the current architecture: the
app is a modular monolith with shared file-backed data. Splitting into
microservices should wait until there is a real runtime boundary, such as a
worker queue or database-backed storage layer.

### Docker CI

The standalone `docker.yml` workflow was absorbed into `.github/workflows/ci.yml`'s
`docker` job, so a broken image blocks the same pull-request/manual-dispatch
gate the other gates run under, rather than reporting separately alongside it.
See [README.md](README.md#continuous-integration) for the full gate table.

The `docker` job:

1. Checks out the repository.
2. Creates a CI-only `.env` file with a dummy `ANTHROPIC_API_KEY`.
3. Builds the Docker image (`docker compose build`).
4. Starts the Compose app service.
5. Calls `http://127.0.0.1:8000/health`.

The dummy key is enough for startup and health checks because the job does
not call Anthropic-backed generation endpoints. This is a smoke
test, not a deployment pipeline.

---

## Troubleshooting

### Docker says the daemon is not running

If PowerShell shows an error such as `docker_engine` pipe not found, the Windows
Docker daemon is not running. This repo has been verified using Docker inside
Ubuntu WSL, so use the WSL-prefixed commands:

```powershell
wsl docker compose ps
```

If WSL itself is not running, open an Ubuntu terminal once, then retry.

### Docker container starts but the app fails

Check logs:

```powershell
wsl docker compose logs app
```

The most common cause is a missing or invalid `.env`, especially
`ANTHROPIC_API_KEY`.

### Docker app cannot see profiles or jobs

Confirm the data mount exists inside the container:

```powershell
wsl docker compose exec app python -c "from pathlib import Path; print(sorted(p.name for p in Path('/app/data').iterdir()))"
```

You should see folders such as `profiles`, `job_store`, `application_tracker`,
and `ta_config`.

### `WinError 10013` on startup

Port 8000 is already in use. Run on a different port:

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8010
```

### `KeyError: 'ANTHROPIC_API_KEY'`

`.env` is missing, or `uvicorn` was not started from the project root. Always run from `CV_adapter/`.

### `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'`

`httpx` version conflict. Reinstall dependencies:

```powershell
python -m pip install -r requirements.txt
```

### CV file upload — restored as durable async import (V2 Slice 4)

`POST /api/import-cv` (the old synchronous route) is **permanently retired**,
not merely disabled. It was first disabled because it ran text extraction plus an LLM call on the open HTTP
request — past the verified 60-second CloudFront/ALB ceiling on a full CV —
with no idempotency key, so a browser retrying a timed-out upload ran the whole
import a second time at the origin. It also wrote the consultant's `profile.md`
regardless of which CV the editor had open, which the two-artifact boundary
(P-009) forbids.

Slice 4 restores CV import at a new address, durably:
`POST /api/profiles/{username}/cv-import` (`backend/cv_importer/`). It saves
the upload, returns **202** with a run to poll immediately, and the editor's
**↑ Import CV** button polls `GET .../cv-import/status` until it settles —
exactly the pattern `Tailor CV` already uses on `html/jobs.html`. The uploaded
file is deleted after the handler's last required stage on every path,
including failure and a joined-in-flight duplicate — there is no 30-day
retained copy the way the old route's archived upload was
(`project_context/consultant-profile.schema.md` §0.2).

**What to check if you see a request to `/api/import-cv` in the logs:** it will
still be a 404, and it means a browser is running a cached copy of a page from
before Slice 3. A reload fixes it; nothing is retried at the origin.

The extraction/merge components (`cv_parser`, `experience_extractor`,
`experience_writer`, `profile_merger`) are unchanged — Slice 4 reuses them
exactly, behind the new durable route.

**`DEBUG_CV_IMPORT`'s sidecar-file diagnostics were never carried into the
durable path** — they were orphaned when Slice 3 removed the synchronous route
entirely, and Slice 4 did not reintroduce writing raw extracted text or the
model's raw output to disk (that would sit awkwardly against the plan's
transient-source-deletion floor). Use the runner's own
`stage`/`progress`/`error_code`/`error_message` on the polled status instead
(see below). The flag itself, having had zero Python readers, was removed
entirely in STAB-7 — do not reintroduce it without also reintroducing a real
reader.

### Generation fails with an API error

Check the `uvicorn` terminal output. Common causes: invalid or expired API key, upstream rate limit, unsupported file type, or PDF text extraction failure.

### Debugging a bad CV import

If `profile.md` looks empty or wrong after an import, the durable run's own
status carries the reason — poll `GET /api/profiles/{username}/cv-import/status`
(or check the `cv_imports` row and its linked `workflow_runs` row directly).
`error_code` is a stable slug (`extraction_failed`, `profile_invalid`,
`source_missing`, `unexpected_error`); `error_message` is bounded and never the
model's raw output or extracted CV text, by design (`backend/cv_importer/handler.py`).
A previous version of this section described sidecar debug files written
directly to disk; that facility was not carried into the durable path — see
the note above. The same root causes still apply, diagnosed from the status
and server logs instead of a written-out extract:

| Symptom | Likely cause |
|---|---|
| `error_code: extraction_failed`, no obvious reason | The CV text handed to the LLM was empty or garbled — a table-heavy Word document (check `cv_parser.py → _extract_docx` walks tables) or a scanned/image PDF (`pdfplumber` cannot OCR images) |
| `error_code: profile_invalid` | The LLM's output parsed as YAML but failed `_validate()` in `experience_writer.py` (e.g. no `identity.full_name`, no role groups) |
| Import succeeds but `profile.md` has many `needs_review: true` blocks | Normal for a sparse/ambiguous CV — see the schema's flag-don't-guess rule (`project_context/consultant-profile.schema.md` §4.4); not a failure |

---

### A stray file under `.cv_import_tmp/`

Each consultant's `data/profiles/<username>/.cv_import_tmp/` holds at most one
transient uploaded CV while an import is processing, and the handler deletes
it on every path. A file can occasionally be left behind if that delete itself
fails (a storage hiccup) and the consultant never uploads again afterward —
accepted for this pilot rather than backed by a scheduled sweep, since the
file costs disk and nothing else, and it is removed unconditionally if the
consultant is later offboarded (`DELETE /api/privacy/consultants/{username}`).
To clean one up by hand: confirm no import is currently `processing` for that
consultant, then delete the file directly — it is never referenced once the
row is `succeeded`/`failed`.

---

### `matching_config.pending.yaml` is present

A delta guard held a proposed config. Nothing applies it automatically, and the
V1 dashboard approval controls are gone (V2 Slice 1). Review the file, and if
the proposed changes look reasonable, apply it by renaming it to
`matching_config.yaml`.
