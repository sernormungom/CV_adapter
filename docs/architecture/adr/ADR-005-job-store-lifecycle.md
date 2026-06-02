# ADR-005: Job Store Lifecycle and Position Expiry Policy

**Status:** Accepted  
**Date:** 2026-05-26

---

## Context

MVP 0.0 accumulated job positions in the Job Store without any expiry or cleanup mechanism. Over time the store filled with outdated positions — roles that had closed, been filled, or were no longer relevant. These stale positions contaminated the Dashboard and the matching pipeline: the pre-filter ranked them, the LLM standardised them, and the consultant saw them. Rejecting a closed position wastes a Dashboard slot and produces misleading feedback signal for the Learning Module.

Beyond the Dashboard, the Job Store is also consumed by the CV Preparation Pipeline (ADR-001). The CV pipeline reads raw position text from the Job Store when generating a tailored CV. This creates a second expiry concern: if a position expires after the consultant selects it for CV generation but before the CV is produced, the CV pipeline encounters missing or expired data mid-operation.

Two distinct expiry scenarios must be handled:

1. **Batch expiry** — positions that have closed before a batch cycle runs should never enter the pre-filter. The Dashboard should never show a closed position.
2. **In-flight expiry** — a position that was valid when selected for CV generation may expire while the CV pipeline is operating, or between selection and CV generation.

---

## Decision

**Positions have an explicit lifecycle with two states: active and expired.**

A position transitions from active to expired at **close date + 2 days**. The 2-day buffer accounts for minor data latency from job boards and gives the consultant a small window to act on a position whose closing date passed very recently. The buffer duration is a configurable parameter.

### Batch cycle behaviour

The pre-filter operates only on active positions. Expired positions are excluded at query time — the pre-filter never receives them as candidates. This is enforced by the Job Store's query interface, not by the pre-filter's logic: the store returns only active positions when queried for a batch. Responsibility for expiry enforcement lives in one place.

### CV pipeline behaviour on expired positions

When the CV pipeline receives a `(consultant, position)` handoff, its first action is a **validity check** against the Job Store. If the position is expired, the CV pipeline does not proceed. It notifies the consultant that the position closed and CV generation is not possible.

If a position expires *during* CV generation (after the validity check passes), the CV pipeline completes the generation using the data it has already read. Position data is read once at the start of the pipeline and held in working memory for the duration of that run. The pipeline does not re-query the Job Store mid-operation. When delivering the completed CV, the render step notifies the consultant that the position closed during generation and they should verify availability before sending. This notification responsibility is co-located with the Talent Advisor notification at the render step (per the main design decisions).

### Retention and cleanup

**The Job Store does not hard-delete expired positions.** Expired positions are retained with their expired status. This preserves referential integrity for two consumers:

- The Application Tracker may reference a position by ID for a consultant who applied before it expired.
- The Learning Module reads decision history that includes verdicts on positions now expired. Hard deletion would corrupt the feedback signal.

Expired positions are invisible to the pre-filter and the Dashboard. They are accessible only to consumers that explicitly request them.

**A periodic cleanup process** (frequency configurable, suggested monthly) hard-deletes positions that are both expired and have no Application Tracker references. This prevents unbounded store growth while preserving referential integrity.

---

## Consequences

**Positive:**

- The Dashboard never shows a closed position. This was an explicit MVP 0.0 failure mode; it is structurally prevented here.
- The CV pipeline fails fast and informatively on expired positions. The consultant receives a clear message rather than a silent failure or a long wait.
- The Learning Module and Application Tracker retain full decision history, including verdicts on positions that have since expired. Historical signal is not destroyed by expiry.
- Expiry enforcement is centralised in the Job Store's query interface. Neither the pre-filter nor the CV pipeline need to implement their own expiry logic.

**Negative / tradeoffs:**

- **Soft deletion adds query complexity.** Every query against the Job Store must filter by status. This is mitigated by making the active-only query the default interface and requiring explicit opt-in to query expired positions — but developers writing new queries must be aware of this convention.
- **The 2-day buffer is a product assumption.** It may not hold for all job boards or all markets. The buffer is configurable and should be revisited with real operational data.
- **The "complete mid-run" policy tolerates a small inconsistency.** A CV produced for a position that expired during generation is technically a CV for a closed role. The notification at delivery (described above) is the mitigation; it does not eliminate the inconsistency, it makes it visible.
- **Periodic cleanup requires a scheduled process.** One more daemon to operate, monitor, and maintain. Operational burden is low but not zero.

**Neutral observations:**

- The distinction between "expired" (soft) and "deleted" (hard) is an instance of a general principle: **data with external references should never be hard-deleted until those references are resolved**. This principle will recur as the system grows.
- The validity check at CV pipeline entry is the right place to enforce the expiry rule for the CV pipeline, rather than at the handoff mechanism itself. The handoff (a `consultant, position` tuple) is intentionally thin (ADR-001); adding expiry validation to the handoff would couple the two pipelines more tightly than intended.

---

## Alternatives Considered

**Alternative 1: Hard-delete expired positions immediately.**  
Rejected. Breaks Application Tracker references and Learning Module history. Short-term simplicity is not worth the long-term integrity cost.

**Alternative 2: No expiry — show all positions, let the consultant filter.**  
Rejected. This was MVP 0.0's failure mode. The consultant's time is the scarce resource; making them manually filter expired positions is a design failure.

**Alternative 3: Expiry enforced at the pre-filter, not the store.**  
Rejected. Centralising expiry logic in the store's query interface is strictly better than distributing it across consumers. Each consumer implementing its own expiry check is a maintenance burden and a source of inconsistency — two consumers may implement the rule slightly differently, and the difference will cause bugs.

**Alternative 4: Abort CV generation if position expires mid-run.**  
Rejected as the default behaviour. Aborting mid-run produces a worse outcome than completing: the consultant loses a potentially useful CV, receives no output for their wait, and must restart. The position is likely still technically open (it expired during a run measured in seconds to minutes). Completing the run and flagging the status at delivery is the better tradeoff. An abort policy could be appropriate if CV generation were a long-running process measured in hours — it is not.
