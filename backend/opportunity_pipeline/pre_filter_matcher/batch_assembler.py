"""
Batch assembler — scores all active jobs, selects top batch, writes scores back.

Steps:
  1. Load all active jobs from Job Store.
  2. Load consultant profile.md and matching_config.yaml.
  3. If no config exists, trigger bootstrap (LLM generates initial config).
  4. Score every active job via scoring_engine.
  5. Select top (10 - exploration_slots) by overall_score.
  6. Fill remaining slots with exploration candidates.
  7. Write match_score + score_breakdown + batch_selected back to each job's JSON.
  8. Return list of selected job dicts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from backend.config import DATA_DIR, JOB_STORE_DIR
from ..source_collector.position_writer import list_active_positions, save_position
from .config_reader import load_config
from .scoring_engine import score_job
from .exploration_selector import select_exploration_slots


BATCH_SIZE = 10


def _load_profile(consultant_id: str) -> Optional[Dict[str, Any]]:
    profile_path = DATA_DIR / consultant_id / "profile.md"
    if not profile_path.exists():
        return None
    md = profile_path.read_text(encoding="utf-8")
    m = re.search(r"```yaml\s*(.*?)```", md, re.S)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None


def run_matching(consultant_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Score all active jobs for consultant_id.
    Returns (selected_batch, stats).
    selected_batch: list of job dicts with match_score and score_breakdown set.
    stats: {total_active, scored, selected, config_source}
    """
    profile = _load_profile(consultant_id)
    if not profile:
        raise ValueError(f"Profile not found for consultant: {consultant_id}")

    config = load_config(consultant_id)
    config_source = "existing"
    if config is None:
        # Bootstrap: import lazily to avoid circular dependency
        from ..learning_module.config_bootstrapper import bootstrap_config
        config = bootstrap_config(consultant_id)
        config_source = "bootstrapped"

    active_jobs = list_active_positions(JOB_STORE_DIR)
    if not active_jobs:
        return [], {"total_active": 0, "scored": 0, "selected": 0, "config_source": config_source}

    # Score all jobs
    scored = []
    for job in active_jobs:
        result = score_job(job, profile, config)
        job["match_score"] = result["overall_score"]
        job["score_breakdown"] = result["score_breakdown"]
        job["recommended_status"] = result["recommended_status"]
        job["hard_blockers"] = result["hard_blockers"]
        job["soft_risks"] = result["soft_risks"]
        job["job_terms_found"] = result["job_terms_found"]
        save_position(job, JOB_STORE_DIR)
        scored.append(job)

    # Sort descending by overall_score
    scored.sort(key=lambda j: j.get("match_score") or 0.0, reverse=True)

    # Select top core slots
    exploration_slots = int((config.get("exploration") or {}).get("slots") or 1)
    core_count = max(1, BATCH_SIZE - exploration_slots)
    top_core = scored[:core_count]

    # Fill exploration slots
    exploration = select_exploration_slots(scored, top_core, exploration_slots, config)

    # Combine and mark as selected
    batch = top_core + exploration
    selected_ids = {j["job_id"] for j in batch}

    # Clear previous batch selection flags
    for job in scored:
        was_selected = job["job_id"] in selected_ids
        if job.get("batch_selected") != was_selected:
            job["batch_selected"] = was_selected
            job["batch_selected_at"] = None if not was_selected else job.get("batch_selected_at")
            save_position(job, JOB_STORE_DIR)

    return batch, {
        "total_active": len(active_jobs),
        "scored": len(scored),
        "selected": len(batch),
        "config_source": config_source,
    }
