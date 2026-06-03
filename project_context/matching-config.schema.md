# Matching Configuration Schema

- **Schema version:** 1.0
- **Status:** Draft
- **Date:** 2026-06-02
- **Format:** YAML
- **Filename convention:** `matching_config.yaml` inside the consultant's profile folder
- **Produced by:** Learning Module (after each verdict cycle); bootstrapped by LLM on first run

---

## Purpose

The matching configuration is the consultant-specific, LLM-updatable ruleset that governs how the Pre-Filter & Matcher scores job positions. It encodes the consultant's priorities, target role archetypes, growth signals, preferred work shape, and location preferences. It starts as a first-version generated from the consultant's profile and is refined by the Learning Module after each verdict cycle.

---

## Schema

```yaml
schema_version: '1.0'
consultant_id: string                    # must match folder name

metadata:
  cycle_id: string | null                # null on bootstrap; cycle ID that produced this config
  created_at: ISO-8601 timestamp
  updated_at: ISO-8601 timestamp
  source: bootstrap | learning_module    # how this version was produced

scoring_weights:
  # Five dimensions. Must sum to 1.0 (±0.01 tolerance).
  expertise_fit: float   # 0.0–1.0 — How well job terms match consultant's evidence
  role_fit:      float   # 0.0–1.0 — How well job role aligns with target archetypes
  growth_fit:    float   # 0.0–1.0 — How much the job offers growth toward stated goals
  interest_fit:  float   # 0.0–1.0 — How well work shape matches preferences
  practical_fit: float   # 0.0–1.0 — Location, work mode, contract type

thresholds:
  keep:  int    # overall_score >= keep  → recommended_status: keep
  maybe: int    # overall_score >= maybe → recommended_status: maybe
  # below maybe → recommended_status: reject

exploration:
  slots: int    # number of exploration slots in final batch (1 or 2 out of 10)
  # Exploration slots are filled from top-20 candidates outside the top-8,
  # prioritising different role_archetype from the top selections.

term_catalogs:
  # Terms extracted from raw job text and matched against consultant's evidence blocks.
  # Each term has a weight multiplier applied to its evidence match score.
  languages:    # programming languages
    high_signal:    list[string]   # weight 1.45 — core differentiators
    standard:       list[string]   # weight 1.00
    low_signal:     list[string]   # weight 0.35 — too generic to differentiate
  tools:        # dev tools, frameworks, platforms
    high_signal:    list[string]
    standard:       list[string]
    low_signal:     list[string]
  methods:      # processes, methodologies
    high_signal:    list[string]
    standard:       list[string]
    low_signal:     list[string]
  standards:    # compliance standards, certifications
    high_signal:    list[string]
    standard:       list[string]
  verification: # V&V methods
    high_signal:    list[string]
    standard:       list[string]
  domains:      # technical domains
    high_signal:    list[string]
    standard:       list[string]
    low_signal:     list[string]

role_archetypes:
  # Maps role archetype names to match signals found in job titles and descriptions.
  # Used in role_fit scoring.
  # Each archetype entry:
  - name: string                  # e.g. "Embedded SW Engineer"
    title_patterns: list[string]  # keywords matched in job title (case-insensitive)
    body_patterns:  list[string]  # keywords matched in job body
    seniority_bonus: float        # 0.0–0.20 — extra score if seniority matches consultant level

growth_signals:
  # Terms and patterns in job descriptions that indicate growth toward stated goals.
  # Matched against job text; each hit contributes to growth_fit score.
  primary:    list[string]   # weight 1.0 — direct primary growth goals
  secondary:  list[string]   # weight 0.5 — secondary growth themes
  penalize:   list[string]   # weight -0.3 — signals that pull away from growth goals

interest_signals:
  # Work shape signals — what the consultant enjoys doing.
  preferred:  list[string]   # weight +1.0 — adds to interest_fit
  avoid:      list[string]   # weight -0.8 — subtracts from interest_fit

blockers:
  # Hard and soft blockers evaluated regardless of other scores.
  hard:   list[string]   # matched text → recommended_status forced to reject
  soft:   list[string]   # matched text → risk_deduction applied

location_scores:
  # Maps location/work-mode combos to a practical_fit score (0–100).
  # Unmatched combinations get default score.
  rules:
    - match:  string   # text pattern matched against city/work_mode fields
      score:  int      # 0–100
  default_score: int   # fallback if no rule matches
```

---

## Constraints

- `scoring_weights` must sum to 1.0 (tolerance ±0.01).
- `thresholds.keep` must be > `thresholds.maybe`.
- `exploration.slots` must be 1 or 2.
- All lists may be empty but the keys must be present.
- `cycle_id` is null only for the bootstrap version.

---

## Delta Guard Limits (enforced by Learning Module)

The Learning Module will hold a proposed config for manual review if any of these conditions are met in a single cycle update:

- Any single dimension weight shifts by more than ±0.30
- More than 5 terms removed from any single catalog tier in one cycle
- `thresholds.keep` or `thresholds.maybe` shifts by more than ±10

Held configs are written to `matching_config.pending.yaml` and presented to the consultant for review before being applied.
