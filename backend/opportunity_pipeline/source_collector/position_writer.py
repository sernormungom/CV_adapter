"""
Writes raw collected job items to the Job Store as JSON files.
Deduplicates by job_id. Returns a manifest summary.

Job Store schema (one file per job at JOB_STORE_DIR/{job_id}.json):
  job_id          str
  status          "active" | "expired"
  source_id       str
  source_type     str
  source_url      str
  title_guess     str
  company_hint    str
  raw_text        str
  collected_at    ISO-8601 str
  match_score     float | null     — set by Pre-Filter & Matcher
  score_breakdown dict | null      — set by Pre-Filter & Matcher
  batch_selected  bool             — true if in current top-10 batch
  batch_selected_at  str | null
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _job_path(job_id: str, job_store_dir: Path) -> Path:
    return job_store_dir / f"{job_id}.json"


def load_position(job_id: str, job_store_dir: Path) -> Optional[Dict[str, Any]]:
    path = _job_path(job_id, job_store_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_position(job: Dict[str, Any], job_store_dir: Path) -> None:
    job_store_dir.mkdir(parents=True, exist_ok=True)
    path = _job_path(job["job_id"], job_store_dir)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def list_all_positions(job_store_dir: Path) -> List[Dict[str, Any]]:
    jobs = []
    for path in sorted(job_store_dir.glob("job_*.json")):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return jobs


def list_active_positions(job_store_dir: Path) -> List[Dict[str, Any]]:
    return [j for j in list_all_positions(job_store_dir) if j.get("status") == "active"]


def write_positions(
    raw_items: List[Dict[str, Any]],
    job_store_dir: Path,
) -> Dict[str, Any]:
    """
    Accept raw items from board_connector.collect_from_sources().
    Deduplicate against existing Job Store entries.
    Write new positions as JSON with status=active.
    Return manifest: {new, duplicate, error, items}.
    """
    job_store_dir.mkdir(parents=True, exist_ok=True)
    new_count = duplicate_count = error_count = 0
    manifest_items = []

    for item in raw_items:
        job_id = item.get("job_id") or ""
        error = item.get("error") or ""

        if error or not job_id:
            error_count += 1
            manifest_items.append({
                "job_id": job_id,
                "status": "error",
                "source_id": item.get("source_id", ""),
                "title_guess": item.get("title_guess", ""),
                "error": error or "empty job_id",
            })
            continue

        if _job_path(job_id, job_store_dir).exists():
            duplicate_count += 1
            manifest_items.append({
                "job_id": job_id,
                "status": "duplicate",
                "source_id": item.get("source_id", ""),
                "title_guess": item.get("title_guess", ""),
            })
            continue

        job = {
            "job_id": job_id,
            "status": "active",
            "source_id": item.get("source_id", ""),
            "source_type": item.get("source_type", ""),
            "source_url": item.get("source_url") or "",
            "title_guess": item.get("title_guess") or "",
            "company_hint": item.get("company_hint") or "",
            "raw_text": item.get("raw_text") or "",
            "collected_at": item.get("collected_at") or "",
            "match_score": None,
            "score_breakdown": None,
            "batch_selected": False,
            "batch_selected_at": None,
        }
        save_position(job, job_store_dir)
        new_count += 1
        manifest_items.append({
            "job_id": job_id,
            "status": "new",
            "source_id": item.get("source_id", ""),
            "title_guess": item.get("title_guess", ""),
        })

    return {
        "new": new_count,
        "duplicate": duplicate_count,
        "error": error_count,
        "total_processed": len(raw_items),
        "items": manifest_items,
    }
