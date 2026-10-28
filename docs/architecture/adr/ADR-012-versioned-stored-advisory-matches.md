# ADR-012: Store Versioned Advisory Matches on the Consultant–Job Relationship

**Status:** Deferred for V2 Pilot by P-007; **decision
adopted and mechanism replaced for V3** by V3-P-008 (stored lazy cache; see
`docs/architecture/areas/jobs.md` G2)
**Date:** 2026-08-21
**Amended:** 2026-09-10 (V3)
**Supersedes:** ADR-007's on-demand-only storage decision
**Related:** ADR-003, ADR-004, ADR-007, V2 Slice 1,
P-007, V3-P-006/P-008

---

## V3 amendment — store the projection, drop the versioning

V3 adopts this ADR's central decision: one derived advisory row per calculated
Consultant–Job pair, never copied onto the Job. Everything this ADR built *on
top* of that is withdrawn, because the product owner clarified that a score is
an indicator rather than a measurement — 50% and 60% carry the same practical
meaning — so a slightly old row is acceptable and freshness is not worth
machinery. Deleted from the design: input hashes and version columns, the
observable `Out of date` state, stale-write refusal, the defined refresh
triggers for Job edits and config changes, and the nightly 03:00 audit.

What replaces them is a write-through cache. The key is `(job_id,
consultant_id)` — no `site_id`, since the Site is the Job's and V3 locks actors
to one Site — with upsert as the only write, which is what makes at-least-once
delivery safe. Rows fill lazily when a Jobs page needs a pair it has no row
for, and are deleted when the Job closes, when the Consultant stops looking, or
by cascade; that event-driven cleanup replaces both the audit and any retention
timer, and bounds the table at open Jobs × looking Consultants per Site.

Measurement supports the omission of eager preparation:
`docs/evidence/v3-scorer-timing-2026-09-10.md` records the deterministic scorer
at ~0.19 ms per realistic posting, so 30 pairs fill in under 6 ms. This ADR's
rejection of "continue computing every score on demand" rested on shared
dashboards "paying for a changing matrix"; at that cost the premise does not
hold, and persistence is justified by SQL-side sorting and filtering instead.

The "Learning configuration" section below is void: V3-P-008 decision 5 deletes
the verdict-fed learning path and its delta guard (see ADR-004's V3
supersession). Everything else this ADR says about scores remaining advisory,
never granting access, never hiding a Job, and Apply never depending on a
score, is retained and reinforced.

---

## V2 Pilot amendment (historical)

V2 Pilot reuses ADR-007's existing on-demand, per-Consultant scorer and stores no
versioned match projection. Scores remain advisory and never affect eligibility.
Implement this ADR only after measured pilot evidence justifies caching and the
active plan/decision log explicitly changes.

---

## Context

ADR-007 correctly established that a match score is not an intrinsic Job property.
It varies by Consultant profile and matching configuration, so putting the last
calculated score on a shared Job caused cross-Consultant contamination. ADR-007
therefore selected on-demand calculation and retained no match projection.

V2 changes the operating model. The cloud presents Site-wide Jobs to several
Consultants and TAs, background work is durable, and dashboards need stable sorting
without synchronously recalculating a matrix on every request. The product owner
also clarified that a score is only an aid for ordering promising positions. It is
not a decision, an eligibility rule, or a reason to discard a Job. Temporary
staleness is acceptable.

## Decision

V2 stores one **derived, versioned advisory projection** in
`job_consultant_matches` for each calculated Consultant–Job pair.

- The unique business key is `(site_id, consultant_id, job_id)`.
- The row stores the deterministic score, a bounded plain-language breakdown,
  input hashes/versions for the Job, profile, matching config, and scorer, plus
  calculation state and timestamps.
- The score remains relationship state. It is never copied onto the Job or treated
  as canonical evidence about the Consultant.
- Dashboard reads use the stored projection for ordering. Missing, failed, or stale
  rows remain visible and usable with an honest `Calculating`/`Out of date` state.
- Scores never reject, discard, hide, suppress, or make a Job ineligible. Apply and
  manual Job access never depend on score availability or value.
- The product-facing Job-review choices remain Apply and Reject only. The existing
  `thresholds.maybe` configuration value may remain as an **internal scoring band**
  during schema transition; it is not a verdict, UI action, or persisted review
  outcome.

### Recalculation and staleness

V2 deliberately does not enqueue immediate recalculation for every Job edit.
Instead, any write that changes a scoring input updates its canonical version/hash
and makes affected projections observably stale. Recalculation is performed by
defined background triggers and the nightly 03:00 audit:

- a newly created Job schedules its row calculations for Looking Consultants;
- a newly enabled Looking Consultant schedules the Consultant column;
- an automatically applied matching-config change schedules the Consultant column;
- explicit operational recovery may schedule a row, column, or cell; and
- the nightly audit compares stored input hashes/versions with canonical Job,
  profile, config, and scorer versions, then schedules missing/stale cells.

A Job edit therefore marks affected match rows stale immediately but relies on the
audit or another defined trigger for recalculation. This protects fast editing and
matches the product decision that score freshness is not business-critical.

### Learning configuration

Learning feedback may produce an LLM-proposed matching configuration. The proposal
auto-applies only after schema validation and the existing delta guard pass. A
validation/guard failure leaves the active configuration unchanged and creates an
Admin action-required item. Applied changes may affect ranking only; configuration
schema and service validation must prevent exclusion/discard rules.

## Consequences

### Positive

- TA and Consultant dashboards sort cheaply and consistently.
- Cross-Consultant contamination remains structurally impossible.
- Explicit hashes make staleness diagnosable rather than silently serving an
  unversioned cache.
- Background failures degrade ranking quality without breaking Jobs,
  Applications, or CV work.
- The nightly audit repairs missed work without requiring an expensive queue fanout
  on every Job edit.

### Trade-offs

- Derived rows consume database space and require stale-state/audit logic.
- A user may temporarily see old ordering after a Job edit; the UI must disclose
  this without implying that the score is precise.
- Tests must cover Postgres uniqueness, version comparisons, concurrent claims, and
  the absence of score-based eligibility filtering.

## Alternatives considered

### Continue computing every score on demand

Rejected for V2. It is logically clean but makes every shared dashboard request pay
for a changing Consultant–Job matrix and gives no durable calculation/error state.

### Recalculate immediately on every input write

Rejected. It turns low-value derived data into a synchronous dependency and creates
unnecessary work during edits. Observable staleness plus defined triggers and the
nightly audit are sufficient for this pilot.

### Store scores on Jobs

Rejected for the reason established by ADR-007: a score belongs to a
Consultant–Job relationship, not to either entity alone.

### Use scores to pre-filter Jobs

Rejected as a product and governance rule. Matching is an ordering aid. Humans may
review any otherwise eligible Job regardless of score.
