"""
Applies or archives matching configs.

The Learning Module never auto-applies. It writes matching_config.pending.yaml; the user
confirms via the dashboard, which calls apply_pending_config to promote pending -> active
(archiving the previously active config first).

apply_pending_config(consultant_id) -> dict   # the now-active config
archive_config(consultant_id)       -> Path | None  # archive path of the old active config
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

# Fields the Learning Module attaches to the pending file but that are not part of the
# active matching_config schema — stripped before promotion.
_PENDING_ONLY_FIELDS = ("pending_reason", "held_reason", "diff", "rationale")


def _consultant_dir(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id


def _active_path(consultant_id: str) -> Path:
    return _consultant_dir(consultant_id) / "matching_config.yaml"


def _pending_path(consultant_id: str) -> Path:
    return _consultant_dir(consultant_id) / "matching_config.pending.yaml"


def _archive_dir(consultant_id: str) -> Path:
    return _consultant_dir(consultant_id) / "config_archive"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8",
    )


def archive_config(consultant_id: str) -> Optional[Path]:
    """
    Copy the currently active matching_config.yaml into config_archive/ with a timestamp.
    Returns the archive path, or None if there is no active config to archive.
    """
    active = _active_path(consultant_id)
    if not active.exists():
        return None
    archive = _archive_dir(consultant_id) / f"matching_config.{_now_stamp()}.yaml"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(active.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info("Archived matching_config for %s -> %s", consultant_id, archive.name)
    return archive


def apply_pending_config(consultant_id: str) -> Dict[str, Any]:
    """
    Promote matching_config.pending.yaml to the active matching_config.yaml.
    Archives the previously active config, strips pending-only fields, and removes
    the pending file. Returns the now-active config dict.
    Raises FileNotFoundError if no pending config exists.
    """
    pending_path = _pending_path(consultant_id)
    if not pending_path.exists():
        raise FileNotFoundError(f"No pending config for consultant {consultant_id}")

    pending = yaml.safe_load(pending_path.read_text(encoding="utf-8")) or {}
    if not isinstance(pending, dict):
        raise ValueError("Pending config is not a YAML mapping")

    # Archive the outgoing active config before overwriting it.
    archive_config(consultant_id)

    # Strip the Learning Module's bookkeeping fields to leave a clean active config.
    active_cfg = {k: v for k, v in pending.items() if k not in _PENDING_ONLY_FIELDS}
    metadata = active_cfg.get("metadata") or {}
    metadata["updated_at"] = _now_iso()
    active_cfg["metadata"] = metadata

    _write_yaml(_active_path(consultant_id), active_cfg)
    pending_path.unlink()
    logger.info("Applied pending matching_config for %s", consultant_id)
    return active_cfg
