# ADR-001: Separate the Opportunity Matching and CV Preparation Pipelines

**Status:** Accepted  
**Date:** 2026-05-26

---

## Context

MVP 0.0 implemented two workflows — opportunity matching and CV preparation — as a tightly coupled system. The CV preparation pipeline depended on internal state and intermediate artifacts produced by the matching pipeline, and the two shared control flow as well as data.

This coupling produced several concrete problems in 0.0:

- Changes to the matching pipeline's internal data shape forced corresponding changes in the CV pipeline, even when the CV pipeline's behaviour was unchanged.
- The CV pipeline could not be run independently for a position that had not been processed by the full matching workflow, which limited operational flexibility (e.g., generating a CV for a position discovered outside the system).
- Failures in matching cascaded into the CV pipeline, even when CV generation had no logical dependency on the failed matching step.
- Reasoning about each pipeline in isolation was difficult, because their boundaries were not enforced anywhere in the architecture.

For MVP 1.0, both pipelines are being redesigned. This decision determines how they relate to each other.

---

## Decision

The Opportunity Matching Pipeline and the CV Preparation Pipeline are designed as **two independent systems that share state through common stores but do not share control flow**.

The only handoff between them is the tuple **(consultant, position)** — produced by the consultant's action in the Dashboard, consumed as input by the CV Preparation Pipeline.

The CV Preparation Pipeline reads everything else it needs (raw position text, consultant experience data) directly from the shared stores — the Job Store and the Profile & Configuration Store — without invoking any matching-pipeline components.

Both pipelines may read from and write to the shared stores. Specifically, the CV Preparation Pipeline writes new experience entries to the Profile & Configuration Store (via the CV Importer, and via consultant input captured during CV drafting). The Learning Module of the Matching Pipeline writes matching configuration to the same store. These writes target distinct logical schemas within the store.

---

## Consequences

**Positive:**

- Each pipeline can be developed, deployed, tested, and reasoned about independently.
- The CV pipeline can be invoked for any `(consultant, position)` pair, including positions discovered outside the matching workflow, as long as the position exists in the Job Store.
- A failure in matching does not block CV generation for previously selected positions.
- The architecture diagram tells the truth: the boundary between the two systems is visible at L2, and changes confined to one pipeline are visibly confined.

**Negative / tradeoffs:**

- The shared stores become a coupling point. Schema changes in the Job Store or Profile & Configuration Store now affect two systems. This requires that **schema evolution be treated as a versioned, deliberate activity**, with both pipelines being updated coherently when the schema changes.
- "Independent control flow" means the CV pipeline operating on stale or expired data is now a real failure mode. ADR-005 (Job Store Lifecycle) addresses this by requiring the CV pipeline to check position validity at handoff time and refuse to operate on expired positions.
- Some duplicated reading occurs: both pipelines read position data from the Job Store. This is accepted as the cost of independence.

**Neutral observations:**

- The two pipelines are not symmetric. The Matching Pipeline writes more aggressively (new positions, matching configuration, decision history); the CV Pipeline mostly reads, with the exception of experience-database growth. This asymmetry is expected and not a problem.

---

## Alternatives Considered

**Alternative 1: Continue with coupled pipelines (status quo from 0.0).**  
Rejected. The problems described in the Context section are not incidental — they follow directly from the coupling. Decoupling at this point is cheaper than continuing to pay the integration tax on every change.

**Alternative 2: Couple via an explicit orchestrator.**  
A central orchestrator container would call the matching pipeline, then the CV pipeline, sequencing them. Rejected because the two pipelines operate on fundamentally different time horizons: matching runs daily (batch), CV generation runs on demand (consultant-initiated, possibly weeks after matching). Forcing them under one orchestrator would create artificial sequencing that does not reflect the actual usage pattern.

**Alternative 3: Full event-driven decoupling via a message bus.**  
The Matching Pipeline would emit `position-selected` events; the CV Pipeline would subscribe. Rejected for MVP 1.0 as overengineering. The handoff happens at most a few times per day per consultant; a message bus adds operational complexity (delivery guarantees, ordering, replay) without solving a problem we have. **Worth revisiting** if the system later needs to support multiple downstream consumers of `position-selected` events (analytics, multiple CV generation strategies, etc.).
