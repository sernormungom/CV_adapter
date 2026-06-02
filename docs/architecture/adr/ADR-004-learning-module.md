# ADR-004: System Learning Module — Closing the Feedback Loop

**Status:** Accepted  
**Date:** 2026-05-26

---

## Context

MVP 0.0 had no feedback loop. The matcher was configured once, statically. Consultant decisions — which positions they accepted, rejected, or flagged — were recorded for tracking purposes but never used to improve future matching. The system did not get better with use.

MVP 1.0 introduces the premise that **consultant decisions are signal, not just records**. Every verdict a consultant gives on a Dashboard position carries information about what they want and what they don't. Over time, this signal should make the matcher smarter — ranking better-fitting positions higher and filtering out patterns the consultant consistently rejects.

The question this ADR answers is: **how does consultant feedback become matching behaviour?**

Three constraints shape the design:

1. **The matcher is code-based and configuration-driven** (ADR-003). The only lever available is the matching configuration. Feedback must translate into configuration changes, not code changes.
2. **Feedback-loop collapse is a real risk.** If the system over-weights recent decisions, it can converge on a narrow model of what the consultant wants, stop surfacing diverse positions, and lose the ability to self-correct. This is the filter bubble problem applied to a job-matching context.
3. **Signal quality varies.** A bare "no" verdict carries weak signal. A "no" accompanied by free-text reasoning ("this role is too junior, I need a senior architect title") carries strong signal. The Learning Module must be designed around the richer input.

---

## Decision

A **System Learning Module** is introduced as a dedicated container in the Opportunity Matching Pipeline. It activates at the completion of each Dashboard Review cycle — when the consultant has given verdicts on all presented positions for that batch.

### Inputs

- All verdicts from the completed cycle: yes / no / maybe, each with mandatory reasoning for "no" and "maybe", optional reasoning for "yes"
- Historical verdicts from previous cycles (read from the Application Tracker)
- The consultant's current profile (read from the Profile & Configuration Store)
- The current matching configuration (read from the Profile & Configuration Store)

### Process

The module constructs a prompt to the LLM containing all of the above. The LLM is asked to produce an **updated matching configuration** that better reflects the consultant's demonstrated preferences — not just their stated profile. The LLM reasons across the full decision history, including free-text reasoning, to identify patterns: skills the consultant consistently rejects despite profile match, role types they accept outside their stated focus, domain preferences that have shifted.

The LLM emits a new matching configuration conforming to the schema defined in ADR-003. The Matcher adopts this configuration for the next batch cycle.

### Mandatory Safeguards

The following three safeguards are non-negotiable. Removing or bypassing any of them requires a new ADR explicitly accepting the associated risk.

**Safeguard 1 — Schema validation.**  
The Learning Module validates the LLM's output against the matching configuration schema before writing it to the store. Invalid output is rejected. The previous configuration remains active. An error is emitted to Observability and the System Administrator is notified. The batch cycle is not blocked — the next batch runs with the previous configuration.

**Safeguard 2 — Bounded delta.**  
The new configuration is compared to the previous one. Changes exceeding a defined threshold (e.g., weight shifts greater than ±0.3 per term, or removal of more than N terms in a single update) are flagged and held for review rather than applied automatically. This prevents a single anomalous cycle from destabilising the matcher. Threshold values are configurable parameters, not hardcoded constants. The review and approval process for held configurations must be defined before the first production cycle; responsibility falls to the Talent Advisor or System Administrator.

**Safeguard 3 — Exploration budget.**  
Of the 10 positions presented to the consultant each cycle, **1–2 slots are reserved for exploration positions** — positions selected by criteria other than the top matcher score (e.g., diversity of role type, recency of posting, or random sampling from the top 20). These positions are not subject to the pre-selection gate's top-10 ranking. The exploration budget serves two purposes: it provides signal about positions the current configuration would never surface (protecting against filter-bubble collapse), and it acts as an early-warning system for pre-filter quality — if the consultant consistently accepts exploration positions over matched positions, that is evidence the matcher's configuration is drifting from actual preferences. The exact size of the exploration budget (1 or 2 of 10) is a configurable product parameter.

### Mandatory Reasoning on Verdicts

The Dashboard enforces that a "no" or "maybe" verdict cannot be submitted without a free-text reason. A "yes" reason is optional — acceptance without explanation is still a meaningful positive signal. Making the reason mandatory for negative and uncertain verdicts is the minimum required to keep the feedback signal useful. A history of bare yes/no verdicts degrades the LLM's ability to distinguish between "rejected because overqualified" and "rejected because wrong domain" — two signals that should produce opposite configuration updates. Very short inputs ("wrong domain", "too junior") are accepted; the constraint is presence, not length.

---

## Consequences

**Positive:**

- The system improves with use. A consultant who has given 20 cycles of feedback will see a meaningfully better-calibrated Dashboard than a new consultant on their first cycle.
- Configuration updates are transparent and auditable. The matching configuration is a readable artifact; a consultant or Talent Advisor can inspect the current configuration and compare it to the previous version.
- The feedback signal is qualitatively richer than a rating system because it includes free-text reasoning, enabling the LLM to make distinctions a numeric score cannot express.
- The exploration budget creates a natural evaluation loop: the ratio of "exploration positions accepted" to "matched positions accepted" is a metric for matcher quality that requires no additional instrumentation — it falls out of the decision history already being recorded.

**Negative / tradeoffs:**

- **The Learning Module adds a mandatory dependency on the LLM at cycle completion.** If the LLM call fails, the configuration is not updated. The system degrades gracefully (previous configuration remains active), but repeated failures mean the system stops learning. Observability and alerting are required to detect this condition.
- **Mandatory reasoning creates friction for the consultant.** A consultant who wants to quickly dismiss several irrelevant positions must write something for each. This is a deliberate tradeoff — friction is the price of signal quality — but it should be acknowledged in UX design.
- **The bounded-delta safeguard introduces a review step when thresholds are exceeded.** The process for this review must be defined before the first production cycle.
- **Cold start.** A new consultant has no decision history. The Learning Module on the first cycle has only the consultant's profile to work with, equivalent to MVP 0.0's static configuration. Quality improves only after several cycles of data. This is expected and unavoidable; worth communicating to new users.

**Neutral observations:**

- The Learning Module is the only container that reads from the Application Tracker at runtime. All other containers write to it or ignore it. This asymmetry is correct — the Application Tracker is the system's memory, and the Learning Module is the only component whose job is to reason over that memory.
- The exploration budget size (1–2 of 10) is a product parameter, not an architectural constant. The architecture mandates that an exploration budget exists; the exact size is tunable.

---

## Alternatives Considered

**Alternative 1: Direct consultant configuration — let the consultant update matching terms themselves.**  
Rejected as the primary mechanism. This requires the consultant to have a mental model of how the matcher works and to express preferences in the matcher's vocabulary rather than natural language. Not ruled out as a supplementary power-user interface, but cannot replace the Learning Module.

**Alternative 2: Rule-based feedback processing — code reads verdicts and applies predefined update rules.**  
For example: "three consecutive 'no' verdicts for technology X → reduce X's weight by 0.1." Rejected because verdict patterns are richer than any finite set of rules can capture. The free-text reasoning in particular is irreducible to rules. The LLM is the right tool precisely because it can synthesise across unstructured text.

**Alternative 3: Continuous learning — update configuration after every single verdict.**  
Rejected. Single verdicts are noisy. Waiting for a full cycle produces a batch of correlated signal — all verdicts from the same presentation context — which is more statistically meaningful. Cycle-level updates also make configuration history tractable: one configuration version per cycle, not one per verdict.

**Alternative 4: No Learning Module — static configuration with manual updates.**  
Rejected. This is MVP 0.0's approach, and its failure is documented in the originating problem statement. Included here for completeness and to close the door explicitly.
