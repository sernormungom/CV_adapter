"""Behavioural tests for html/cv-importer.html's identity-seeding IIFE
(`window.__identityReady`), added in the S6 correction round.

The regression: an admin/TA's `?consultant_id=` view-as target was written
straight to `localStorage` — becoming the identity every base-CV read/write
on the page acts as, and the identity `renderTopNav` forwards on nav links —
with no check that `GET /api/me/consultants` (the one source of "which
Consultant contexts this caller may act in") actually authorized it. A
hand-typed or stale target was trusted exactly like a real selection, even
though `html/auth-guard.js`'s own `renderTopNav` comment states the design
intent: a carried context "grants nothing" until the destination
re-validates it. This page was the one place that skipped the
re-validation. Static text assertions cannot see this, since the call sites
(`localStorage.setItem('mpya_username', ...)`) are unchanged in shape.

`tests/client/cv_identity_probe.js` runs only the identity IIFE (the first of
this page's two inline `<script>` blocks — see
`tests/test_cv_editor_behaviour.py`/`cv_editor_probe.js` for why the file has
exactly two) under a stub with a controllable `resolveConsultantContext`, and
reports the resulting `localStorage` state.

Node is not a documented requirement of the `static` gate, so an absent
interpreter **skips** here, the way an absent Docker skips the integration
gate. CI runners ship Node, so it runs there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.static

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROBE = _REPO_ROOT / "tests" / "client" / "cv_identity_probe.js"
_PAGE = _REPO_ROOT / "html" / "cv-importer.html"


@pytest.fixture(scope="module")
def identity_behaviour() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the identity behaviour probe needs it")
    result = subprocess.run(
        [node, str(_PROBE), str(_PAGE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"the identity probe failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_a_consultant_always_gets_their_own_identity(identity_behaviour):
    scenario = identity_behaviour["consultant_own"]
    assert scenario == {"mpya_username": "nora", "mpya_consultant_id": "nora"}


def test_a_consultant_only_account_cannot_be_redirected_by_a_hand_typed_target(
    identity_behaviour,
):
    """Control, not new behaviour: a Consultant-only session never switches,
    whatever `?consultant_id=` says — the server would refuse it anyway."""
    scenario = identity_behaviour["consultant_ignores_target"]
    assert scenario == {"mpya_username": "nora", "mpya_consultant_id": "nora"}


def test_a_ta_view_as_target_the_policy_authorizes_is_seeded(identity_behaviour):
    scenario = identity_behaviour["ta_valid_target"]
    assert scenario == {
        "mpya_username": "consultant.a",
        "mpya_consultant_id": "consultant.a",
    }


def test_an_unauthorized_view_as_target_is_cleared_not_trusted(identity_behaviour):
    """The regression. A TA's target that GET /api/me/consultants does not
    return must not seed an identity every other function on the page would
    then act as."""
    scenario = identity_behaviour["ta_invalid_target"]
    assert scenario == {"mpya_username": None, "mpya_consultant_id": None}


def test_a_failed_consultant_lookup_clears_rather_than_trusts_the_target(
    identity_behaviour,
):
    scenario = identity_behaviour["ta_lookup_fails"]
    assert scenario == {"mpya_username": None, "mpya_consultant_id": None}


def test_a_dual_role_admin_consultant_falls_back_to_their_own_profile(
    identity_behaviour,
):
    """An invalid target must not leave a dual-role Admin/TA+Consultant with
    no identity at all when they legitimately have their own profile to fall
    back to."""
    scenario = identity_behaviour["dual_role_invalid_target_falls_back_to_self"]
    assert scenario == {
        "mpya_username": "the.admin",
        "mpya_consultant_id": "the.admin",
    }


def test_a_ta_only_session_with_no_target_clears_a_stale_identity(
    identity_behaviour,
):
    """V3-P-012 / S7: a TA/Admin-only session with no `?consultant_id=` at
    all must clear any prior session's or prior selection's stored identity
    rather than leaving it in place."""
    scenario = identity_behaviour["ta_no_target_clears_stale_identity"]
    assert scenario == {"mpya_username": None, "mpya_consultant_id": None}
