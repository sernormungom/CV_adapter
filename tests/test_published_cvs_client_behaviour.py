"""Behavioural tests for the Published CVs dashboard's own script (V3-P-003,
S6 correction round 1).

Two control-flow defects the `static` gate's text assertions cannot see:

1. Same shape as `tests/test_applications_client_behaviour.py`: the page read
   `?consultant_id=` straight off the URL and used it in the scope banner, the
   `GET /api/publications?consultant_username=` filter, and the context
   forwarded to `renderTopNav`, with no check that `GET /api/me/consultants`
   actually authorized it.
2. A publication row offered only Extend/Revoke — no way to open the
   Application or tailored-CV editor it belongs to, including an *expired*
   publication, which most needs it: Extend refuses a dead link, so opening
   the editor to republish is the only way forward.

`tests/client/published_cvs_client_probe.js` runs the page's real script
*together with the real `html/auth-guard.js`* under a stub DOM and a
programmable fetch, driving the page's own `window.__initReady`. This module
asserts on what it actually rendered.

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
_PROBE = _REPO_ROOT / "tests" / "client" / "published_cvs_client_probe.js"
_PAGE = _REPO_ROOT / "html" / "published-cvs.html"
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


# ── Consultant-context validation ─────────────────────────────────────────


def test_no_requested_context_shows_no_banner(client_behaviour):
    scenario = client_behaviour["no_context"]
    assert scenario["scopeMsg"]["className"] == "result"
    assert "consultant_id=" not in scenario["topNavHtml"]


def test_an_authorized_context_is_shown_and_forwarded(client_behaviour):
    scenario = client_behaviour["valid_context"]
    assert "show" in scenario["scopeMsg"]["className"]
    assert "info" in scenario["scopeMsg"]["className"]
    assert "consultant.a" in scenario["scopeMsg"]["html"]
    assert "consultant_id=consultant.a" in scenario["topNavHtml"]


def test_an_unauthorized_context_is_dropped_not_rendered_as_valid(client_behaviour):
    """The regression: a `?consultant_id=` outside the caller's
    `GET /api/me/consultants` set used to render a banner claiming it was a
    real selection, even though the Consultant-scoped API silently ignores an
    unauthorized filter and answers with the caller's own rows — a banner
    naming someone else's identity over the caller's own data."""
    scenario = client_behaviour["invalid_context"]
    assert scenario["scopeMsg"]["className"] == "result"
    assert "not.mine" not in scenario["scopeMsg"]["html"]
    assert "consultant_id=" not in scenario["topNavHtml"]


def test_a_failed_lookup_is_not_silently_treated_as_unassigned(client_behaviour):
    scenario = client_behaviour["failed_lookup"]
    assert "err" in scenario["scopeMsg"]["className"]
    assert "Could not load your consultant list" in scenario["scopeMsg"]["text"]
    assert "consultant_id=" not in scenario["topNavHtml"]


# ── Open the underlying Application / tailored CV ─────────────────────────


def test_every_row_offers_open_including_expired_and_revoked(client_behaviour):
    """Acceptance 3: a publication row must open the existing Application or
    tailored-CV editor. This must not depend on the link still being live —
    an expired publication (Extend refuses it) and a revoked one both still
    have a real underlying Application, and republishing one starts by
    opening its editor."""
    html = client_behaviour["no_context"]["pubListHtml"]
    for app_id, consultant in (
        ("app-active", "consultant.a"),
        ("app-expired", "consultant.b"),
        ("app-revoked", "consultant.a"),
    ):
        assert f'data-act="open"' in html
        assert f'data-app="{app_id}"' in html, (
            f"no Open control carries application {app_id}"
        )
        assert f'data-consultant="{consultant}"' in html, (
            f"the Open control for {app_id} does not carry its owner's identity"
        )


def test_open_uses_the_existing_application_editor_route():
    """The destination must be the same `?application=<id>` editor mode
    `html/applications.html` already uses (P-009's base/tailored split), not a
    new route this packet would have to invent."""
    body = _PAGE.read_text(encoding="utf-8")
    assert "function openApplication(applicationId, consultantUsername)" in body
    assert "'/html/cv-importer.html?application=' + encodeURIComponent(applicationId)" in body
    assert "'&consultant_id=' + encodeURIComponent(consultantUsername)" in body
