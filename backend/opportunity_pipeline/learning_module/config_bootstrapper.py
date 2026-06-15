"""
Bootstraps the FIRST matching_config.yaml for a consultant via an LLM.

Reads profile.md + the matching-config schema, asks the model (Sonnet — cheap, since this
is a first-pass generation rather than a nuanced refinement) to emit a complete config,
validates it against the schema constraints, and writes matching_config.yaml.

bootstrap_config(consultant_id) is a synchronous entry point (callers in the dashboard
are sync); the LLM call runs inside via asyncio.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import anthropic
import yaml

from ..pre_filter_matcher.config_reader import save_config
from backend.profile_reader import load_profile, profile_to_yaml

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / "project_context" / "matching-config.schema.md"

_REQUIRED_KEYS = {
    "schema_version", "consultant_id", "metadata", "scoring_weights", "thresholds",
    "exploration", "term_catalogs", "role_archetypes", "growth_signals",
    "interest_signals", "blockers", "location_scores",
}
_WEIGHT_DIMENSIONS = ("expertise_fit", "role_fit", "growth_fit", "interest_fit", "practical_fit")

_SYSTEM_TEMPLATE = """\
You generate the FIRST matching configuration for a consultant on a job-matching platform,
derived purely from their profile. This config governs how incoming jobs are scored.

=== MATCHING CONFIG SCHEMA (your output MUST conform exactly to this) ===
{schema}
=== END SCHEMA ===

RULES:
1. Return ONLY raw YAML — no markdown fences, no commentary.
2. Produce a COMPLETE document: every required key present, correct types.
3. scoring_weights must sum to 1.0 (±0.01). thresholds.keep must be > thresholds.maybe.
   exploration.slots must be 1 or 2. All lists may be empty but keys must exist.
4. metadata.cycle_id must be null. metadata.source must be "bootstrap".
5. Derive term_catalogs, role_archetypes, growth_signals, interest_signals, blockers and
   location_scores from the consultant's actual skills, history, goals and location.
   Put genuine differentiators in high_signal tiers and generic terms in low_signal.
6. Be sensible and conservative — this is a starting point the system will refine over time.
"""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner)
    return raw


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate a full matching_config against schema constraints. Returns issue strings."""
    issues: List[str] = []
    if not isinstance(cfg, dict):
        return [f"expected a YAML mapping, got {type(cfg).__name__}"]

    missing = _REQUIRED_KEYS - set(cfg.keys())
    if missing:
        issues.append(f"missing required keys: {sorted(missing)}")

    weights = cfg.get("scoring_weights") or {}
    if not isinstance(weights, dict):
        issues.append("scoring_weights must be a mapping")
    else:
        for dim in _WEIGHT_DIMENSIONS:
            if not isinstance(weights.get(dim), (int, float)):
                issues.append(f"scoring_weights.{dim} must be a number")
        numeric = [float(v) for v in weights.values() if isinstance(v, (int, float))]
        total = sum(numeric)
        if numeric and not (0.99 <= total <= 1.01):
            issues.append(f"scoring_weights must sum to 1.0 (got {total:.3f})")

    thresholds = cfg.get("thresholds") or {}
    keep, maybe = thresholds.get("keep"), thresholds.get("maybe")
    if not isinstance(keep, int) or not isinstance(maybe, int):
        issues.append("thresholds.keep and thresholds.maybe must be integers")
    elif keep <= maybe:
        issues.append(f"thresholds.keep ({keep}) must be > thresholds.maybe ({maybe})")

    slots = (cfg.get("exploration") or {}).get("slots")
    if slots not in (1, 2):
        issues.append(f"exploration.slots must be 1 or 2 (got {slots!r})")

    return issues


async def _generate_config(consultant_id: str, profile_yaml: str) -> Dict[str, Any]:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    system_prompt = _SYSTEM_TEMPLATE.format(schema=schema)
    user_message = (
        f"consultant_id: {consultant_id}\n\n"
        "=== CONSULTANT PROFILE (YAML) ===\n"
        f"{profile_yaml}\n\n"
        "Generate the first matching_config.yaml. Return only the YAML."
    )

    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = _strip_fences(message.content[0].text)
    logger.info("Bootstrap config raw response (first 400 chars): %s", raw[:400])
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Bootstrap LLM returned non-mapping YAML ({type(data).__name__})")
    return data


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def bootstrap_config(consultant_id: str) -> Dict[str, Any]:
    """
    Generate, validate and persist the first matching_config.yaml for the consultant.
    Returns the config dict. Raises ValueError on profile/validation failure.
    """
    try:
        profile_yaml = profile_to_yaml(load_profile(consultant_id))
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e

    cfg = asyncio.run(_generate_config(consultant_id, profile_yaml))

    # Normalise mandatory metadata regardless of what the model produced.
    cfg.setdefault("schema_version", "1.0")
    cfg["consultant_id"] = consultant_id
    metadata = cfg.get("metadata") or {}
    metadata["cycle_id"] = None
    metadata["source"] = "bootstrap"
    metadata.setdefault("created_at", _now_iso())
    metadata["updated_at"] = _now_iso()
    cfg["metadata"] = metadata

    issues = validate_config(cfg)
    if issues:
        logger.error("Bootstrap config validation failed: %s", issues)
        raise ValueError(f"Bootstrap config invalid: {issues}")

    save_config(consultant_id, cfg, make_backup=False)
    logger.info("Bootstrapped matching_config.yaml for %s", consultant_id)
    return cfg
