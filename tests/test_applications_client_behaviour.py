"""Behavioural tests for the Applications dashboard's own script (V3-P-003,
S6 correction round 1).

The `static` gate's text assertions cannot see a **control-flow** defect, and
the S6 review found exactly one: the page read `?consultant_id=` straight off
the URL and used it in the scope banner, the client-side filter, and the
context forwarded to `renderTopNav` — with no check that `GET
/api/me/consultants` (the one source of "which Consultant contexts this
caller may act in", `backend/authz/routes.py`) actually authorized it. A stale
link, a hand-typed username, or someone else's username therefore rendered as
if it were a real, authorized selection.

`tests/client/applications_client_probe.js` runs the page's real script
*together with the real `html/auth-guard.js`* under a stub DOM and a
programmable fetch, driving the page's own `window.__initReady` (the same
"await the real init" convention `html/cv-importer.html` already uses for
`window.__identityReady`). This module asserts on what it actually rendered.

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
_PROBE = _REPO_ROOT / "tests" / "client" / "applications_client_probe.js"
_PAGE = _REPO_ROOT / "html" / "applications.html"
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


def test_no_requested_context_shows_no_banner_and_no_filter(client_behaviour):
    scenario = client_behaviour["no_context"]
    assert scenario["scopeMsg"]["className"] == "result"
    assert scenario["appCount"] == "(2)"
    assert "consultant_id=" not in scenario["topNavHtml"]


def test_an_authorized_context_is_shown_filtered_and_forwarded(client_behaviour):
    scenario = client_behaviour["valid_context"]
    assert "show" in scenario["scopeMsg"]["className"]
    assert "info" in scenario["scopeMsg"]["className"]
    assert "consultant.a" in scenario["scopeMsg"]["html"]
    assert scenario["appCount"] == "(1)"
    assert "consultant_id=consultant.a" in scenario["topNavHtml"]


def test_an_unauthorized_context_is_dropped_not_rendered_as_valid(client_behaviour):
    """The regression: a `?consultant_id=` the caller does not hold in
    `GET /api/me/consultants` used to filter the list and forward on nav links
    exactly like a real selection. It must now be cleared: no scope banner
    naming it, every reachable Application shown, and no forwarded value that
    would carry the unauthorized identity onto the next page."""
    scenario = client_behaviour["invalid_context"]
    assert scenario["scopeMsg"]["className"] == "result"
    assert "not.mine" not in scenario["scopeMsg"]["html"]
    assert scenario["appCount"] == "(2)"
    assert "consultant_id=" not in scenario["topNavHtml"]


def test_a_failed_lookup_is_not_silently_treated_as_unassigned(client_behaviour):
    """A failed `GET /api/me/consultants` must not be presented as "no
    context" or, worse, as a silently-accepted requested one — it is an error,
    and forwarding the unvalidated value would be trusting exactly the input
    the lookup could not check."""
    scenario = client_behaviour["failed_lookup"]
    assert "err" in scenario["scopeMsg"]["className"]
    assert "Could not load your consultant list" in scenario["scopeMsg"]["text"]
    assert "consultant_id=" not in scenario["topNavHtml"]


# ── Read-only Job description (S21, V3-P-032) ──────────────────────────────


def test_the_job_title_is_a_control_unless_the_job_is_gone(client_behaviour):
    d = client_behaviour["description"]
    assert d["title_control"] is True
    assert d["removed_row_plain"] is True
    assert d["removed_row_has_no_view"] is True


def test_the_title_expands_a_read_only_description_and_collapses_again(
    client_behaviour,
):
    """Fetched from `GET /api/jobs/{id}`; the description and a link, nothing
    else — no buttons and none of the detail's other Job fields."""
    d = client_behaviour["description"]
    assert d["fetched"] == ["/api/jobs/job-desc"]
    expanded = d["expanded"]
    assert expanded["className"] == "app-desc show"
    assert expanded["expanded"] == "true"
    html = expanded["html"]
    assert 'class="app-desc-text"' in html
    assert "Line one\nLine two" in html          # line breaks kept, as text
    assert "<button" not in html and "data-act" not in html
    for other_field in ("Acme Corp", "Göteborg", "2026-12-31"):
        assert other_field not in html, other_field
    collapsed = d["collapsed"]
    assert collapsed["className"] == "app-desc"
    assert collapsed["expanded"] == "false"


def test_the_description_and_url_are_escaped_never_inserted_as_html(
    client_behaviour,
):
    html = client_behaviour["description"]["expanded"]["html"]
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    # The URL is an attribute value: its quotes are escaped, so it cannot
    # close the attribute and add a handler.
    assert (
        '<a href="https://example.test/p?a=1&amp;b=&quot;x&quot; '
        'onmouseover=&quot;alert(1)" target="_blank" rel="noopener noreferrer">'
        "Open original posting</a>"
    ) in html


def test_only_an_http_or_https_source_url_becomes_a_link(client_behaviour):
    html = client_behaviour["description"]["non_http_url"]["html"]
    assert 'class="app-desc-text"' in html
    assert "<a" not in html
    assert "javascript:" not in html


def test_a_failed_fetch_is_shown_inline(client_behaviour):
    view = client_behaviour["description"]["not_found"]
    assert view["className"] == "app-desc show"
    assert "Job not found" in view["html"]
    assert 'class="app-desc-text"' not in view["html"]


def test_the_description_view_preserves_line_breaks():
    """The stub DOM cannot render CSS; pin the rule that keeps the posting's
    own line breaks instead."""
    body = _PAGE.read_text(encoding="utf-8")
    assert ".app-desc-text{white-space:pre-wrap" in body
