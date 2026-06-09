# CV Adapter — Admin Guide

For administrators configuring, deploying, or tuning the system.

---

## First-time setup

```powershell
cd C:\Users\NorbertoMuñozGómez\Desktop\CV_adapter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the secrets file from the template:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your real values:

```env
ANTHROPIC_API_KEY=sk-ant-...
DATA_DIR=data/profiles
```

Never commit `.env`. The `.env.example` file is committed and contains no real secrets.

---

## Environment variables

All variables are read by `backend/config.py` via `python-dotenv`. Directories are created automatically on startup if they do not exist.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | — | Anthropic API key for all LLM calls |
| `DATA_DIR` | no | `data/profiles` | Root directory for consultant profile folders |
| `JOB_STORE_DIR` | no | `data/job_store/jobs` | Directory where scraped job JSON files are written |
| `APPLICATION_TRACKER_DIR` | no | `data/application_tracker` | Directory for per-consultant verdict files |
| `TA_CONFIG_DIR` | no | `data/ta_config` | Directory for job source configs and browser profiles |
| `DEBUG_CV_IMPORT` | no | `false` | When `true`, saves two sidecar files per import alongside the archived CV (see [Debugging a bad CV import](#debugging-a-bad-cv-import)) |

---

## Adding a new consultant

Create the folder under `DATA_DIR` using the consultant's username (no `@domain`):

```powershell
New-Item -ItemType Directory -Path "data\profiles\firstname.lastname"
New-Item -ItemType Directory -Path "data\profiles\firstname.lastname\uploads"
```

The remaining files (`profile.yaml`, `style.md`, `matching_config.yaml`) are created automatically on first use:

- `profile.yaml` — created when the consultant imports their CV for the first time
- `style.md` — bootstrapped by the LLM after the first CV generation and correction cycle
- `matching_config.yaml` — bootstrapped by the Learning Module after the first verdict cycle

The username must match exactly what the consultant types in the app (case-sensitive). The app normalises `@domain` suffixes, so `firstname.lastname@company.com` resolves to `firstname.lastname`.

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

### Delta guards

When the Learning Module proposes updates after a verdict cycle, it enforces these limits per cycle:

| Condition | Action |
|---|---|
| Any scoring weight shifts > ±0.30 | Config held for manual review |
| More than 5 terms removed from any single catalog tier | Config held for manual review |
| `thresholds.keep` or `thresholds.maybe` shifts > ±10 | Config held for manual review |

Held configs are written to `data/profiles/<username>/matching_config.pending.yaml` and presented to the consultant in the dashboard for approval before being applied.

To tighten or loosen these limits, edit the constants in `backend/opportunity_pipeline/learning_module/delta_guard.py`.

### Exploration slots

`matching_config.yaml` → `exploration.slots` controls how many of the 10 jobs surfaced per batch are "exploration picks" — candidates outside the top-8 that expose the consultant to a different role archetype. Valid values: 1 or 2.

---

## Configuring job sources

### sources.yaml

`data/ta_config/sources.yaml` defines which job boards are scraped and with which search parameters. Each entry maps to a Playwright adapter in `backend/opportunity_pipeline/source_collector/`.

Current supported sources: **Inkopio**, **Verama**.

### Browser profiles

Playwright browser profiles persist logged-in sessions so scrapers do not re-authenticate on every run. They are stored under `data/ta_config/browser_profiles/`.

To reset a session (e.g. after a session expires or a login changes):

```powershell
Remove-Item -Recurse -Force "data\ta_config\browser_profiles\<source-name>"
```

The next collection run will prompt for a fresh login.

---

## Tuning the LLM prompt context

The AI behaviour for all four LLM-driven steps is controlled by schema files in `project_context/`. These files are injected as context when calling the LLM. Editing them changes what the LLM produces without touching application code.

| File | Controls | When to edit |
|---|---|---|
| `consultant-profile.schema.md` | Structure of the extracted profile facts (`profile.yaml`) | When adding or changing profile fields |
| `consultant-style.design.md` | Presentation rules per consultant (`style.md`) | When changing how CV style is captured or applied |
| `cv-content.schema.md` | Structure of the generated CV intermediate format | When changing what sections appear in generated CVs |
| `matching-config.schema.md` | Structure of scoring weights and catalogs | When adding new scoring dimensions or term categories |

Treat these files as interfaces. If you change a schema, verify that the downstream code that reads the produced YAML/text is still compatible.

---

## Job store lifecycle

Jobs are stored as individual JSON files under `JOB_STORE_DIR` (`data/job_store/jobs/`). The lifecycle policy is documented in `docs/architecture/adr/ADR-005-job-store-lifecycle.md`.

There is currently no automatic expiry. To purge stale jobs manually, delete JSON files whose `collected_at` date is older than your retention window:

```powershell
# Example: delete jobs older than 60 days
$cutoff = (Get-Date).AddDays(-60)
Get-ChildItem "data\job_store\jobs\*.json" |
  Where-Object { (Get-Content $_ | ConvertFrom-Json).collected_at -lt $cutoff } |
  Remove-Item
```

---

## Dependency note

`requirements.txt` pins `httpx<0.28`. This is intentional — newer `httpx` versions are incompatible with the current `anthropic` SDK. Do not upgrade `httpx` without testing the import and generation endpoints end-to-end.

If a dependency conflict appears after a fresh install, reinstall from the pinned file:

```powershell
python -m pip install -r requirements.txt
```

---

## Deployment notes

See `docs/architecture/adr/ADR-006-deployment-architecture.md` for the authoritative decisions.

Key points:

- Designed for single-machine, single-user local deployment
- CORS is currently open (`allow_origins=["*"]`) in `backend/main.py` — restrict this if exposing to a network
- The `--reload` flag in `uvicorn` is for development only; remove it in production
- If changing the backend port, update any hardcoded `localhost:8000` references in `html/cv-builder-mpya-import_ver5 1.html` and `html/opportunity-dashboard.html`

---

## Troubleshooting

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

### Import or generation fails with an API error

Check the `uvicorn` terminal output. Common causes: invalid or expired API key, upstream rate limit, unsupported file type, or PDF text extraction failure.

### "Collect Jobs" returns no results

The Playwright session may have expired. Reset the browser profile for the affected source (see [Browser profiles](#browser-profiles)) and retry.

### Debugging a bad CV import

If `profile.md` looks empty or wrong after an import, enable debug output to inspect what the parser extracted and what the LLM received:

1. Add `DEBUG_CV_IMPORT=true` to your `.env` file (or set it in the shell before starting the server).
2. Re-upload the CV.
3. Two sidecar files appear in the consultant's `uploads/` folder alongside the archived CV:

| File | Contains |
|---|---|
| `<date>_<filename>_extracted.txt` | The plain text the CV parser handed to the LLM — check this first. If it is empty or garbled, the issue is in `cv_parser.py` (e.g. a table-heavy Word document, a scanned/image PDF). |
| `<date>_<filename>_llm_raw.yaml` | The raw YAML the LLM returned before any parsing — if the extracted text looks good but the profile is wrong, the issue is in the LLM prompt or schema. |

Common root causes:

| Symptom | Likely cause |
|---|---|
| `_extracted.txt` has only a few lines / section headings | Word CV uses tables for layout — check that the DOCX parser walks tables (`cv_parser.py → _extract_docx`) |
| `_extracted.txt` is blank or contains garbled characters | PDF is scanned (image-only) or has encoding issues — `pdfplumber` cannot OCR images |
| `_llm_raw.yaml` has `error:` or `needs_review: true` on everything | CV text was too short or ambiguous; check extraction prompt in `experience_extractor.py` |
| `_llm_raw.yaml` is valid YAML but `profile.md` looks wrong | YAML parse succeeded but data did not pass `_validate()` in `experience_writer.py` |

Remove `DEBUG_CV_IMPORT=true` (or set it back to `false`) when done — sidecar files are only for diagnostic use and are not cleaned up automatically.

---

### `matching_config.pending.yaml` keeps appearing

A verdict cycle triggered a delta guard. Open the dashboard and review the pending config — it will not be applied until the consultant approves or rejects it. If the proposed changes look reasonable, you can also apply the file manually by renaming it to `matching_config.yaml`.
