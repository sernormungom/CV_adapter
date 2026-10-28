"""Behavioural test for the Consultants page's round trip of a selected
Consultant context (V3-P-003, S6 correction round 1).

Regression: `html/consultants.html` called `renderTopNav(..., '')` —
unconditionally, regardless of what `?consultant_id=` it was opened with. A
Talent Advisor who picked Consultant B on Jobs, followed the nav to
Consultants, then back to Jobs, landed back on the first Consultant: the
round trip silently dropped B. Static text assertions cannot see this, since
the literal call site (`renderTopNav(document.getElementById('topNav'), me,
'Consultants', ...)`) still exists either way.

`tests/client/consultants_client_probe.js` runs the page's real script
*together with the real `html/auth-guard.js`* under a stub DOM and a
programmable fetch, driving the page's own real `window.__initReady`. This
module asserts on what the rendered nav links actually carry.

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
_PROBE = _REPO_ROOT / "tests" / "client" / "consultants_client_probe.js"
_PAGE = _REPO_ROOT / "html" / "consultants.html"
_AUTH_GUARD = _REPO_ROOT / "html" / "auth-guard.js"


@pytest.fixture(scope="module")
def client_behaviour() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the client behaviour probe needs it")
    result = subprocess.run(
        [node, str(_PROBE), str(_PAGE), str(_AUTH_GUARD)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"the client probe failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_an_authorized_incoming_context_is_forwarded_on_every_nav_link(
    client_behaviour,
):
    scenario = client_behaviour["valid_context"]
    assert "consultant_id=consultant.b" in scenario["topNavHtml"]


def test_no_incoming_context_forwards_nothing(client_behaviour):
    scenario = client_behaviour["no_context"]
    assert "consultant_id=" not in scenario["topNavHtml"]


def test_an_unauthorized_incoming_context_is_not_forwarded(client_behaviour):
    scenario = client_behaviour["invalid_context"]
    assert "consultant_id=" not in scenario["topNavHtml"]
