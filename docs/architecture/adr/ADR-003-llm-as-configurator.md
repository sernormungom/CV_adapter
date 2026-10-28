# ADR-003: LLM Configures the Matcher; Code Performs the Matching

**Status:** Accepted  
**Date:** 2026-05-26

---

## Context

The Opportunity Matching Pipeline must decide, for each candidate position, how well it matches a given consultant's profile. MVP 0.0 implemented this as pure code: hard-coded terms, hard-coded weights, no learning. The result was rigid — adapting the matcher to a new consultant, a new domain, or a shift in the consultant's interests required a code change.

The obvious alternative is to have the LLM perform the matching directly: feed it the position and the profile, ask for a score and a rationale. This is flexible but introduces three problems:

1. **Cost** — LLM matching scales with position volume, recreating the cost problem ADR-002 was designed to avoid.
2. **Determinism** — the same position scored twice may receive different scores. This makes ranking unstable and makes the Learning Module's signal noisy: did the score change because the consultant's preferences shifted, or because the LLM was inconsistent?
3. **Auditability** — when a consultant asks "why was this position ranked third?", a code-based matcher can answer with concrete arithmetic. An LLM-based matcher answers with a post-hoc rationalisation that may or may not reflect what actually drove the score.

What we need is the **flexibility of LLM reasoning** combined with the **determinism, cost profile, and auditability of code**.

---

## Decision

The matching task is split into two roles:

- **The LLM is a configurator.** It produces a **matching configuration**: a structured artifact containing terms (skills, technologies, role types, domain keywords), weights, thresholds, and grouping rules. The LLM does this by reading the consultant's profile and, when available, the history of consultant decisions and reasoning (see ADR-004 for how this history is collected).

- **The Matcher is a deterministic code component.** It reads the matching configuration and applies it to candidate positions to produce scores. Given the same configuration and the same positions, the Matcher produces the same scores. Always.

The contract between the LLM (producer) and the Matcher (consumer) is governed by a **matching configuration schema**, stored as a versioned artifact in the repository. The LLM is prompted to emit only configurations that conform to this schema. The Matcher validates configurations against the schema at read time and rejects malformed input rather than attempting to interpret it.

The matching configuration lives in the Profile & Configuration Store, scoped to the consultant.

---

## Consequences

**Positive:**

- **Flexibility where it's valuable, determinism where it matters.** The LLM's strength — synthesising across unstructured input (a profile, a stack of free-text decision reasons) into a structured representation — is used exactly once per update cycle. The matching itself, which runs on every position, is fast, cheap, deterministic, and inspectable.
- **Auditability.** A consultant or Talent Advisor can ask "why was this ranked this way?" and receive a real answer based on concrete arithmetic against the current configuration.
- **Cost.** Matching itself does not consume tokens. The LLM is called once per configuration update, not once per position.
- **Stable ranking within a cycle.** If the same batch is re-matched (e.g., after a transient failure), the scores are identical. This makes the Dashboard reproducible and the Learning Module's feedback signal clean.

**Negative / tradeoffs:**

- **Schema is now a load-bearing artifact.** The matching configuration schema is the contract between an LLM (which can produce anything) and a Matcher (which can only handle what it's coded for). Schema changes require coordinated updates to the LLM prompt and the Matcher implementation. Schema evolution must be a deliberate, versioned activity — not a casual edit.
- **The LLM can produce a *valid* configuration that is *bad* for the consultant.** Schema validation catches malformed configurations, not unhelpful ones. The Learning Module's feedback loop (ADR-004) is what eventually corrects bad-but-valid configurations, but there is a window during which a poor configuration affects consultant experience. ADR-004's exploration budget partially mitigates this by ensuring some diversity survives even a poorly-tuned configuration.
- **Two-step debugging.** When a position is ranked unexpectedly, the question becomes: "is the configuration wrong, or is the matcher misapplying a correct configuration?" Two layers means two places to look. Acceptable, because each layer is independently inspectable — but worth naming.

**Neutral observations:**

- This decision is independent of which LLM is used. The system depends on the LLM being capable of emitting valid structured output against the schema. Any model meeting that bar is interchangeable. This is a useful property worth preserving as the LLM landscape evolves.
- The Matcher is the right place to put any future non-LLM matching logic (rules, hard filters like geography or seniority floor). Code-based logic belongs in the code-based component.

---

## Pattern Note

This decision instantiates a pattern worth naming: **LLM-as-Configurator**. The LLM operates at the level of *defining the rules*, not *applying them*. Application is deterministic code. This pattern is broadly useful whenever you want LLM flexibility for inputs that change slowly (preferences, taxonomies, business rules) combined with deterministic application for operations that run frequently. It is the inverse of the more common "LLM-in-the-loop" pattern, where the LLM is invoked on each operation. For high-volume, cost-sensitive, audit-sensitive systems, the configurator pattern is often the better tradeoff.

---

## Alternatives Considered

**Alternative 1: LLM performs the matching directly.**  
Rejected on cost, determinism, and auditability grounds (see Context). The flexibility benefit is real but is captured by the configurator pattern without paying these costs.

**Alternative 2: Pure code-based matching with hand-tuned configuration (MVP 0.0's approach).**  
Rejected. The whole point of MVP 1.0 is that the system adapts to the consultant. A static, hand-tuned configuration cannot do this. The Learning Module — which is the heart of the "adaptive system" premise — requires something that consumes consultant feedback and turns it into matching behaviour changes. The LLM is the natural fit for that role.

**Alternative 3: Hybrid scoring — code computes a base score, LLM adjusts it per-position.**  
Rejected. This reintroduces per-position LLM cost (recreating ADR-002's problem) and reintroduces nondeterminism into the score. It also blurs the line between "LLM as configurator" and "LLM as matcher," making the system harder to reason about and harder to debug. The pattern works because the boundary is clean; muddying it defeats the purpose.
