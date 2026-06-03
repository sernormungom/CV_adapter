"""
Learning Module routes.

GET /pending-config?consultant_id=<id>
  Returns the current pending config proposal for review by the dashboard:
    {status: "proposed"|"held"|"none", diff, rationale, held_reason}
  Confirm/discard are handled in dashboard/routes.py via config_writer functions.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter

from backend.config import DATA_DIR

router = APIRouter()


def _pending_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.pending.yaml"


@router.get("/pending-config")
def get_pending_config(consultant_id: str) -> dict:
    """Return the pending config proposal, or status 'none' if there isn't one."""
    path = _pending_path(consultant_id)
    if not path.exists():
        return {"status": "none", "diff": {}, "rationale": "", "held_reason": None}

    try:
        pending = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"status": "none", "diff": {}, "rationale": "", "held_reason": None}

    pending_reason = pending.get("pending_reason") or "proposed"
    return {
        "status": pending_reason,  # "proposed" | "held"
        "diff": pending.get("diff") or {},
        "rationale": pending.get("rationale") or "",
        "held_reason": pending.get("held_reason"),
    }
