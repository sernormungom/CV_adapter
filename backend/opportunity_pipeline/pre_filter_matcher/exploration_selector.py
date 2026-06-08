"""
Exploration selector — reserves 1–2 slots in the final batch for diversity.

Picks candidates from outside the top-8 (scored positions ranked 9–20) that
represent a different role archetype from what already dominates the top scores.
Falls back to random sampling if no archetype diversity is available.
"""

from __future__ import annotations

import random
import re
import unicodedata
from typing import Any, Dict, List


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _detect_archetype(job: Dict[str, Any], archetypes: List[Dict[str, Any]]) -> str:
    text_folded = _fold((job.get("raw_text") or "") + " " + (job.get("title_guess") or ""))
    best_name = "Unknown"
    best_hits = 0
    for arch in archetypes:
        patterns = [_fold(p) for p in (arch.get("title_patterns") or []) + (arch.get("body_patterns") or [])]
        hits = sum(1 for p in patterns if re.search(r"\b" + re.escape(p) + r"\b", text_folded))
        if hits > best_hits:
            best_hits = hits
            best_name = arch.get("name") or "Unknown"
    return best_name


def select_exploration_slots(
    scored_jobs: List[Dict[str, Any]],
    top_jobs: List[Dict[str, Any]],
    num_slots: int,
    config: Dict[str, Any],
    reviewed_ids: "set[str] | None" = None,
) -> List[Dict[str, Any]]:
    """
    scored_jobs: ALL scored jobs sorted by overall_score descending.
    top_jobs: the already-selected top-N jobs (before exploration).
    num_slots: number of exploration slots to fill (1 or 2).
    config: matching_config dict (used for role_archetypes).
    reviewed_ids: job_ids already judged by the consultant — excluded from candidates.

    Returns a list of exploration candidates (length <= num_slots).
    """
    archetypes = config.get("role_archetypes") or []
    top_ids = {j["job_id"] for j in top_jobs}
    excluded = top_ids | (reviewed_ids or set())
    top_archetypes = {_detect_archetype(j, archetypes) for j in top_jobs}

    # Candidates: positions ranked 9–20 (outside the core top selections)
    candidates = [j for j in scored_jobs if j["job_id"] not in excluded][:20]

    if not candidates:
        return []

    # Prefer candidates with a different archetype from the top selections
    diverse = [c for c in candidates if _detect_archetype(c, archetypes) not in top_archetypes]
    pool = diverse if diverse else candidates

    # Shuffle to add randomness, then take num_slots
    shuffled = list(pool)
    random.shuffle(shuffled)
    return shuffled[:num_slots]
