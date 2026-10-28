# Shared primitives and dormant subsystems

**Owns:** `shared/`, `backend/opportunity_pipeline/learning_module/`, and the
record of what was retired. The `verdicts` table it used to also own was
dropped in V3 packet S2 (V3-P-008 decision 5).
**Decisions:** ADR-003, ADR-004, ADR-010, V3-P-008; P-018 (Slice 6
discarded); invariants 3, 4, 8.
**Read alongside:** [ADR-003](../adr/ADR-003-llm-as-configurator.md) (LLM as
configurator), [ADR-004](../adr/ADR-004-learning-module.md) (learning module
and delta guards, with its V3 supersession),
[ADR-008](../adr/ADR-008-local-ta-cockpit.md) (the original Cockpit decision)
and [ADR-010](../adr/ADR-010-email-only-automatic-job-ingestion.md) (its
retirement) — kept as historical record.

Read this before assuming any of it is live. **Most of this area has no live
writer**, and that is the single most important fact in it.

---

## Contract

### `shared/` — what is left

| File | Status |
|---|---|
| `shared/job_record.py` | **Live**, but only `clean_text`, reached from `POST /api/jobs` via `backend/jobs/service.py::build_input`. |
| `shared/scoring.py` | **Live** — the deterministic scoring engine. `score_job()` takes an optional precomputed `evidence_index` for bulk callers. |
| `shared/matching_config.py` | **Live** — `validate_config()`, the matching-config sanity check (required keys, weight sum, threshold ordering), used by `backend/matching/config_reader.py`. |

STAB-7 removed `shared/job_record.py`'s identity-derivation chain (native id →
identity key → stable `job_id`, `build_job_item`) once its only caller,
`shared/collection.py`, was deleted, and removed the now-empty `local_tools/`
package.

### Learning module — one surviving file, the verdict-fed rest deleted

`backend/opportunity_pipeline/learning_module/` holds only
`config_bootstrapper.py` now: it already derives a whole config from
`profile.md` alone, which is what makes it survive V3-P-008 decision 5
untouched. `history_aggregator.py`, `config_update_prompt.py`,
`delta_guard.py`, `config_updater.py`, `config_writer.py`, and `routes.py`
(`/api/opportunities`, the pending-config reader) are deleted (V3 packet S2) —
they had no live writer to justify keeping, and decision 5 orders the
mechanism removed, not merely left unreachable. `backend/main.py` no longer
mounts a learning router.

Always read `project_context/matching-config.schema.md` alongside this code.

---

## Guards

### G1 — The verdict-fed learning path is deleted, not merely deferred (invariant 4, V3-P-008)

The old deferred "Automatic Learning/config updates" item described a
mechanism that never had a live writer — the V1 confirm/discard endpoints
lived in the retired review dashboard, so no live entry point ever reached
`history_aggregator.py`. V3-P-008 decision 5 orders it removed outright rather
than left deferred: verdict aggregation, the config-update LLM call, the
proposed-change diff reader, and the delta guard are gone from the code (V3
packet S2). The config is derived from `profile.md` alone by
`config_bootstrapper.py`, re-run only by an explicit human "Update match
configurator" action (S1).

**Invariant 4 now states that surviving rule** — human-triggered, profile-only,
never destroys a valid config on a failed generation — **not** the deleted
mechanism. Do not read this guard as a requirement to rebuild auto-apply
learning; that needs a fresh decision and design, not a revival of ADR-004.

### G2 — void (delta guard deleted, V3 packet S2)

`delta_guard.py` and its per-cycle parameter-shift limits are deleted along
with the rest of the verdict-fed path (V3-P-008 decision 5): with no automatic
proposal left to produce, there is nothing left to cap. ADR-004's V3
supersession records the two rules that outlive it — ordering-only effect, and
the LLM stays the configurator (ADR-003, G3 below). Do not reintroduce a delta
guard without a fresh decision authorizing a new proposal-generating
mechanism.

### G3 — The LLM is the configurator; do not replace it with static heuristics (invariant 3, ADR-003)

To change tuning behaviour, change the prompt in `config_bootstrapper.py` or
the schema (`project_context/matching-config.schema.md`) — **not** hardcoded
rule code.

### G4 — The `verdicts` table is dropped; nothing reads or writes one, at all

V3 packet S2 (V3-P-008 decision 5) dropped the
`verdicts` table (`alembic/versions/0003_v3_drop_verdicts.py`) and deleted
`verdict_store.py`. Its last two readers — the GDPR export
(`backend/privacy/service.py`) and the daily retention purge
(`backend/retention/service.py`) — no longer reference it; `history_aggregator.py`
(unreachable) was deleted with the rest of the learning module.
`save_verdicts` was removed in STAB-7 with its last callers. Slice 1 retired
the dashboard, Slice 2's `applications` became the source of truth for
application state, and Slice 3 removed the last writer
(`POST /api/profiles/{u}/accepted-positions/{job}/status`). STAB-7 removed the
last dead *reader*, `GET /api/profiles/{username}/accepted-positions`.
**CV-publish state comes from `GET /api/publications`, never from a verdict.**
A grep for `verdict` across the tree should return only this kind of
deliberate historical record.

### G5 — The Cockpit is retired and must not come back (invariant 8, ADR-010)

ADR-010 superseded ADR-008. V2 Step 1 removed the Cockpit, Playwright
adapters, local pool, scheduler, toasts, and setup tooling. STAB-6 removed
`local_tools/push_client.py` and the bearer API-token path; STAB-7 removed
`shared/collection.py` and the empty `local_tools/` package.
**Do not reintroduce browser scraping, a local triage pool, or a resident
scheduler without a new ADR that changes ADR-010.**

### G6 — Schema-first (invariant 5)

The four `project_context/*.schema.md` files are the contract between LLM calls
and the rest of the system. Changing one changes what the LLM produces, which
may break downstream parsers or the Jinja2 template. Update the schema, then
verify the consumer.

### G7 — Profile = facts only (invariant 2)

`profile.md` stores structured facts (skills, tenure, titles); presentation
decisions belong in `style.md`. Never write presentation rules into
`profile.md`.

### Isolated data roots that nothing uses

`JOB_STORE_DIR` (`data/job_store/jobs/`) and `TA_CONFIG_DIR`
(`data/ta_config/`) still exist as constants in `backend/config.py` and
`conftest.py`, but nothing reads or writes either, and neither is created at
import. **The constants stay** because `tests/test_test_isolation.py` asserts
on all five roots, and that gate is not weakened to shorten a list.
