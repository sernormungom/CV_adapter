# ADR-002: Pre-selection Gate Before LLM Standardization

**Status:** Accepted  
**Date:** 2026-05-26

---

## Context

The Opportunity Matching Pipeline ingests job positions from configured sources daily. The raw volume is high — many positions per source per day, most of which are not relevant to any given consultant. Two facts about MVP 1.0 shape this decision:

1. **LLM operations have non-trivial cost.** Each LLM call consumes tokens, which translates directly to monetary cost and latency. Standardization (rewriting a raw job ad into a clean, structured description for the Dashboard) is a per-position LLM call. Running it on every collected position would be wasteful — most positions will never be shown to the consultant.

2. **The Dashboard shows the consultant a small, curated set.** Per the MVP 1.0 design, the consultant reviews exactly 10 positions per cycle. The system does not need high-quality descriptions for positions the consultant will never see.

In MVP 0.0, standardization was code-based and ran on every position. This produced poor-quality descriptions (the originating problem) but had no cost concern. Moving standardization to the LLM solves the quality problem but introduces a cost problem that must be addressed architecturally, not just by hoping volumes stay low.

---

## Decision

The Matching Pipeline includes a **pre-selection gate**: a code-based scoring step that ranks all collected positions against the consultant's profile and selects the **top 10**. Only these 10 positions are passed to the LLM Standardizer. The remaining positions are retained in the Job Store but are not standardized and not shown on the Dashboard for this cycle.

The pre-selection gate uses the same matcher that produces the consultant-facing matching scores, configured by the same matching configuration that the Learning Module maintains (see ADR-003 and ADR-004). It is not a separate cheap-and-cheerful scorer.

LLM standardization runs on the selected 10 positions in a **single batched call** where possible, producing structured descriptions for the Dashboard.

The selection size — 10 — is a product decision (the consultant's review capacity per cycle), not a technical constraint. The architecture treats it as a configurable parameter.

---

## Consequences

**Positive:**

- LLM cost scales with consultant count and cycle frequency, not with source volume. Adding a new high-volume job board does not increase token spend.
- LLM quality budget is concentrated where it matters: the descriptions the consultant actually reads.
- The pipeline has a clear, defensible flow: *collect → filter → standardize → present*. Each stage has one job.

**Negative / tradeoffs:**

- **The pre-filter is now the silent kingmaker.** If the code-based matcher ranks a relevant position outside the top 10, that position is never seen by the consultant for this cycle and never receives LLM standardization. The system's apparent quality is bounded by the pre-filter's quality.
- This risk is partially mitigated by the **exploration budget** (see ADR-004): 1–2 of the 10 slots are reserved for positions selected by criteria other than pure score, providing a check against pre-filter blind spots.
- The risk is also mitigated by the fact that the pre-filter and the consumer-facing matcher are the **same component** configured by the same Learning Module. Improvements to matching quality automatically improve pre-filter quality. They do not drift apart.
- **The consultant never sees positions ranked 11+, even if some are good.** This is accepted. The product premise is that the consultant's time is the scarce resource, not job opportunities.

**Neutral observations:**

- Single-LLM-call batched standardization is treated as the default. If LLM providers change pricing such that per-position calls become cheaper (e.g., aggressive prompt caching), the implementation can switch without changing the architecture. The ADR commits to *one standardization step*, not to a specific batching strategy.

---

## Alternatives Considered

**Alternative 1: Standardize all positions with the LLM.**  
Rejected. Cost scales with source volume, which is unbounded. The system would discourage adding new sources, which is the opposite of what we want — broad source coverage is a feature.

**Alternative 2: LLM does the ranking as well as the standardization (top 15 → LLM ranks → top 10 + descriptions).**  
Considered seriously. Rejected for MVP 1.0 on two grounds. First, the pre-filter already uses the full consultant profile (experience, studies, interests), so its ranking should be reasonable. Second, adding the LLM to the ranking step introduces a second LLM failure mode (inconsistent ranking across runs, hallucinated reasoning) without clear evidence the pre-filter's ranking is the bottleneck.

**This alternative is explicitly deferred, not killed.** It will be revisited after approximately two months of operation. The trigger to reconsider is empirical: if consultants systematically reject bottom-ranked positions and accept positions surfaced only by the exploration budget, that is evidence the pre-filter's ranking — not just its selection — is weak, and LLM re-ranking becomes worth its cost.

**Alternative 3: Separate cheap pre-filter from the consumer-facing matcher.**  
A simpler scorer (e.g., keyword overlap) for pre-filtering, with the full matcher running only on survivors. Rejected because it creates two matching codepaths that can drift apart, doubles the maintenance burden, and means improvements to the Learning Module only help one of them. Using the same matcher for both purposes keeps the system coherent.
