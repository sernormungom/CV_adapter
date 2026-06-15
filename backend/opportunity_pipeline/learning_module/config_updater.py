"""
Orchestrates the propose-config-update flow for the Learning Module.

propose_config_update(consultant_id, cycle_id):
  1. aggregate verdict history
  2. load current matching_config.yaml (bootstrap if absent)
  3. build the LLM prompt
  4. call the LLM (Opus — the quality step) for a full proposed config + rationale
  5. validate the proposed config against the schema
  6. run the delta guard
  7. write matching_config.pending.yaml (with pending_reason/held_reason/diff/rationale)
  8. return {status, diff, rationale}

This is a synchronous entry point (the dashboard route calls it synchronously); the async
LLM call runs inside via asyncio.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
import yaml

from backend.config import DATA_DIR
from backend.profile_reader import load_profile, profile_to_yaml
from ..pre_filter_matcher.config_reader import load_config
from .config_bootstrapper import bootstrap_config, validate_config
from .config_update_prompt import build_system_prompt, build_user_message
from .delta_guard import check_delta
from .history_aggregator import aggregate_history, format_history_for_prompt

logger = logging.getLogger(__name__)

_LAST_N_CYCLES = 5


def _pending_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.pending.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner)
    return raw


def compute_diff(current: Dict[str, Any], proposed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively compare current vs proposed and return a nested dict of changes.
    Leaf changes are rendered as {"from": old, "to": new}. Keys with no change are omitted.
    """
    diff: Dict[str, Any] = {}
    keys = set(current.keys()) | set(proposed.keys())
    for key in keys:
        cur_v = current.get(key, "__absent__")
        new_v = proposed.get(key, "__absent__")
        if isinstance(cur_v, dict) and isinstance(new_v, dict):
            nested = compute_diff(cur_v, new_v)
            if nested:
                diff[key] = nested
        elif cur_v != new_v:
            diff[key] = {
                "from": None if cur_v == "__absent__" else cur_v,
                "to": None if new_v == "__absent__" else new_v,
            }
    return diff


async def _call_llm(profile_summary: str, current_config: dict, history_text: str) -> Dict[str, Any]:
    system_prompt = build_system_prompt()
    user_message = build_user_message(profile_summary, current_config, history_text)

    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = _strip_fences(message.content[0].text)
    logger.info("Config update raw response (first 400 chars): %s", raw[:400])
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Config update LLM returned non-mapping YAML ({type(data).__name__})")
    return data


def propose_config_update(consultant_id: str, cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Run the full propose flow and write matching_config.pending.yaml.
    Returns {status: "proposed"|"held", diff, rationale}.
    """
    history = aggregate_history(consultant_id, last_n_cycles=_LAST_N_CYCLES)
    history_text = format_history_for_prompt(history)

    current = load_config(consultant_id)
    if current is None:
        logger.info("No matching_config for %s — bootstrapping before update", consultant_id)
        current = bootstrap_config(consultant_id)

    try:
        profile_summary = profile_to_yaml(load_profile(consultant_id))
    except FileNotFoundError:
        profile_summary = "(profile not found)"

    llm_result = asyncio.run(_call_llm(profile_summary, current, history_text))
    proposed = llm_result.get("proposed_config")
    rationale = (llm_result.get("rationale") or "").strip()
    if not isinstance(proposed, dict):
        raise ValueError("LLM response missing a valid 'proposed_config' mapping")

    # Normalise mandatory metadata on the proposed config.
    proposed["consultant_id"] = consultant_id
    proposed.setdefault("schema_version", current.get("schema_version", "1.0"))
    metadata = proposed.get("metadata") or {}
    metadata["cycle_id"] = cycle_id
    metadata["source"] = "learning_module"
    metadata.setdefault("created_at", (current.get("metadata") or {}).get("created_at") or _now_iso())
    metadata["updated_at"] = _now_iso()
    proposed["metadata"] = metadata

    issues = validate_config(proposed)
    if issues:
        logger.error("Proposed config validation failed: %s", issues)
        raise ValueError(f"Proposed config invalid: {issues}")

    held, held_reasons = check_delta(current, proposed)
    diff = compute_diff(current, proposed)

    pending = dict(proposed)
    pending["pending_reason"] = "held" if held else "proposed"
    pending["held_reason"] = "; ".join(held_reasons) if held else None
    pending["diff"] = diff
    pending["rationale"] = rationale

    path = _pending_path(consultant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(pending, allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8",
    )

    status = "held" if held else "proposed"
    logger.info("Config update proposed for %s (status=%s, %d top-level changes)",
                consultant_id, status, len(diff))
    return {"status": status, "diff": diff, "rationale": rationale,
            "held_reason": pending["held_reason"]}
