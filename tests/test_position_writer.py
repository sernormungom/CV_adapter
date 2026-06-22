"""
Tests for mark_expired_positions and purge_expired_positions.
All tests use tmp_path so the real job store is never touched.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.opportunity_pipeline.source_collector.position_writer import (
    mark_expired_positions,
    purge_expired_positions,
)

TODAY = datetime.now(timezone.utc).date()

# Swedish month abbreviation map (mirrors _SWEDISH_MONTHS in position_writer)
_ABBREV = {
    1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "maj", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec",
}


def _write_job(job_store: Path, job_id: str, **fields) -> Path:
    """Write a minimal job JSON file; returns the file path."""
    job = {
        "job_id": job_id,
        "status": fields.pop("status", "active"),
        "close_date": fields.pop("close_date", None),
        "collected_at": fields.pop(
            "collected_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        "expired_at": fields.pop("expired_at", None),
        **fields,
    }
    path = job_store / f"{job_id}.json"
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# mark_expired_positions
# ---------------------------------------------------------------------------

def test_iso_close_date_within_window_expires(tmp_path):
    close = (TODAY + timedelta(days=3)).isoformat()
    _write_job(tmp_path, "job_iso", close_date=close)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 1
    job = json.loads((tmp_path / "job_iso.json").read_text())
    assert job["status"] == "expired"
    assert job["expired_at"] is not None


def test_board_style_close_date_expires(tmp_path):
    # Build a board-style string for today → always within the 7-day window
    board_str = f"{TODAY.day} {_ABBREV[TODAY.month]} {TODAY.year} 23:59 (1 dag kvar)"
    _write_job(tmp_path, "job_board", close_date=board_str)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 1
    job = json.loads((tmp_path / "job_board.json").read_text())
    assert job["status"] == "expired"
    assert job["expired_at"] is not None


def test_plan_example_board_date_expires(tmp_path):
    # The exact example from the spec: "16 jun 2026 23:59 (1 dag kvar)"
    # June 16 2026 is in the past; it is always <= today + 7 once past.
    _write_job(tmp_path, "job_example", close_date="16 jun 2026 23:59 (1 dag kvar)")

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 1
    job = json.loads((tmp_path / "job_example.json").read_text())
    assert job["status"] == "expired"


def test_missing_close_date_old_job_expires(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_old", close_date=None, collected_at=old)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 1
    job = json.loads((tmp_path / "job_old.json").read_text())
    assert job["status"] == "expired"
    assert job["expired_at"] is not None


def test_missing_close_date_recent_job_stays_active(tmp_path):
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_recent", close_date=None, collected_at=recent)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 0
    job = json.loads((tmp_path / "job_recent.json").read_text())
    assert job["status"] == "active"


def test_unparseable_close_date_old_job_expires_via_collected_at(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_baddate", close_date="not-a-date", collected_at=old)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 1


def test_far_future_close_date_old_collected_at_expires(tmp_path):
    # Age cap fires even when close_date is far in the future.
    far_future = (TODAY + timedelta(days=365)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_agecap", close_date=far_future, collected_at=old)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 1
    job = json.loads((tmp_path / "job_agecap.json").read_text())
    assert job["status"] == "expired"
    assert job["expired_at"] is not None


def test_far_future_close_date_recent_collected_at_stays_active(tmp_path):
    # Neither cap fires: close_date far out, collected_at fresh.
    far_future = (TODAY + timedelta(days=365)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_fresh", close_date=far_future, collected_at=recent)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 0
    job = json.loads((tmp_path / "job_fresh.json").read_text())
    assert job["status"] == "active"


def test_already_expired_without_expired_at_is_stamped_not_counted(tmp_path):
    _write_job(tmp_path, "job_legacy", status="expired", expired_at=None)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 0  # backfill, not a new expiry
    job = json.loads((tmp_path / "job_legacy.json").read_text())
    assert job["status"] == "expired"
    assert job["expired_at"] is not None


def test_already_expired_with_expired_at_not_re_stamped(tmp_path):
    original_ts = "2026-01-01T00:00:00+00:00"
    _write_job(tmp_path, "job_already", status="expired", expired_at=original_ts)

    mark_expired_positions(tmp_path)

    job = json.loads((tmp_path / "job_already.json").read_text())
    assert job["expired_at"] == original_ts


def test_active_job_with_close_date_beyond_window_stays_active(tmp_path):
    future = (TODAY + timedelta(days=30)).isoformat()
    _write_job(tmp_path, "job_future", close_date=future)

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 0
    assert json.loads((tmp_path / "job_future.json").read_text())["status"] == "active"


def test_no_close_date_no_collected_at_stays_active(tmp_path):
    # Both close_date and collected_at are absent → no basis to expire; leave unchanged.
    _write_job(tmp_path, "job_nodate", close_date=None, collected_at="")

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 0
    job = json.loads((tmp_path / "job_nodate.json").read_text())
    assert job["status"] == "active"


def test_corrupt_json_is_skipped(tmp_path):
    (tmp_path / "job_corrupt.json").write_text("{bad json", encoding="utf-8")

    result = mark_expired_positions(tmp_path)

    assert result["marked_expired"] == 0


# ---------------------------------------------------------------------------
# purge_expired_positions
# ---------------------------------------------------------------------------

def test_expired_beyond_grace_is_purged(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_purge", status="expired", expired_at=old_ts)

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 1
    assert not (tmp_path / "job_purge.json").exists()


def test_expired_within_grace_is_retained(tmp_path):
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_grace", status="expired", expired_at=recent_ts)

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 0
    assert (tmp_path / "job_grace.json").exists()


def test_expired_with_invalid_expired_at_is_retained(tmp_path):
    _write_job(tmp_path, "job_invalid", status="expired", expired_at="not-a-datetime")

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 0
    assert (tmp_path / "job_invalid.json").exists()


def test_expired_with_missing_expired_at_is_retained(tmp_path):
    _write_job(tmp_path, "job_noexpiry", status="expired", expired_at=None)

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 0
    assert (tmp_path / "job_noexpiry.json").exists()


def test_active_job_is_never_purged(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    _write_job(tmp_path, "job_active", status="active", expired_at=old_ts)

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 0
    assert (tmp_path / "job_active.json").exists()


def test_purge_corrupt_json_is_skipped(tmp_path):
    (tmp_path / "job_corrupt.json").write_text("{bad", encoding="utf-8")

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 0


def test_purge_counts_only_deleted_files(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")

    _write_job(tmp_path, "job_del1", status="expired", expired_at=old_ts)
    _write_job(tmp_path, "job_del2", status="expired", expired_at=old_ts)
    _write_job(tmp_path, "job_keep", status="expired", expired_at=recent_ts)
    _write_job(tmp_path, "job_active2", status="active")

    result = purge_expired_positions(tmp_path, grace_days=7)

    assert result["purged"] == 2
    assert not (tmp_path / "job_del1.json").exists()
    assert not (tmp_path / "job_del2.json").exists()
    assert (tmp_path / "job_keep.json").exists()
    assert (tmp_path / "job_active2.json").exists()
