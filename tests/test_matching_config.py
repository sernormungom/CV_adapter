"""Tests for shared/matching_config.py — the matching-config sanity check used
by config_reader.py. Stdlib-only, kept in shared/ so a future local tool could
reuse it without importing backend."""

from __future__ import annotations

import copy

from shared.matching_config import validate_config

_VALID = {
    "schema_version": "1.0",
    "consultant_id": "alice",
    "scoring_weights": {
        "expertise_fit": 0.30,
        "role_fit": 0.30,
        "growth_fit": 0.20,
        "interest_fit": 0.15,
        "practical_fit": 0.05,
    },
    "thresholds": {"keep": 76, "maybe": 58},
    "term_catalogs": {},
    "role_archetypes": [],
    "growth_signals": {},
    "interest_signals": {},
    "blockers": {},
    "location_scores": {},
}


def test_valid_config_passes():
    assert validate_config(_VALID) is True


def test_missing_required_key_fails():
    cfg = copy.deepcopy(_VALID)
    del cfg["blockers"]
    assert validate_config(cfg) is False


def test_weights_not_summing_to_one_fails():
    cfg = copy.deepcopy(_VALID)
    cfg["scoring_weights"]["practical_fit"] = 0.50
    assert validate_config(cfg) is False


def test_keep_not_greater_than_maybe_fails():
    cfg = copy.deepcopy(_VALID)
    cfg["thresholds"] = {"keep": 50, "maybe": 58}
    assert validate_config(cfg) is False


def test_non_int_thresholds_fail():
    cfg = copy.deepcopy(_VALID)
    cfg["thresholds"] = {"keep": 76.0, "maybe": 58}
    assert validate_config(cfg) is False
