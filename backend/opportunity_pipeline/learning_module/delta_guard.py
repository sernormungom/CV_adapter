"""
Delta guard: checks that a proposed matching_config does not change too much in one cycle.

If any limit is exceeded the config is held for manual review:
- any single scoring_weights dimension shifts by more than ±0.30
- more than 5 terms removed from any single term_catalogs tier
- thresholds.keep or thresholds.maybe shifts by more than ±10

check_delta(current, proposed) -> (held: bool, reasons: list[str])
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

WEIGHT_SHIFT_LIMIT = 0.30
TERMS_REMOVED_LIMIT = 5
THRESHOLD_SHIFT_LIMIT = 10

_WEIGHT_DIMENSIONS = (
    "expertise_fit", "role_fit", "growth_fit", "interest_fit", "practical_fit",
)


def _check_weights(current: Dict[str, Any], proposed: Dict[str, Any], reasons: List[str]) -> None:
    cur = current.get("scoring_weights") or {}
    new = proposed.get("scoring_weights") or {}
    for dim in _WEIGHT_DIMENSIONS:
        c = cur.get(dim)
        n = new.get(dim)
        if not isinstance(c, (int, float)) or not isinstance(n, (int, float)):
            continue
        shift = abs(float(n) - float(c))
        if shift > WEIGHT_SHIFT_LIMIT + 1e-9:
            reasons.append(
                f"scoring_weights.{dim} shifted by {shift:.2f} "
                f"(> {WEIGHT_SHIFT_LIMIT:.2f} limit): {c} -> {n}"
            )


def _check_term_removals(current: Dict[str, Any], proposed: Dict[str, Any], reasons: List[str]) -> None:
    cur_catalogs = current.get("term_catalogs") or {}
    new_catalogs = proposed.get("term_catalogs") or {}
    for catalog, cur_tiers in cur_catalogs.items():
        if not isinstance(cur_tiers, dict):
            continue
        new_tiers = new_catalogs.get(catalog) or {}
        for tier, cur_terms in cur_tiers.items():
            if not isinstance(cur_terms, list):
                continue
            new_terms = new_tiers.get(tier) or []
            new_set = {str(t).strip().lower() for t in new_terms}
            removed = [t for t in cur_terms if str(t).strip().lower() not in new_set]
            if len(removed) > TERMS_REMOVED_LIMIT:
                reasons.append(
                    f"term_catalogs.{catalog}.{tier} removed {len(removed)} terms "
                    f"(> {TERMS_REMOVED_LIMIT} limit)"
                )


def _check_thresholds(current: Dict[str, Any], proposed: Dict[str, Any], reasons: List[str]) -> None:
    cur = current.get("thresholds") or {}
    new = proposed.get("thresholds") or {}
    for key in ("keep", "maybe"):
        c = cur.get(key)
        n = new.get(key)
        if not isinstance(c, (int, float)) or not isinstance(n, (int, float)):
            continue
        shift = abs(int(n) - int(c))
        if shift > THRESHOLD_SHIFT_LIMIT:
            reasons.append(
                f"thresholds.{key} shifted by {shift} (> {THRESHOLD_SHIFT_LIMIT} limit): {c} -> {n}"
            )


def check_delta(current: Dict[str, Any], proposed: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Return (held, reasons). held is True if any safety bound is exceeded."""
    reasons: List[str] = []
    _check_weights(current, proposed, reasons)
    _check_term_removals(current, proposed, reasons)
    _check_thresholds(current, proposed, reasons)
    return (len(reasons) > 0, reasons)
