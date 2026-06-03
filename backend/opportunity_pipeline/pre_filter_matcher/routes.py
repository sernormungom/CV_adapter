"""
Pre-Filter & Matcher routes.

POST /run-matching?consultant_id=<id>
  Scores all active jobs for the consultant and selects the top batch.
  Returns {selected, stats}.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .batch_assembler import run_matching

router = APIRouter()


@router.post("/run-matching")
def run_matching_endpoint(consultant_id: str) -> dict:
    """
    Score all active jobs for consultant_id and select the top batch.
    Returns {selected: [...job summaries...], stats: {...}}.
    """
    try:
        batch, stats = run_matching(consultant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Matching failed: {exc}")

    summaries = [
        {
            "job_id": j["job_id"],
            "title_guess": j.get("title_guess", ""),
            "source_id": j.get("source_id", ""),
            "match_score": j.get("match_score"),
            "recommended_status": j.get("recommended_status"),
            "hard_blockers": j.get("hard_blockers") or [],
        }
        for j in batch
    ]
    return {"selected": summaries, "stats": stats}
