"""
Matching-config validation — pure, no I/O.

Used by the cloud backend (``backend/matching/config_reader.py``, which owns
loading/backup/writing the YAML file). Kept in ``shared/`` so a future local
tool could reuse the same validation without importing the backend.
"""

from __future__ import annotations

from typing import Any, Dict

REQUIRED_KEYS = {
    "schema_version", "consultant_id", "scoring_weights", "thresholds",
    "term_catalogs", "role_archetypes", "growth_signals", "interest_signals",
    "blockers", "location_scores",
}


def validate_config(cfg: Dict[str, Any]) -> bool:
    """Sanity-check a matching_config.yaml dict: required keys present,
    scoring weights sum to ~1.0, and threshold ordering holds."""
    if not REQUIRED_KEYS.issubset(cfg.keys()):
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
