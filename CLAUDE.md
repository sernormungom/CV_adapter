# CV Adapter — Context for Claude Code

This file tells you what to load and what to know before working on any part of this codebase.

---

## Architecture in two sentences

Two independent pipelines share a file-based data store. The **CV Prep Pipeline** imports consultant CVs and generates tailored CVs. The **Opportunity Matching Pipeline** collects jobs, scores them, surfaces matches in a dashboard, and learns from consultant verdicts.

---

## What to ignore

`TemporaryInspiration/` is an archive of prior MVPs and prototyping work. Do not load or reference it unless the user explicitly asks for historical context. It does not reflect the current architecture.

---

## File map — load these files for each feature area

### CV import (`/api/import-cv`)
- `backend/cv_importer/file_receiver.py` — upload endpoint
- `backend/cv_importer/cv_parser.py` — text extraction from PDF/DOCX
- `backend/cv_importer/experience_extractor.py` — LLM extraction call
- `backend/cv_importer/experience_writer.py` — writes `profile.yaml`
- `project_context/consultant-profile.schema.md` — the schema the LLM must produce; **read this first**

### CV generation (`/api/generate-cv`)
- `backend/cv_pipeline/handoff_receiver.py` — generation endpoint
- `backend/cv_pipeline/context_assembler.py` — assembles (profile + job description) into LLM context
- `backend/cv_pipeline/cv_content_generator.py` — LLM call
- `backend/cv_pipeline/cv_renderer.py` — Jinja2 rendering
- `backend/templates/cv.html.jinja2` — the template
- `project_context/cv-content.schema.md` — intermediate format the LLM must produce; **read this first**
- `project_context/consultant-style.design.md` — per-consultant presentation rules injected into context

### Pre-filter matcher (`/api/opportunities/...`)
- `backend/opportunity_pipeline/pre_filter_matcher/batch_assembler.py`
- `backend/opportunity_pipeline/pre_filter_matcher/exploration_selector.py`
- `project_context/matching-config.schema.md` — scoring schema; **always load alongside matcher code**

### Learning module (`/api/opportunities/...`)
- `backend/opportunity_pipeline/learning_module/history_aggregator.py` — aggregates verdict history
- `backend/opportunity_pipeline/learning_module/config_update_prompt.py` — builds the LLM prompt
- `backend/opportunity_pipeline/learning_module/delta_guard.py` — enforces change limits
- `backend/opportunity_pipeline/learning_module/config_bootstrapper.py` — first-time config generation
- `backend/opportunity_pipeline/learning_module/config_updater.py` — applies or holds the proposed config
- `backend/opportunity_pipeline/learning_module/config_writer.py` — writes `matching_config.yaml`
- `backend/opportunity_pipeline/learning_module/routes.py` — endpoints
- `project_context/matching-config.schema.md` — always load alongside learning module code

### Job collection (scrapers)
- `backend/opportunity_pipeline/source_collector/board_connector.py`
- `backend/opportunity_pipeline/source_collector/inkopio_playwright_adapter.py`
- `backend/opportunity_pipeline/source_collector/verama_playwright_adapter.py`
- `backend/opportunity_pipeline/source_collector/position_writer.py`
- `data/ta_config/sources.yaml` — source configuration (queries, URLs)

### Dashboard (`/api/dashboard/...`)
- `backend/opportunity_pipeline/dashboard/routes.py`
- `html/opportunity-dashboard.html`

### Configuration and startup
- `backend/main.py` — router registration, static mounts, health check, username normalisation
- `backend/config.py` — all env vars and path constants; **start here when tracing a path issue**

---

## Data stores

| Store | Path | Format | Written by |
|---|---|---|---|
| Consultant profiles | `data/profiles/<username>/` | `profile.yaml`, `style.md`, `matching_config.yaml`, `uploads/` | CV importer, learning module |
| Pending config | `data/profiles/<username>/matching_config.pending.yaml` | YAML | Learning module (when delta guard triggers) |
| Job store | `data/job_store/jobs/` | One JSON file per job, keyed by job ID | Source collector |
| Application tracker | `data/application_tracker/<username>_verdicts.json` | JSON | Dashboard |
| Source config | `data/ta_config/sources.yaml` | YAML | Admin (manual) |
| Browser profiles | `data/ta_config/browser_profiles/` | Playwright state dirs | Playwright adapters |

All directory paths come from `backend/config.py`. Never hardcode `data/` paths.

---

## Router prefix map

| Router module | Registered prefix | Key endpoints |
|---|---|---|
| `cv_importer/file_receiver.py` | `/api` | `POST /api/import-cv` |
| `cv_pipeline/handoff_receiver.py` | `/api` | `POST /api/generate-cv` |
| `source_collector/routes.py` | `/api/opportunities` | collect, list jobs |
| `pre_filter_matcher/routes.py` | `/api/opportunities` | score, batch |
| `dashboard/routes.py` | `/api/dashboard` | get matches, post verdict |
| `learning_module/routes.py` | `/api/opportunities` | trigger update, approve pending |

---

## Key design invariants — do not break these

**1. Pipeline separation (ADR-001).** The CV pipeline and the matching pipeline are independent. The only shared artefact is `(consultant_username, job_id)`. Never route matching logic through the CV pipeline or vice versa.

**2. Profile = facts only.** `profile.yaml` stores structured facts (skills, tenure, titles). Presentation decisions belong in `style.md`. Never write presentation rules into `profile.yaml`.

**3. LLM as configurator (ADR-003).** The Learning Module uses an LLM to propose `matching_config.yaml` updates. Do not replace this with static heuristics. If you need to change tuning behaviour, change the LLM prompt (`config_update_prompt.py`) or the schema (`matching-config.schema.md`), not hardcoded rule code.

**4. Delta guards (ADR-004).** Limits in `delta_guard.py` cap per-cycle parameter shifts: weight changes > ±0.30, threshold changes > ±10, or > 5 term removals from a single tier trigger a hold. Do not remove these without understanding why they exist.

**5. Schema-first.** The four `project_context/*.schema.md` files are the contract between LLM calls and the rest of the system. Changing a schema changes what the LLM produces, which may break downstream parsers or the Jinja2 template. Treat them as interfaces: update the schema, then verify the consumer.

**6. Username normalisation.** `_normalize_username()` in `main.py` strips `@domain` suffixes. Use it consistently — do not introduce raw email-address lookups against the profile directory.

---

## Running the app (PowerShell)

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Health check: http://127.0.0.1:8000/health → `{"status":"ok"}`
