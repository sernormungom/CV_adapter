"""On-demand advisory scoring for one Consultant–Job pair.

A score is a pure function of (consultant profile, matching config, job),
computed by deterministic code — no LLM. It is therefore **not a property of a
Job** and is never written back onto one: the same Job scores differently for
each Consultant, and a Consultant's config changes every learning cycle
(CLAUDE.md invariant 7, `docs/DECISIONS.md` P-007).

Three rules this module exists to hold:

**A missing profile or config is not an error, and never hides the Job.** The
caller gets `scored: false` with a machine-readable `reason` and a *null*
`match_score` — not a zero, which would read as a genuinely poor match, and not
a 500, which would make an ordinary onboarding state look like a fault. Slice 1
acceptance requires an "honest, safe failure response", and invariant 7 forbids
a missing score from making a valid Job ineligible.

**A consultant who has opted out (`User.actively_looking is False`) is never
scored, and their profile is never read for the attempt.** This is the Art 21
"ability to object" enforcement for `docs/legal/lia.md`'s Art 6(1)(f)
legitimate-interest matching/scoring processing (STAB-1). The flag is checked
before `load_profile`/`load_config` run at all, so opting out also stops the
scorer from reading the consultant's profile — a data-minimization property,
not just an ordering detail. The result is the same honest `scored: false` +
`reason` + null `match_score` shape as the other two failure reasons; it must
never hide, reject, or discard the Job itself (invariant 7).

**Nothing bootstraps a config here.** Bootstrapping calls an LLM and belongs to
an explicit learning run, not to a passive view of a Job.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.db.models import User
from backend.matching.config_reader import load_config
from backend.profile_reader import load_profile
from shared.scoring import score_job

#: Why a score could not be produced. Stable slugs — a client may branch on
#: them, so they are part of the API contract.
REASON_NO_PROFILE = "no_profile"
REASON_INVALID_PROFILE = "invalid_profile"
REASON_NO_CONFIG = "no_config"
REASON_OPTED_OUT = "opted_out"


def job_scoring_text(job) -> str:
    """The text the deterministic scorer reads for a relational Job.

    V1 fed it `raw_text`, the whole scraped posting. A manually entered Job
    keeps the same signal in separate columns, so they are rejoined here —
    title first, because `score_role_fit` looks for title patterns in the
    leading window of the text, and location included because `score_practical_fit`
    matches its rules against the body.
    """
    parts = (job.title, job.company, job.location, job.description)
    return "\n".join(p for p in parts if p)


def score_job_for_consultant(job, consultant: User) -> Dict[str, Any]:
    """Score `job` for `consultant`. Pure: writes nothing, anywhere.

    Always returns a dict. On success: `scored: true` plus the scorer's fields.
    On a missing/unparseable profile, a missing/invalid config, or a
    consultant who has opted out (`actively_looking is False`): `scored:
    false`, a `reason` slug, and `match_score: None`.

    The opt-out check runs **before** `load_profile`/`load_config` — an
    opted-out consultant's profile is never read for this call.
    """
    identity = {
        "consultant_username": consultant.username,
        "job_id": str(job.id),
    }
    if not consultant.actively_looking:
        return _unscored(REASON_OPTED_OUT, identity)

    try:
        profile = load_profile(consultant.username)
    except FileNotFoundError:
        return _unscored(REASON_NO_PROFILE, identity)
    except ValueError:
        # profile.md exists but has no YAML fence, or it is not a mapping.
        # An operator-fixable data problem, not a server fault.
        return _unscored(REASON_INVALID_PROFILE, identity)

    # load_config already falls back to the backup and validates the schema, so
    # None here means "no usable config", not merely "file absent".
    config = load_config(consultant.username)
    if config is None:
        return _unscored(REASON_NO_CONFIG, identity)

    result = score_job({"raw_text": job_scoring_text(job)}, profile, config)
    return {
        **identity,
        "scored": True,
        "reason": None,
        "match_score": result["overall_score"],
        "score_breakdown": result["score_breakdown"],
        "recommended_status": result["recommended_status"],
        "hard_blockers": result["hard_blockers"],
        "soft_risks": result["soft_risks"],
        "job_terms_found": result["job_terms_found"],
    }


def _unscored(reason: str, identity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **identity,
        "scored": False,
        "reason": reason,
        "match_score": None,
        "score_breakdown": None,
        "recommended_status": None,
        "hard_blockers": [],
        "soft_risks": [],
        "job_terms_found": {},
    }
