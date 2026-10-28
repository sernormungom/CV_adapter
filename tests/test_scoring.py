"""Tests for shared/scoring.py — the deterministic scoring engine used by
backend/matching/service.py. Stdlib-only — must run with no backend imports."""

from __future__ import annotations

from shared.scoring import build_evidence_index, score_job

_PROFILE = {
    "career_history": {
        "role_groups": [
            {
                "blocks": [
                    {
                        "languages": ["Python", "C++"],
                        "tools": ["FastAPI", "Docker"],
                        "processes_standards": ["ISO 26262"],
                        "verification_validation": ["HIL testing"],
                        "domains": ["Automotive"],
                        "evidence_items": [{"text": "Led backend rewrite"}],
                    }
                ]
            }
        ]
    }
}

_CONFIG = {
    "scoring_weights": {
        "expertise_fit": 0.30,
        "role_fit": 0.30,
        "growth_fit": 0.20,
        "interest_fit": 0.15,
        "practical_fit": 0.05,
    },
    "thresholds": {"keep": 76, "maybe": 58},
    "term_catalogs": {
        "languages": {"high_signal": ["python"], "standard": [], "low_signal": []},
        "tools": {"high_signal": ["fastapi"], "standard": ["docker"], "low_signal": []},
        "domains": {"high_signal": ["automotive"], "standard": [], "low_signal": []},
    },
    "role_archetypes": [
        {
            "name": "Backend Engineer",
            "title_patterns": ["backend engineer"],
            "body_patterns": ["python"],
            "seniority_bonus": 0.1,
        }
    ],
    "growth_signals": {"primary": ["mentoring"], "secondary": [], "penalize": ["legacy maintenance only"]},
    "interest_signals": {"preferred": ["greenfield"], "avoid": ["on-call"]},
    "blockers": {"hard": ["security clearance required"], "soft": ["heavy travel"]},
    "location_scores": {"rules": [{"match": "remote", "score": 90}], "default_score": 60},
}

_JOB_TEXT = (
    "Backend Engineer\n\n"
    "We need a Python developer with FastAPI and Docker experience in the "
    "automotive domain. Remote work. Opportunities for mentoring juniors."
)


def _job(text=_JOB_TEXT):
    return {"raw_text": text}


def test_build_evidence_index_flattens_and_folds():
    index = build_evidence_index(_PROFILE)
    assert "python" in index["languages"]
    assert "fastapi" in index["tools"]
    assert "automotive" in index["domains"]


def test_score_job_computes_expected_shape():
    result = score_job(_job(), _PROFILE, _CONFIG)
    assert set(result.keys()) == {
        "overall_score", "score_breakdown", "recommended_status",
        "hard_blockers", "soft_risks", "job_terms_found",
    }
    assert result["overall_score"] > 0
    assert result["recommended_status"] in {"keep", "maybe", "reject"}


def test_score_job_with_precomputed_evidence_index_matches_default():
    """Bulk callers pass a precomputed evidence_index; result must match the
    default (profile-derived) path exactly."""
    default_result = score_job(_job(), _PROFILE, _CONFIG)
    index = build_evidence_index(_PROFILE)
    precomputed_result = score_job(_job(), _PROFILE, _CONFIG, evidence_index=index)
    assert precomputed_result == default_result


def test_hard_blocker_forces_reject():
    job = _job(_JOB_TEXT + " This role requires an active security clearance required.")
    result = score_job(job, _PROFILE, _CONFIG)
    assert result["recommended_status"] == "reject"
    assert "security clearance required" in result["hard_blockers"]


def test_no_matching_terms_scores_low():
    job = _job("Pastry Chef\n\nWe need someone who can bake croissants and manage a kitchen team.")
    result = score_job(job, _PROFILE, _CONFIG)
    assert result["recommended_status"] == "reject"
