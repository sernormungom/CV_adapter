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
  expired_at      ISO-8601 str | null    — set when status transitions to "expired"
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
    # full names
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
    # abbreviations (Swedish + overlapping English)
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

_DATE_LABEL_RE = re.compile(
    r"(?:ansökningstiden\s+löper\s+ut|sista\s+ansökningsdag|ansökan\s+senast"
    r"|sista\s+dag\s+att\s+ansöka|deadline|close\s+date|closing\s+date"
    r"|application\s+deadline|last\s+day\s+to\s+apply|apply\s+by"
    r"|job\s+posting\s+end\s+date)"
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

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_TRAILING_TIME_RE = re.compile(r"\s+\d{1,2}:\d{2}.*$")
_TRAILING_PAREN_RE = re.compile(r"\s*\(.*?\)\s*$")


def _parse_close_date(raw: str) -> Optional[date]:
    """
    Parse a stored close_date value into a date object.
    Handles:
      - YYYY-MM-DD
      - DD mon YYYY or DD month YYYY (Swedish full names, Swedish/English abbreviations)
      - Board strings like "16 jun 2026 23:59 (1 dag kvar)" — trailing time and
        parenthetical text are stripped before parsing.
    Returns None for empty or unparseable input.
    """
    if not raw:
        return None
    s = _TRAILING_PAREN_RE.sub("", raw).strip()
    s = _TRAILING_TIME_RE.sub("", s).strip()

    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})$", s)
    if m:
        d_val, month_name, y_val = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _SWEDISH_MONTHS.get(month_name)
        if mo is None:
            try:
                mo = datetime.strptime(month_name, "%B").month
            except ValueError:
                return None
        try:
            return date(y_val, mo, d_val)
        except ValueError:
            return None

    return None


def mark_expired_positions(
    job_store_dir: Path,
    days_before_close: int = 7,
    max_age_days: int = 30,
) -> Dict[str, int]:
    """
    Mark active jobs as expired when either condition is true (OR, not fallback):
    - collected_at is parseable and older than max_age_days, regardless of close_date
    - close_date is parseable and within days_before_close days (or already past)
    If neither collected_at nor close_date is usable the job is left unchanged.
    Also backfills expired_at on already-expired jobs that lack it (not counted).
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

        status = job.get("status")

        # Backfill: stamp expired_at on legacy expired jobs that never got one
        if status == "expired" and not job.get("expired_at"):
            job["expired_at"] = _utc_now()
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            continue  # not a new expiry; do not increment count

        if status != "active":
            continue

        should_expire = False

        # Age cap: always checked — expire regardless of close_date
        collected_str = job.get("collected_at") or ""
        if collected_str:
            try:
                ca = datetime.fromisoformat(collected_str).date()
                if ca <= cutoff_age:
                    should_expire = True
            except ValueError:
                pass

        # Close-date window: independently checked — expire if closing soon
        cd = _parse_close_date(job.get("close_date") or "")
        if cd is not None and cd <= cutoff_close:
            should_expire = True

        if should_expire:
            job["status"] = "expired"
            if not job.get("expired_at"):
                job["expired_at"] = _utc_now()
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            count += 1

    return {"marked_expired": count}


def purge_expired_positions(
    job_store_dir: Path,
    grace_days: int = 7,
) -> Dict[str, int]:
    """
    Hard-delete expired jobs whose expired_at is older than grace_days.
    Skips: active jobs, corrupt JSON, missing/invalid expired_at.
    Does not touch verdict/application-tracker files.
    Returns {"purged": N}.
    """
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=grace_days)
    count = 0

    for path in sorted(job_store_dir.glob("job_*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if job.get("status") != "expired":
            continue

        expired_at_str = job.get("expired_at") or ""
        if not expired_at_str:
            continue

        try:
            expired_at = datetime.fromisoformat(expired_at_str)
        except (ValueError, TypeError):
            continue

        if expired_at.tzinfo is None:
            continue

        if expired_at <= threshold:
            path.unlink()
            count += 1

    return {"purged": count}


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
