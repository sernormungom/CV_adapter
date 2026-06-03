"""
Source Collector routes.

POST /collect
  Reads sources.yaml, collects from all enabled sources, writes new
  positions to the Job Store, returns a manifest summary.
  Runs synchronously — FastAPI executes sync endpoints in a thread pool.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

from backend.config import JOB_STORE_DIR, TA_CONFIG_DIR
from .board_connector import collect_from_sources
from .position_writer import write_positions

router = APIRouter()

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
_SOURCES_FILE = TA_CONFIG_DIR / "sources.yaml"


def _load_sources() -> list:
    if not _SOURCES_FILE.exists():
        return []
    data = yaml.safe_load(_SOURCES_FILE.read_text(encoding="utf-8")) or {}
    return data.get("sources", [])


@router.post("/collect")
def collect_jobs() -> dict:
    """
    Trigger job collection from all enabled sources.
    Returns: {new, duplicate, error, total_processed, items}
    """
    try:
        sources = _load_sources()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read sources.yaml: {exc}")

    if not sources:
        return {"new": 0, "duplicate": 0, "error": 0, "total_processed": 0, "items": [],
                "message": "No sources configured in sources.yaml"}

    raw_items = collect_from_sources(sources, _PROJECT_ROOT)
    manifest = write_positions(raw_items, JOB_STORE_DIR)
    return manifest
