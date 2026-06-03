"""
Reads and validates matching_config.yaml for a consultant.
Falls back to the previous backup if the current file is malformed.
Returns None if no config exists (caller triggers bootstrap).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from backend.config import DATA_DIR


def _config_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.yaml"


def _backup_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.backup.yaml"


def _validate(cfg: Dict[str, Any]) -> bool:
    required_keys = {"schema_version", "consultant_id", "scoring_weights", "thresholds",
                     "term_catalogs", "role_archetypes", "growth_signals", "interest_signals",
                     "blockers", "location_scores"}
    if not required_keys.issubset(cfg.keys()):
        return False
    weights = cfg.get("scoring_weights") or {}
    total = sum(float(v) for v in weights.values() if isinstance(v, (int, float)))
    if not (0.98 <= total <= 1.02):
        return False
    thresholds = cfg.get("thresholds") or {}
    if not (isinstance(thresholds.get("keep"), int) and isinstance(thresholds.get("maybe"), int)):
        return False
    if thresholds["keep"] <= thresholds["maybe"]:
        return False
    return True


def load_config(consultant_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the matching config dict for consultant_id, or None if absent.
    Tries current → backup on parse/validation failure.
    """
    path = _config_path(consultant_id)
    backup = _backup_path(consultant_id)

    for candidate in (path, backup):
        if not candidate.exists():
            continue
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if _validate(data):
                return data
        except Exception:
            pass
    return None


def save_config(consultant_id: str, cfg: Dict[str, Any], make_backup: bool = True) -> None:
    path = _config_path(consultant_id)
    if make_backup and path.exists():
        shutil.copy2(path, _backup_path(consultant_id))
    path.write_text(
        yaml.dump(cfg, allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8",
    )
