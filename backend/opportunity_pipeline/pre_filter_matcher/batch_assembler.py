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

from typing import Any, Dict, List, Tuple

from backend.config import JOB_STORE_DIR
from backend.profile_reader import load_profile
from ..verdict_store import VerdictStoreError, load_verdicts
from ..source_collector.position_writer import job_identity_key, list_active_positions, load_position, save_position
from .config_reader import load_config
from .scoring_engine import score_job
from .exploration_selector import select_exploration_slots


BATCH_SIZE = 10


def run_matching(consultant_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Score all active jobs for consultant_id.
    Returns (selected_batch, stats).
    selected_batch: list of job dicts with match_score and score_breakdown set.
    stats: {total_active, scored, selected, config_source}
    """
    try:
        profile = load_profile(consultant_id)
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e

    config = load_config(consultant_id)
    config_source = "existing"
    if config is None:
        # Bootstrap: import lazily to avoid circular dependency
        from ..learning_module.config_bootstrapper import bootstrap_config
        config = bootstrap_config(consultant_id)
        config_source = "bootstrapped"

    all_active_jobs = list_active_positions(JOB_STORE_DIR)
    total_active = len(all_active_jobs)

    # Exclude jobs already reviewed by this consultant
    reviewed_ids: set = set()
    reviewed_identity_keys: set = set()
    try:
        verdicts = load_verdicts(consultant_id)
    except VerdictStoreError as exc:
        raise ValueError(str(exc)) from exc
    reviewed_ids = set(verdicts.keys())
    reviewed_identity_keys = {
        str(v.get("identity_key") or "")
        for v in verdicts.values()
        if isinstance(v, dict) and v.get("identity_key")
    }
    for jid in reviewed_ids:
        job = load_position(jid, JOB_STORE_DIR)
        if job:
            reviewed_identity_keys.add(job_identity_key(job))
    active_jobs = [
        j for j in all_active_jobs
        if j["job_id"] not in reviewed_ids and job_identity_key(j) not in reviewed_identity_keys
    ]

    if not active_jobs:
        return [], {
            "total_active": total_active,
            "reviewed_excluded": len(reviewed_ids),
            "scored": 0,
            "selected": 0,
            "config_source": config_source,
            "all_reviewed": True,
        }

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

    # Fill exploration slots (also excludes reviewed jobs from diversity pool)
    exploration = select_exploration_slots(scored, top_core, exploration_slots, config, reviewed_ids)

    # Combine and mark as selected
    batch = top_core + exploration
    selected_ids = {j["job_id"] for j in batch}

    # Clear previous batch selection flags across ALL active jobs (including reviewed ones
    # that may still have batch_selected=true from a previous cycle).
    scored_map = {j["job_id"]: j for j in scored}
    for job in all_active_jobs:
        was_selected = job["job_id"] in selected_ids
        # Use the in-memory scored copy if available (it has updated scores)
        job = scored_map.get(job["job_id"], job)
        if job.get("batch_selected") != was_selected:
            job["batch_selected"] = was_selected
            job["batch_selected_at"] = None if not was_selected else job.get("batch_selected_at")
            save_position(job, JOB_STORE_DIR)

    return batch, {
        "total_active": total_active,
        "reviewed_excluded": len(reviewed_ids),
        "scored": len(scored),
        "selected": len(batch),
        "config_source": config_source,
    }
