# ADR-007: Per-Consultant Match Scores Are Computed On Demand, Not Stored

**Status:** Superseded for V2 by ADR-012
**Date:** 2026-06-29

ADR-007 remains the historical explanation for removing Consultant-dependent fields
from shared Job records. V2 preserves that ownership rule but supersedes the
on-demand-only storage decision with a versioned relational match projection; see
ADR-012.

---

## Context

The system began single-user. The Pre-Filter & Matcher scored every active job for
one consultant and wrote the result — `match_score`, `score_breakdown`,
`recommended_status`, `hard_blockers`, `soft_risks`, `job_terms_found`, and a
`batch_selected` flag — **back onto the shared job record** in the Job Store
(one JSON file per job).

Multi-tenancy (ADR-006 deployment, and the TA → many-consultants model) breaks this
assumption. A job is collected once and shared across all consultants, but a match
score is meaningful only relative to a specific consultant: the same job scores
differently for each profile and each matching configuration. With the score stored
on the job, a job file only ever held *the last consultant who happened to be
scored* — wrong for every other consultant, and a race between consultants.

A score is, in fact, a **pure function** of `(consultant profile, matching config,
job)`, computed by deterministic code (no LLM). And a consultant's matching config
changes every learning cycle (ADR-003, ADR-004), so any stored score is stale the
moment that config updates.

---

## Decision

**Match scores are never persisted on the job record. They are computed on demand,
per consultant, wherever they are needed.**

- A Job holds **intrinsic facts only** (title, company, location, text, dates,
  lifecycle status). No scoring column exists.
- **Since V2 Slice 1 the single source of truth is
  `backend/matching/service.py::score_job_for_consultant`**, which scores one
  (consultant, job) pair on request. It reads profile + config, scores live, and
  writes nothing. It never bootstraps a missing config — bootstrapping calls an LLM
  and stays tied to an explicit run-cycle, not a passive view; a missing
  profile/config yields `scored: false` with a `reason` slug and a null score,
  not an exception and not a zero.
  (It replaced `pre_filter_matcher/scorer.py`'s `score_active_jobs`/`score_one`,
  which scanned the retired JSON Job Store and filtered by TA city. City is no
  longer a job-visibility rule: the explicit `job_consultants` link is.)
- Consumers compute as needed: the jobs page's **Score** control today.
  (The Match Watcher was removed and superseded by the TA Cockpit's local
  score-at-collection toasts — and the Cockpit is itself retired, ADR-010.)

**The only per-consultant matching state that is persisted:**

Since V2 Slice 1, **none.** Both items below went with the review-cycle model:

1. ~~**The review batch**~~ (`current_batch.json`) — there is no cycle, no
   curated batch of ten, and no exploration slots to keep stable across a
   session. A consultant sees every Job they are linked to.
2. ~~**The score snapshot on a verdict**~~ — a `job_reviews` marker records the
   decision and an optional free-text reason, deliberately **without** a score.
   Recording the number that was on screen would create exactly the stale,
   authoritative-looking artifact this ADR exists to avoid, and would invite
   reading a human's Reject as score-driven. Slice 2 may revisit this for
   Applications.

---

## Consequences

**Positive:**

- Correct under multi-tenancy: each consultant sees their own scores; no cross-consultant contamination.
- No cache-invalidation problem: scores always reflect the current profile + config + job set. A config update is immediately visible with no recompute-and-rewrite step.
- A Job keeps a single clear responsibility — the posting's own facts plus a lifecycle.
- Scoring is pure and side-effect-free, so it is trivially testable and reusable.

**Negative / tradeoffs:**

- Scores are recomputed on each view. Acceptable: scoring is code-only term matching, sub-second in practice. If volume grows, an in-request memo or a short-TTL cache is the escape hatch — not persistence on the job. (V2 Slice 1 narrows this further: one pair is scored per request, on an explicit click.)
- ~~The review batch is a separate per-consultant artifact to manage.~~ Gone with the cycle model in V2 Slice 1.

**Neutral:**

- This is the same principle as ADR-005's soft-delete rule, applied to a different axis: **state that depends on a relationship between two entities belongs to the relationship, not to either entity.** A score belongs to `(consultant, job)`, not to the job.

---

## Alternatives Considered

**Alternative 1: A `match_scores` table keyed by `(consultant, job)`.**
Rejected as the default. It persists a value that is cheap to recompute and goes stale on every config change, reintroducing an invalidation burden (when to recompute, how to detect a config change) for no real gain. On-demand is simpler and always correct. Revisit only if scoring becomes a measured performance bottleneck.

**Alternative 2: Per-consultant score files.**
Rejected for the same staleness reason, plus more files and read-whole-to-sort cost — consistency with the file store does not justify caching a pure function.

**Alternative 3: Keep scores on the job, namespaced by consultant (a map on the record).**
Rejected. Concurrent consultants would contend on the same file, the record would grow unbounded with tenants, and it still caches a derivable value.
