"""
Writes raw collected job items to the Job Store as JSON files.
Deduplicates by job_id. Returns a manifest summary.

Job Store schema (one file per job at JOB_STORE_DIR/{job_id}.json):
  job_id          str
  status          "active" | "expired"
  source_id       str
  source_type     str
  source_url      str
  source_native_id str
  identity_key    str      — canonical identity for dedupe/review exclusion
  title_guess     str
  company_hint    str
  raw_text        str
  collected_at    ISO-8601 str
  close_date      YYYY-MM-DD str | null  — application deadline; null if unknown
  match_score     float | null     — set by Pre-Filter & Matcher
  score_breakdown dict | null      — set by Pre-Filter & Matcher
  batch_selected  bool             — true if in current top-10 batch
  batch_selected_at  str | null
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .board_connector import build_identity_key, extract_source_native_id


# ---------------------------------------------------------------------------
# Close-date extraction
# ---------------------------------------------------------------------------

_SWEDISH_MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}

_DATE_LABEL_RE = re.compile(
    r"(?:ansökningstiden\s+löper\s+ut|sista\s+ansökningsdag|ansökan\s+senast"
    r"|sista\s+dag\s+att\s+ansöka|deadline|close\s+date|closing\s+date"
    r"|application\s+deadline|last\s+day\s+to\s+apply|apply\s+by)"
    r"\s*[:\-–]?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})",
    re.IGNORECASE,
)


def _parse_date_str(raw: str) -> Optional[str]:
    raw = raw.strip()
    # YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw
    # DD/MM/YYYY or DD.MM.YYYY
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    # DD Month YYYY (Swedish or English)
    m = re.fullmatch(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if m:
        d, month_name, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _SWEDISH_MONTHS.get(month_name)
        if mo is None:
            try:
                mo = datetime.strptime(month_name, "%B").month
            except ValueError:
                return None
        try:
            return date(int(y), mo, d).isoformat()
        except ValueError:
            return None
    return None


def _extract_close_date(raw_text: str) -> Optional[str]:
    """Return first parseable deadline date found in raw_text, or None."""
    for m in _DATE_LABEL_RE.finditer(raw_text):
        parsed = _parse_date_str(m.group(1))
        if parsed:
            return parsed
    return None


# ---------------------------------------------------------------------------
# Expiry / cleanup
# ---------------------------------------------------------------------------

def mark_expired_positions(
    job_store_dir: Path,
    days_before_close: int = 7,
    max_age_days: int = 30,
) -> Dict[str, int]:
    """
    Mark active jobs as expired when:
    - close_date is set and is within days_before_close days (or already past)
    - close_date is null and collected_at is older than max_age_days days
    Returns {"marked_expired": N}. Never deletes records.
    """
    today = datetime.now(timezone.utc).date()
    cutoff_close = today + timedelta(days=days_before_close)
    cutoff_age = today - timedelta(days=max_age_days)
    count = 0

    for path in sorted(job_store_dir.glob("job_*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("status") != "active":
            continue

        should_expire = False
        close_date_str = job.get("close_date") or ""
        if close_date_str:
            try:
                cd = date.fromisoformat(close_date_str[:10])
                if cd <= cutoff_close:
                    should_expire = True
            except ValueError:
                pass
        else:
            collected_str = job.get("collected_at") or ""
            if collected_str:
                try:
                    ca = datetime.fromisoformat(collected_str).date()
                    if ca <= cutoff_age:
                        should_expire = True
                except ValueError:
                    pass

        if should_expire:
            job["status"] = "expired"
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            count += 1

    return {"marked_expired": count}


# ---------------------------------------------------------------------------

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


def job_identity_key(job: Dict[str, Any]) -> str:
    existing = job.get("identity_key") or ""
    if existing:
        return existing
    source_native_id = job.get("source_native_id") or extract_source_native_id(
        job.get("raw_text") or "",
        job.get("source_url") or "",
    )
    return build_identity_key(
        job.get("raw_text") or "",
        job.get("source_url") or "",
        source_native_id,
        job.get("source_type") or "",
        job.get("source_id") or "",
    )


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
    existing_by_identity = {}
    for existing_job in list_all_positions(job_store_dir):
        key = job_identity_key(existing_job)
        if key:
            existing_by_identity.setdefault(key, existing_job.get("job_id"))

    for item in raw_items:
        job_id = item.get("job_id") or ""
        identity_key = item.get("identity_key") or ""
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

        duplicate_job_id = existing_by_identity.get(identity_key)
        if duplicate_job_id and duplicate_job_id != job_id:
            duplicate_count += 1
            manifest_items.append({
                "job_id": job_id,
                "existing_job_id": duplicate_job_id,
                "status": "duplicate",
                "source_id": item.get("source_id", ""),
                "title_guess": item.get("title_guess", ""),
            })
            continue

        raw_text = item.get("raw_text") or ""
        job = {
            "job_id": job_id,
            "status": "active",
            "source_id": item.get("source_id", ""),
            "source_type": item.get("source_type", ""),
            "source_url": item.get("source_url") or "",
            "source_native_id": item.get("source_native_id") or "",
            "identity_key": identity_key,
            "title_guess": item.get("title_guess") or "",
            "company_hint": item.get("company_hint") or "",
            "raw_text": raw_text,
            "collected_at": item.get("collected_at") or "",
            "close_date": item.get("close_date") or _extract_close_date(raw_text),
            "match_score": None,
            "score_breakdown": None,
            "batch_selected": False,
            "batch_selected_at": None,
        }
        save_position(job, job_store_dir)
        if identity_key:
            existing_by_identity[identity_key] = job_id
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
