"""
Builds the LLM prompt for proposing a matching_config update.

The LLM is given: the matching-config schema, a profile summary, the current config,
and the recent verdict history. It returns the FULL proposed matching_config.yaml plus
a separate `rationale` block explaining what changed and why. We compute the diff
programmatically (current vs proposed), so the model does not need to emit a delta.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / "project_context" / "matching-config.schema.md"


_SYSTEM_TEMPLATE = """\
You are the Learning Module for a consultant job-matching platform. Your job is to refine
a consultant's matching configuration based on the verdicts they gave to recently matched jobs.

=== MATCHING CONFIG SCHEMA (your output MUST conform to this) ===
{schema}
=== END SCHEMA ===

HOW THE CONFIG IS USED:
- scoring_weights blend five fit dimensions into one score (they must sum to 1.0).
- thresholds.keep / thresholds.maybe turn the score into a recommendation.
- term_catalogs tier terms (high_signal / standard / low_signal) to weight evidence matches.
- role_archetypes, growth_signals, interest_signals, blockers, location_scores shape fit.

YOUR TASK:
Study the verdict history. Where the consultant said "no" or "maybe" to jobs the system
scored highly, find the systematic signal in their reasons and adjust the config so the
matcher better reflects their true preferences. Where they said "yes" to lower-scored jobs,
strengthen the signals that should have ranked those higher.

CHANGE DISCIPLINE (important — large jumps will be held for manual review):
- Move scoring_weights gently; never shift a single dimension by more than ~0.25 in one cycle.
- Keep scoring_weights summing to 1.0 (±0.01).
- Do not remove more than ~4 terms from any single catalog tier in one cycle.
- Move thresholds.keep / thresholds.maybe by at most ~8 points in one cycle.
- Preserve thresholds.keep > thresholds.maybe and exploration.slots in {{1, 2}}.
- Only change what the evidence justifies. If no change is warranted, return the current
  config unchanged with a rationale saying so.

OUTPUT FORMAT — return ONLY raw YAML (no markdown fences, no preamble), a single mapping:

proposed_config:
  # the COMPLETE matching_config.yaml you propose (full document, every required key present)
  schema_version: '1.0'
  consultant_id: ...
  # ...all sections...
rationale: |
  A concise plain-language explanation of what you changed and why, referencing the
  verdict signals that motivated each change. If nothing changed, say so.
"""


def build_system_prompt() -> str:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    return _SYSTEM_TEMPLATE.format(schema=schema)


def build_user_message(profile_summary: str, current_config: dict, history_text: str) -> str:
    current_yaml = yaml.dump(current_config, allow_unicode=True, sort_keys=False, width=110)
    return (
        "=== CONSULTANT PROFILE SUMMARY ===\n"
        f"{profile_summary}\n\n"
        "=== CURRENT matching_config.yaml ===\n"
        f"{current_yaml}\n"
        "=== RECENT VERDICT HISTORY ===\n"
        f"{history_text}\n\n"
        "Propose the refined matching_config.yaml. Return only the YAML with "
        "`proposed_config` and `rationale` keys, as specified."
    )
