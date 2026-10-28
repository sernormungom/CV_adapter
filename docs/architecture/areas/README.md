# Subsystem contracts — routing index

Living documentation of how each subsystem behaves **now**. Provider-neutral:
Claude and Codex sessions both read these, and both navigate from this file —
not from a provider-specific entry point.

These are not ADRs. An ADR (`../adr/`) records a decision at a point in time
and is not revised as the code moves. A file here describes current behaviour
and **is** revised with the code that it documents.

## Start here — routing a complaint or a change

**Bug report, not a file path?** Match the symptom, then read that area's
**Contract + Guards** before changing anything. Do not load unrelated areas.

| Symptom | Area contract |
|---|---|
| Hiring-manager link shows nothing, "not found", or "expired" | [cv-publish](cv-publish.md) |
| A shared CV is still reachable after Revoke | [cv-publish](cv-publish.md) |
| Download PDF does nothing in the hiring-manager viewer | [cv-publish](cv-publish.md) |
| Published CV shows wrong fonts/colours, or raw markup | [cv-publish](cv-publish.md) |
| Publish is refused (400 or 409) | [cv-publish](cv-publish.md) |
| "Too many requests" / 429 on a public CV link | [cv-publish](cv-publish.md) |
| CV import finishes but the profile is unchanged, or facts appear twice | [cv-import](cv-import.md) |
| Import stuck on "processing" and never finishes | [cv-import](cv-import.md) |
| Import upload refused (409), or a re-upload does nothing | [cv-import](cv-import.md) |
| An imported profile contains something that was not in the CV | [cv-import](cv-import.md) — see Watch items |
| Import or generation fails with a timeout | [workflows](workflows.md) |
| A background job never runs, runs twice, or reports a contradictory status | [workflows](workflows.md) |
| Two app instances both seem to be processing work | [workflows](workflows.md) |
| Tailoring a CV changed the consultant's own base CV | [cv-artifacts](cv-artifacts.md) |
| CV edits are lost, or a save is refused as out of date (409) | [cv-artifacts](cv-artifacts.md) |
| Generated CV is empty, unstyled, or missing the photo | [cv-artifacts](cv-artifacts.md) |
| SuperCV is empty, loses profile fields, or shows the wrong consultant | [super-cv](super-cv.md) |
| A chat profile patch is lost or refused as stale | [super-cv](super-cv.md) |
| A job will not score, or scores as zero | [jobs](jobs.md) |
| A job was refused as a duplicate, or a duplicate slipped through | [jobs](jobs.md) |
| Cannot reject a job, or a reject cannot be undone | [jobs](jobs.md) |
| A job disappeared from the Jobs list after Reject or Apply | [jobs](jobs.md) "UI entry point" — by design (S21) |
| Apply is refused (409), or a job shows the wrong application state | [applications](applications.md) |
| The "acting for" consultant list is empty or shows only me | [authz](authz.md) |
| The top-banner navigation shows the wrong role's destinations, or loses the selected Consultant across pages | [authz](authz.md) G4 |
| A 403 or 404 where access was expected | [authz](authz.md) — then the owning area |
| Matching config never updates itself | [shared-and-legacy](shared-and-legacy.md) — by design; only "Update match configurator" regenerates it |
| _anything else_ | the Areas table below |

**Changing code rather than chasing a report?** Use the Areas table below —
it maps directories and tables to the same contracts.

## How to use this

Before changing a subsystem, read its **Contract** and **Guards**. That is
enough to implement safely — every constraint needed for correctness lives in
Guards. Load a **History** section only when investigating a regression or
questioning an existing design, and load unrelated areas only when the work
actually reaches them.

## Areas

| Editing… | Read |
|---|---|
| `backend/cv_publish/`, `backend/templates/cv-viewer.html`, `html/cv-viewer.js`, `html/published-cvs.html`, table `cv_publications` | [cv-publish.md](cv-publish.md) |
| `backend/cv_importer/`, table `cv_imports`, the import half of `html/cv-importer.html` | [cv-import.md](cv-import.md) |
| `backend/workflows/`, table `workflow_runs`, any workflow handler | [workflows.md](workflows.md) |
| `backend/cv_state/`, `backend/tailored_cv/`, `backend/cv_pipeline/`, `html/cv-importer.html`, table `tailored_cvs` | [cv-artifacts.md](cv-artifacts.md) |
| `backend/super_cv/`, `html/super-cv.html`, the lossless read/save contract for `profile.md` | [super-cv.md](super-cv.md) |
| `backend/jobs/`, `backend/matching/`, `html/jobs.html`, tables `jobs` / `job_decision_pairs` / `job_reviews` / `job_consultant_matches` | [jobs.md](jobs.md) |
| `backend/applications/`, table `applications`, `html/applications.html` | [applications.md](applications.md) |
| `backend/authz/`, `backend/auth/`, `backend/sites/`, `backend/bootstrap/pilot.py`, `html/auth-guard.js`'s top-banner navigation, `html/consultants.html` | [authz.md](authz.md) |
| `shared/`, `backend/opportunity_pipeline/learning_module/` | [shared-and-legacy.md](shared-and-legacy.md) |

## Cross-cutting files — no area of their own

| Editing… | Read |
|---|---|
| `backend/llm.py`, or **any** LLM call site | [workflows.md](workflows.md) G11 — the two-tier timeout. A workflow handler's call **must** pass `timeout=WORKFLOW_TIMEOUT_SECONDS`; the 30s module default is only for the synchronous chat routes |
| `backend/cv_assistant/` (`/api/chat`, `/api/chat/profile-patch`) | [workflows.md](workflows.md) G11 (it is the tight-timeout tier), [cv-artifacts.md](cv-artifacts.md) G13 (the profile patch writes base profile material and must go through the gate), and [super-cv.md](super-cv.md) G3 (the patch persists through the shared profile lock/revision service) |
| `backend/privacy/`, `backend/retention/` | The GDPR guard in each area that owns the data: [cv-publish.md](cv-publish.md) G13, [cv-import.md](cv-import.md), [cv-artifacts.md](cv-artifacts.md), [shared-and-legacy.md](shared-and-legacy.md) G4 |
| `backend/main.py`, `backend/config.py` | `CLAUDE.md` §"Configuration and startup" — `main.py` is router composition, lifespan, static mounts and the health check **only** |
| `alembic/`, migrations | `docs/running.md` — ordinary forward migrations on top of the baseline |
| Anything under `tests/` | `docs/testing.md` — four gates, markers applied automatically |

## Quick reference

Each area's own Contract section is authoritative — these two tables are a
single-page lookup across all of them, not a second source of truth. If one
disagrees with an area doc, the area doc wins; fix this table.

### Data stores

| Store | Path | Owner |
|---|---|---|
| Identity & roles | Postgres `users`, `user_roles`, `ta_consultants`, `sites` | [authz.md](authz.md) |
| Workflow runs | Postgres `workflow_runs` — references only, never content | [workflows.md](workflows.md) |
| Jobs | Postgres `jobs`, `job_decision_pairs`, `job_reviews`, `job_consultant_matches` — the score is a lazily-filled cache row, not a column on `jobs` (S1) | [jobs.md](jobs.md) |
| Applications | Postgres `applications` — no CV or score column | [applications.md](applications.md) |
| Tailored CVs | Postgres `tailored_cvs` — no stored workflow-run reference | [cv-artifacts.md](cv-artifacts.md) |
| CV imports | Postgres `cv_imports` — no content, no stored workflow-run reference | [cv-import.md](cv-import.md) |
| Consultant profiles | `data/profiles/<username>/` (`profile.md`, `style.md`, `matching_config.yaml`, uploads) | [super-cv.md](super-cv.md), [cv-import.md](cv-import.md), [shared-and-legacy.md](shared-and-legacy.md) |
| Base CV | `data/profiles/<username>/cv_state.json` | [cv-artifacts.md](cv-artifacts.md) |
| CV publications | Postgres `cv_publications` + `data/cv_publications/photos/` | [cv-publish.md](cv-publish.md) |

`JOB_STORE_DIR` and `TA_CONFIG_DIR` are unused isolated data roots kept only
because a test asserts on all five — see [shared-and-legacy.md](shared-and-legacy.md).

### Router prefixes

| Router module | Prefix | Area |
|---|---|---|
| `cv_state/routes.py` | `/api/cv` | [cv-artifacts.md](cv-artifacts.md) |
| `cv_importer/routes.py` | `/api/profiles` | [cv-import.md](cv-import.md) |
| `super_cv/routes.py` | `/api/super-cv` | [super-cv.md](super-cv.md) |
| `cv_publish/routes.py` | `/api/publications` (+ public `/cv/view`) | [cv-publish.md](cv-publish.md) |
| `jobs/routes.py` | `/api/jobs` | [jobs.md](jobs.md) |
| `applications/routes.py` | `/api/applications` | [applications.md](applications.md) |
| `tailored_cv/routes.py` | `/api/applications` | [cv-artifacts.md](cv-artifacts.md) |
| `authz/routes.py` | `/api/me` | [authz.md](authz.md) |
| `learning_module/routes.py` | `/api/opportunities` | [shared-and-legacy.md](shared-and-legacy.md) |

## Writing one

Three sections, in this order:

- **Contract** — modules, routes, storage, authz shape, config. What is true now.
- **Guards** — one entry per constraint: the rule, *why* it exists, and the
  exact regression tests that pin it — full `<file>.py::<test function>` node
  ids, so they are greppable and runnable. Anything a builder must respect
  belongs here, never
  in History. Include an "accepted limitations" list so a reader does not
  helpfully "fix" a deliberate trade-off.
- **Read alongside** (in the header) — the ADRs, legal records or
  `docs/evidence/` files that explain *why*, when they add real rationale. An
  area file carries no delivery history. If losing a link would make an
  implementation unsafe, its content belongs in Guards.

When you add an area, add its row above **and** any symptom rows — here, not
in `CLAUDE.md`.

## Reference IDs

Source comments, tests, ADRs and legal records cite short labels for past
decisions and work items. Their original registers have been retired. This table
gives one line for each label that still appears often, so a comment stays
readable. The rule itself is always in the Guard that cites the label. A label
not listed here is historical; read the code and its Guard.

| Label | Meaning |
|---|---|
| ADR-001 … ADR-012 | Architecture decision records in [`../adr/`](../adr/) |
| P-001 / P-002 / P-003 | Clean-cut rebuild in this repository; disposable-data risk posture; modular monolith on the existing AWS topology |
| P-004 | Real multi-role accounts (roles are memberships) and the fabricated pilot cast |
| P-005 | Originally a single-Site guard, not Site authorization. Site now gates Jobs and matching ([authz.md](authz.md) G3) |
| P-006 | A small durable in-process workflow runner instead of a queue service ([workflows.md](workflows.md)) |
| P-007 | Advisory matching computed per Consultant–Job pair (now cached per pair, [jobs.md](jobs.md) G2) |
| P-008 | Apply/Reject only, with Applications as first-class rows; no Maybe |
| P-009 | Independent base and tailored CVs ([cv-artifacts.md](cv-artifacts.md)) |
| P-010 | Simplified public capability links ([cv-publish.md](cv-publish.md)) |
| P-011 | Browser scraping and the local Cockpit retired |
| P-018 | Automatic email ingestion discarded; API tokens retired |
| P-019 | The AWS app data was wiped and the current version deployed clean; there is no older fallback |
| D-012 / D-013 / D-014 | No permanent third production identity; hardened public CV links (fragment token, hash-only storage, hardened viewer); hardened local auth (password policy, throttling) |
| STAB-1 / 2 / 5 / 6 / 7 | Stabilisation items: "actively looking" opt-out; password policy and login throttling; public-link throttling; API tokens retired; dead-code sweep |
| V3-P-003 / V3-P-011 | Role-specific top-banner navigation and named dashboards; creation is owned by the dashboard |
| V3-P-005 / V3-P-006 | Markdown CV import; lightweight near-duplicate Job check and stored scores |
| V3-P-007 / V3-P-008 | Site-owned Jobs with Site-locked actors; indicator scores as a lazy unversioned cache; matching config generated from the profile alone; verdict learning removed |
| V3-P-015 / V3-P-016 | Scope corrections while building the near-duplicate check |
| V3-P-017 / V3-P-019 | SuperCV: manual, revision-checked editing of `profile.md`; its polish, and the experiment that retired the structured preference fields |
| V3-P-020 | Reject collects no reason |
| V3-P-021 / V3-P-032 | The Jobs page reflects each Consultant's own live decision; the Applications link opens a read-only description |
| V3-P-022 / V3-P-033 | Autosave with no edit must not bump the revision; a content-identical save is a silent no-op |
| V3-P-023 | Tailored CV generation is refused for a Consultant with no photo |
| F-015 / F-025 | Owner feedback: move to newer Claude models; accept Markdown CVs |
| S1 … S26 | Work items. In older comments, "Slice 1–5" and "Step 1" mean earlier work. IDs such as `S4-P1-R7` are review findings within those items |
| M0 / M1 | Early recovery and CI-gate milestones (for example "M1 rollback rule": never weaken a gate to unblock a change) |
