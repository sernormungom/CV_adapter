# ADR-008: Job Triage Stays Local (the TA Cockpit)

**Status:** Superseded by ADR-010
**Date:** 2026-07-03
**Supersedes:** the Match Watcher decision recorded in ADR-006 / the roadmap (Phase 3c)
**Superseded by:** ADR-010 (Retire Job Scraping; Use Email as the Only Automatic Job-Ingestion Channel)
**Related:** ADR-001 (pipeline separation), ADR-005 (job store lifecycle), ADR-007 (per-consultant scoring)

---

## Context

The system is already split at the **collection boundary** (see `CLAUDE.md`). The
cloud backend (`backend/`) does storage, matching, dashboard, learning, CV import /
generation, and auth — Postgres + Claude API, **no browser**. Browser scraping lives
in `local_tools/` on the Talent Advisor's (TA's) laptop and pushes jobs to the
cloud's ingest API with a bearer token. The only shared code is `shared/` — the
stdlib-only job-record contract and collection engine.

This ADR answers the next question up the pipeline: **where does job _triage_
happen?** After collection, someone (or something) must look at the raw jobs, decide
which are worth a consultant's attention, and promote those into the cloud job store.
Two placements were on the table:

1. **Push everything to the cloud**, store it with a `staged` status, and give the TA
   a cloud-hosted triage view to promote or discard staged jobs.
2. **Triage locally**, on the TA's laptop, in a small localhost web app (the "TA
   Cockpit"), and push only the jobs the TA promotes.

The context that forces the choice:

- **Collection is already irreversibly local and human-in-the-loop.** Boards like
  Inkopio and Verama need a real, *headed* Chrome on a real desktop session, sometimes
  with the TA clicking through a login wall. Headed browsers cannot run on a cloud
  server. Whatever triage model we pick, the jobs originate on the laptop.
- **The killer feature is a Windows toast at the moment of collection** — e.g.
  "AUTOSAR Developer @ Volvo — 82 for Norberto" — fired the instant a matching job is
  scraped. That requires scoring *at collection time*, on the laptop, with no cloud
  round-trip in the hot path.
- **The cloud job store should stay curated.** Consultants (and the consultant-facing
  matching pipeline) should only ever see jobs a TA judged worth promoting. Raw,
  unfiltered board output does not belong in the shared store.

Cloud *storage cost* was evaluated as a possible factor and **explicitly ruled out**:
staged job text would cost well under \$0.01/month. It is not a reason for either
option. The decision rests entirely on the three points above.

---

## Decision

**Job triage happens locally, on the TA's laptop, in the TA Cockpit — a small FastAPI
app bound to `127.0.0.1`. The TA is the ingestion filter: only jobs the TA promotes
are pushed to the cloud job store.**

Rationale (the three points, stated as the decision's load-bearing reasons):

1. **Collection is human-in-the-loop Playwright.** It is already local and cannot
   move to the cloud (headed Chrome, login walls). Putting triage next to collection
   keeps the whole scrape → score → decide → push loop on one machine, with no cloud
   round-trip inserted into a workflow that is local by physics.
2. **Instant toasts require score-at-collection, locally.** The notification feature
   is the point of the cockpit. Scoring must run on the laptop the moment a job lands
   so the toast fires immediately. This is only possible if the scorer (and the
   per-consultant configs it needs) live locally — see the shared-scoring move in the
   companion plan.
3. **TA-as-filter keeps the cloud store curated.** Consultants only ever see jobs a
   TA promoted. The local pool absorbs the raw, noisy, high-volume board output; the
   cloud store receives the curated subset.

The cockpit's shape (the detailed companion plan is no longer in the tree):

- A **local SQLite pool** holds collected jobs and their triage state
  (`new | seen | pushed | discarded`) — **not scores**.
- A **scheduler** (in-process timer) triggers collection periodically.
- A **collection runner** spawns collection as a **subprocess** (see invariants).
- **Config sync** pulls each actively-looking consultant's profile + matching config
  from the cloud (`GET /api/ta/matching-configs`, new) and caches them on disk.
- **Score-at-collection** scores new pool rows for each looking consultant via
  `shared/scoring.py` and fires a **Windows toast** for any job at/above the notify
  threshold.
- **Push** sends promoted jobs to the existing `POST /api/opportunities/ingest` with
  the bearer token.

This **supersedes the Match Watcher** (ADR-006 / roadmap Phase 3c): the cloud-side
scheduled daemon that ran the matcher per consultant and sent notifications. Its code
was removed in commit `c911cc2`; its notification role is now served by local
score-at-collection toasts, which are strictly better for this use case (instant, no
cloud round-trip, fired at the source of the event).

### Notification requirement

The TA wants an **instant Windows toast** — "82 for Norberto" — as soon as a matching
job is collected, with no cloud round-trip. The toast click opens the cockpit
dashboard with the job highlighted. The same `(job, consultant)` pair is never
toasted twice (notified pairs are recorded in the local pool). Default notify
threshold: the consultant's `thresholds.keep` from their matching config, with a
per-TA local override. This requirement is the primary driver of the local-triage
decision — it is not satisfiable by any cloud-triage design without a round-trip.

---

## Consequences

**Positive:**

- The whole collection→triage loop lives on one machine, matching where the work
  physically happens. No cloud round-trip in the notification hot path.
- Toasts are instant and offline-tolerant (the scorer and cached configs are local).
- The cloud job store stays curated — consultants never see raw board noise.
- The cloud backend stays browser-free *and* triage-free by construction; its ingest
  surface is unchanged (the cockpit just pushes to the existing endpoint).
- Reuses the existing bearer-token auth and `position_writer` dedup — no new cloud
  trust surface beyond the new read-only configs endpoint.

**Negative / tradeoffs:**

- Triage state (the SQLite pool) now lives per-laptop and is not centrally visible.
  If a laptop dies, its un-pushed triage state is lost — but the source jobs are
  re-collectable and nothing curated (already pushed) is lost.
- Scoring logic must be usable on both sides. This forces the `scoring_engine.py` →
  `shared/scoring.py` move (companion plan, Session 2) — a one-time refactor cost.
- The cockpit is a second app to install, run, and support on TA laptops (mitigated
  by a setup script and auto-start, companion plan Session 6).
- Consultant profiles + configs are now cached on TA laptops (a data-locality /
  privacy consideration — noted for the configs-endpoint payload decision).

**Neutral:**

- This extends the ADR-001 pipeline-separation logic outward: the matching pipeline's
  *input filter* is now a distinct local system, sharing only the thin ingest
  contract with the cloud. Same principle, one boundary further out.

---

## Key invariants that must survive this work

1. **Scores are never persisted onto job records — in the local pool too.** ADR-007
   holds unchanged on both sides. A score is a pure function of
   `(consultant profile, matching config, job)` and is computed on demand wherever
   needed. The SQLite pool stores triage state (`new | seen | pushed | discarded`),
   **never score columns**. `shared/scoring.py` is the single scoring implementation,
   called identically by the cloud (`scorer.py`) and the cockpit.
2. **Cloud-side dedup in `position_writer` (`dedup_key`) stays.** It is the
   idempotency guarantee for pushes: retry-safe, safe when two TAs push the same job,
   and shared with the paste path. Local dedup in the pool (via the
   `shared/job_record.py` identity key) serves a *different* purpose — collapsing
   duplicate scrapes before triage. **Both exist**; neither replaces the other.
3. **The local web server never runs Playwright in-process.** Collection always runs
   as a spawned subprocess
   (`subprocess.Popen([sys.executable, "-m", "local_tools.cli", ...])`). This avoids
   the sync-Playwright-inside-asyncio failure and lets headed Chrome open on the TA's
   desktop session. **This is a hard rule, not a preference.**
4. **`local_tools` (and the cockpit app under `local_tools/app/`) import `shared/`,
   never `backend/`.** This keeps the local side splittable into its own repo later
   (per the cloud/local split). The cockpit reaches the cloud only over HTTP (ingest
   push, configs pull), never by importing backend code.

---

## Alternatives Considered

**Alternative 1 (rejected): Push everything to the cloud with a `staged` status and a
cloud triage view.** All scraped jobs go to the cloud immediately, land with
`status = staged`, and the TA promotes/discards them from a cloud-hosted page.

Rejected because:

- **It breaks the notification timing.** Instant score-at-collection toasts are the
  point (§ Notification requirement). A cloud-triage model inserts a round-trip
  (push → cloud score → notify back) into a workflow whose events originate on the
  laptop — the toast would lag the collection it is announcing, or require the cloud
  to push notifications back to the desktop, reintroducing exactly the complexity the
  removed Match Watcher had.
- **It adds a cloud round-trip to an already-local workflow.** Collection is
  irreversibly local; making triage cloud-side means data leaves and returns for a
  decision made next to where the data was born.
- **It pollutes the curated store.** Staged jobs sitting in the cloud store (even
  behind a status flag) put raw board noise one query mistake away from consultants,
  and burden every job-store query with a status filter for jobs that were never
  meant to be shared. (Storage *cost* is not the reason — that was measured at
  <\$0.01/month and dismissed.)

**Alternative 2 (rejected): Keep the cloud Match Watcher for notifications.** Retain
the scheduled cloud daemon that scores per consultant and notifies the TA. Rejected
for the same round-trip/timing reasons; its code was already removed in `c911cc2`,
and local score-at-collection covers its role instantly and without a cloud daemon to
schedule, monitor, and keep idempotent.

**Alternative 3 (rejected): Run Playwright inside the cockpit web process.** Simpler
deployment (one process). Rejected — it hits the sync-Playwright-inside-asyncio
failure and cannot reliably open a headed browser on the desktop session. The
subprocess boundary (invariant 3) is non-negotiable.
