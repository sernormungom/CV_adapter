"""
Dashboard routes.

GET  /api/dashboard/{consultant_id}/positions
     Returns current batch of top-N jobs with raw text + scores.

POST /api/dashboard/{consultant_id}/verdict
     Body: {job_id, verdict: "yes"|"no"|"maybe", reason: str}
     Saves verdict to application tracker. Reason required for no/maybe.

POST /api/dashboard/{consultant_id}/complete-cycle
     Validates all batch jobs have verdicts, writes them, triggers
     the Learning Module to propose a config update.
     Returns {status, pending_config_diff} for the Dashboard to display.

POST /api/dashboard/{consultant_id}/confirm-config
     Applies the pending matching_config.pending.yaml to matching_config.yaml.

POST /api/dashboard/{consultant_id}/discard-config
     Discards matching_config.pending.yaml.

GET  /api/dashboard/{consultant_id}/status
     Returns {total_active_jobs, batch_size, verdicts_given, cycle_complete,
              pending_config_update}.

POST /api/dashboard/{consultant_id}/run-cycle
     Orchestrates: collect -> run-matching in sequence.
     Returns combined manifest + matching stats.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config import DATA_DIR, JOB_STORE_DIR, TA_CONFIG_DIR
from ..verdict_store import VerdictStoreError, load_verdicts, save_verdicts
from ..source_collector.board_connector import collect_from_sources
from ..source_collector.position_writer import (
    job_identity_key,
    list_active_positions,
    load_position,
    mark_expired_positions,
    purge_expired_positions,
    save_position,
    write_positions,
)
from ..pre_filter_matcher.batch_assembler import run_matching

router = APIRouter()

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pending_config_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.pending.yaml"


def _load_verdicts(consultant_id: str) -> Dict[str, Any]:
    try:
        return load_verdicts(consultant_id)
    except VerdictStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _save_verdicts(consultant_id: str, data: Dict[str, Any]) -> None:
    try:
        save_verdicts(consultant_id, data)
    except VerdictStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _verdict_for_job(job: Dict[str, Any], verdicts: Dict[str, Any]) -> Dict[str, Any]:
    direct = verdicts.get(job.get("job_id") or "")
    if isinstance(direct, dict):
        return direct
    identity_key = job_identity_key(job)
    if not identity_key:
        return {}
    for verdict_job_id, entry in verdicts.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("identity_key") == identity_key:
            return entry
        reviewed_job = load_position(str(verdict_job_id), JOB_STORE_DIR)
        if reviewed_job and job_identity_key(reviewed_job) == identity_key:
            return entry
    return {}


def _load_sources() -> list:
    sources_file = TA_CONFIG_DIR / "sources.yaml"
    if not sources_file.exists():
        return []
    data = yaml.safe_load(sources_file.read_text(encoding="utf-8")) or {}
    return data.get("sources", [])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{consultant_id}/positions")
def get_positions(consultant_id: str) -> dict:
    """
    Return the current batch-selected jobs with raw text + scores.
    """
    batch = [j for j in list_active_positions(JOB_STORE_DIR) if j.get("batch_selected")]
    if not batch:
        batch = sorted(list_active_positions(JOB_STORE_DIR),
                       key=lambda j: j.get("match_score") or 0.0, reverse=True)[:10]

    verdicts = _load_verdicts(consultant_id)

    positions = []
    for job in batch:
        jid = job["job_id"]
        verdict_entry = _verdict_for_job(job, verdicts)
        positions.append({
            "job_id": jid,
            "title_guess": job.get("title_guess") or "",
            "source_id": job.get("source_id") or "",
            "source_url": job.get("source_url") or "",
            "source_native_id": job.get("source_native_id") or "",
            "identity_key": job_identity_key(job),
            "company_hint": job.get("company_hint") or "",
            "collected_at": job.get("collected_at") or "",
            "raw_text": job.get("raw_text") or "",
            "match_score": job.get("match_score"),
            "score_breakdown": job.get("score_breakdown"),
            "recommended_status": job.get("recommended_status"),
            "hard_blockers": job.get("hard_blockers") or [],
            "soft_risks": job.get("soft_risks") or [],
            "verdict": verdict_entry.get("verdict"),
            "reason": verdict_entry.get("reason") or "",
        })

    return {"positions": positions, "total": len(positions)}


class VerdictRequest(BaseModel):
    job_id: str
    verdict: str       # "yes" | "no" | "maybe"
    reason: str = ""


@router.post("/{consultant_id}/verdict")
def submit_verdict(consultant_id: str, body: VerdictRequest) -> dict:
    """Save a single verdict. Reason is required for no/maybe."""
    verdict = body.verdict.lower()
    if verdict not in {"yes", "no", "maybe"}:
        raise HTTPException(status_code=400, detail="verdict must be yes, no, or maybe")
    if verdict in {"no", "maybe"} and not body.reason.strip():
        raise HTTPException(status_code=400, detail=f"Reason is required for verdict '{verdict}'")

    job = load_position(body.job_id, JOB_STORE_DIR)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {body.job_id} not found in Job Store")

    verdicts = _load_verdicts(consultant_id)
    verdicts[body.job_id] = {
        "job_id": body.job_id,
        "verdict": verdict,
        "reason": body.reason.strip(),
        "title_guess": job.get("title_guess") or "",
        "source_id": job.get("source_id") or "",
        "source_url": job.get("source_url") or "",
        "source_native_id": job.get("source_native_id") or "",
        "identity_key": job_identity_key(job),
        "match_score": job.get("match_score"),
        "score_breakdown": job.get("score_breakdown"),
        "recommended_status": job.get("recommended_status"),
        "hard_blockers": job.get("hard_blockers") or [],
        "soft_risks": job.get("soft_risks") or [],
        "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cycle_id": None,
    }
    _save_verdicts(consultant_id, verdicts)
    return {"status": "saved", "job_id": body.job_id, "verdict": verdict}


@router.post("/{consultant_id}/complete-cycle")
def complete_cycle(consultant_id: str) -> dict:
    """
    Validates all batch jobs have verdicts, then calls the Learning Module
    to propose a config update.
    Returns {status, cycle_id, config_proposal} for preview.
    """
    batch = [j for j in list_active_positions(JOB_STORE_DIR) if j.get("batch_selected")]
    verdicts = _load_verdicts(consultant_id)

    missing = [j["job_id"] for j in batch if not _verdict_for_job(j, verdicts)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing verdicts for {len(missing)} jobs: {missing[:3]}{'...' if len(missing) > 3 else ''}",
        )

    no_maybe_missing_reason = [
        jid for jid, v in verdicts.items()
        if v.get("verdict") in {"no", "maybe"} and not v.get("reason")
    ]
    if no_maybe_missing_reason:
        raise HTTPException(
            status_code=400,
            detail=f"Reason missing for {len(no_maybe_missing_reason)} no/maybe verdicts",
        )

    # Tag all batch verdicts with the cycle timestamp
    cycle_id = datetime.now(timezone.utc).strftime("cycle_%Y%m%dT%H%M%S")
    for jid in (j["job_id"] for j in batch):
        if jid in verdicts:
            verdicts[jid]["cycle_id"] = cycle_id
    _save_verdicts(consultant_id, verdicts)

    # Trigger Learning Module to propose config update
    try:
        from ..learning_module.config_updater import propose_config_update
        proposal = propose_config_update(consultant_id, cycle_id)
    except Exception as exc:
        return {
            "status": "verdicts_saved",
            "cycle_id": cycle_id,
            "config_proposal": None,
            "warning": f"Learning Module failed: {exc}",
        }

    return {
        "status": "cycle_complete",
        "cycle_id": cycle_id,
        "config_proposal": proposal,
    }


@router.post("/{consultant_id}/confirm-config")
def confirm_config(consultant_id: str) -> dict:
    """Apply pending config update."""
    from ..learning_module.config_writer import apply_pending_config
    try:
        apply_pending_config(consultant_id)
        return {"status": "applied"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No pending config update found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{consultant_id}/discard-config")
def discard_config(consultant_id: str) -> dict:
    """Discard pending config update."""
    pending = _pending_config_path(consultant_id)
    if pending.exists():
        pending.unlink()
    return {"status": "discarded"}


@router.get("/{consultant_id}/status")
def get_status(consultant_id: str) -> dict:
    """Return current pipeline status for the consultant."""
    active_jobs = list_active_positions(JOB_STORE_DIR)
    batch = [j for j in active_jobs if j.get("batch_selected")]
    verdicts = _load_verdicts(consultant_id)

    verdicts_given = sum(1 for j in batch if _verdict_for_job(j, verdicts))

    return {
        "total_active_jobs": len(active_jobs),
        "batch_size": len(batch),
        "verdicts_given": verdicts_given,
        "verdicts_remaining": len(batch) - verdicts_given,
        "cycle_complete": len(batch) > 0 and verdicts_given >= len(batch),
        "pending_config_update": _pending_config_path(consultant_id).exists(),
    }


@router.post("/{consultant_id}/run-cycle")
def run_cycle(consultant_id: str) -> dict:
    """
    Orchestrate: collect jobs from all sources, then score/select top batch.
    Returns combined manifest + matching stats.
    """
    # Step 1: Collect
    sources = _load_sources()
    raw_items = collect_from_sources(sources, _PROJECT_ROOT) if sources else []
    manifest = write_positions(raw_items, JOB_STORE_DIR)

    # Step 1b: Expire stale / closing-soon positions, then purge old expired ones
    expiry = mark_expired_positions(JOB_STORE_DIR)
    purge = purge_expired_positions(JOB_STORE_DIR)

    # Step 2: Match
    try:
        batch, match_stats = run_matching(consultant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Matching failed: {exc}")

    error_details = [
        {"source_id": it.get("source_id", ""), "error": it.get("error", "")}
        for it in manifest.get("items", [])
        if it.get("status") == "error" and it.get("error")
    ]
    return {
        "collection": {
            "new": manifest["new"],
            "duplicate": manifest["duplicate"],
            "error": manifest["error"],
            "expired": expiry["marked_expired"],
            "purged": purge["purged"],
            "error_details": error_details,
        },
        "matching": match_stats,
        "batch_size": len(batch),
    }
