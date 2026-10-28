"""Phase 2e (frontend): the auth pages and guard are served by the app.

The `static` gate (V2 M1): these are the static-client contract tests, so a
change to an HTML/JS/CSS asset has a test that fails when it breaks. CI runs
this gate on every HTML/JS/CSS change — see docs/testing.md.
"""

import re

import pytest

pytestmark = pytest.mark.static


@pytest.mark.parametrize(
    "path,marker",
    [
        ("/html/login.html", "Sign in"),
        ("/html/setup-password.html", "Set your password"),
        ("/html/admin-dashboard.html", "Provision"),
        # V3-P-003/S6: this surface is now Consultants; the file survives only
        # as a redirect shim because the server's own post-login landing map
        # (backend/auth/routes.py::_LANDING, out of this packet's scope) is
        # still keyed to this path.
        ("/html/ta-dashboard.html", "Talent Advisor"),
        ("/html/consultants.html", "MPYA Consultants"),
        ("/html/applications.html", "MPYA Applications"),
        ("/html/published-cvs.html", "MPYA Published CVs"),
        # The Slice 1 Job client, which replaced opportunity-dashboard.html.
        ("/html/jobs.html", "Create job"),
        ("/html/auth-guard.js", "requireAuth"),
        # Shared render core (Session 2, ADR-009) — still mounted; inert
        # without the viewer shell, which is not (see the S5-P2 test below).
        ("/html/cv-render.js", "function render("),
        ("/html/cv-render.css", ".cvs{"),
        ("/html/cv-viewer.js", "beaconSent"),
    ],
)
def test_auth_pages_served(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200
    assert marker in r.text


def test_public_viewer_route_serves_shell_with_noindex(client):
    """/cv/view serves the token-agnostic shell unconditionally — the token
    lives only in the URL *fragment* (`#<token>`), which a browser never sends
    to any server, so the route takes no path parameter at all (D-013)."""
    r = client.get("/cv/view")
    assert r.status_code == 200
    assert "Download PDF" in r.text  # the viewer shell
    assert "<div class=\"cvs\"" in r.text
    assert r.headers.get("X-Robots-Tag") == "noindex"
    # The promotion gate's strict header set (docs/plans/v2-pilot.md §11).
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("Content-Security-Policy", "")
    # A path segment after /cv/view no longer routes anywhere — the raw token
    # must never reach the server in a path (the §1 safety floor).
    assert client.get("/cv/view/anytokenatall").status_code == 404


def test_the_viewer_shell_has_no_header_less_alias_under_the_html_mount(client):
    """`S5-P1-R2` finding 1: `html/` is generically mounted at `/html`
    (`backend/main.py`), so a copy of the shell living there was a fully
    functional viewer carrying none of `_PUBLIC_HEADERS` — the fragment token
    worked against it exactly as well as against the real, hardened route.
    The shell now lives outside the mounted directory (`backend/templates/`),
    so this alias must 404, not merely lack headers on a 200."""
    r = client.get("/html/cv-viewer.html")
    assert r.status_code == 404


#: Any HTML inline event-handler attribute (`onclick=`, `onerror=`, …) inside a
#: tag. Every one of them is refused by a `script-src` without
#: `'unsafe-inline'`, so in the hardened shell each is a control that renders
#: and does nothing.
_INLINE_HANDLER_ATTR = re.compile(r"<[^>]*?\son[a-z]+\s*=", re.IGNORECASE)


def _csp_directives(response):
    """The response's CSP as {directive: [source, ...]}."""
    raw = response.headers.get("Content-Security-Policy", "")
    return {
        parts[0]: parts[1:]
        for parts in (d.strip().split() for d in raw.split(";") if d.strip())
    }


def test_the_hardened_viewer_shell_has_no_inline_event_handler_its_csp_would_kill(
    client,
):
    """`S5-P2-R1`: the shell's Download PDF button was an inline
    `onclick="window.print()"` while the response declared `script-src 'self'`
    with no `'unsafe-inline'` — a visible, dead public-viewer action. The CSP
    and the button were each asserted, in separate tests that never met.

    This asserts the *class*: the shell carries no `on*=` attribute at all, so
    the next one added fails a gate instead of shipping dead, and the action
    itself is owned by the external script the CSP already permits.

    What this cannot show is a browser actually honouring the policy — only
    that the inline handler is gone and `html/cv-viewer.js` owns the action.
    Clicking Download PDF against the real `/cv/view` response is a permanent
    step in `docs/testing.md`'s manual pilot walkthrough (step 8).
    """
    r = client.get("/cv/view")
    assert r.status_code == 200

    # The exact policy that makes an inline handler dead — no relaxation.
    assert _csp_directives(r)["script-src"] == ["'self'"]

    found = _INLINE_HANDLER_ATTR.findall(r.text)
    assert not found, (
        "the hardened viewer shell carries inline event-handler attribute(s) its "
        f"own CSP refuses to run: {found}"
    )

    # The control is still offered…
    assert 'id="downloadBtn"' in r.text
    assert "Download PDF" in r.text
    # …and the already-permitted external script is what makes it work.
    js = client.get("/html/cv-viewer.js")
    assert js.status_code == 200
    assert "downloadBtn" in js.text
    assert "window.print()" in js.text


# ── Multi-role client contract (V2 Step 1) ────────────────────────────────
#
# /api/auth/me returns `roles` (every role held) and `primary_role` (a display
# hint); the scalar `role` field is gone. The client migration to membership
# tests is otherwise invisible to the gates: a page that regressed to
# `me.role` would read `undefined`, silently sending a legitimate user to the
# wrong landing page or hiding a control from them, with every test still green.

_IDENTITY_PAGES = (
    "/html/auth-guard.js",
    "/html/login.html",
    "/html/cv-importer.html",
    "/html/jobs.html",
    "/html/ta-dashboard.html",
    "/html/admin-dashboard.html",
    "/html/consultants.html",
    "/html/applications.html",
    "/html/published-cvs.html",
)


def test_auth_guard_exposes_the_membership_helpers(client):
    body = client.get("/html/auth-guard.js").text
    for helper in ("function rolesOf(", "function hasRole(", "function hasAnyRole("):
        assert helper in body, f"auth-guard.js no longer defines {helper!r}"
    # requireAuth must gate on membership, not on a single role value.
    assert "hasAnyRole(me, allowedRoles)" in body


@pytest.mark.parametrize("path", _IDENTITY_PAGES)
def test_pages_do_not_read_the_removed_scalar_role_field(client, path):
    """No page may read `me.role` / `<obj>.user.role` — the API has no such
    field since roles became a membership list.

    Deliberately narrow: CV *content* legitimately has an assignment `role`
    (`a.role`, `role:a.role`) and the chat history uses `role:'user'`, so this
    matches only the identity-object access patterns. The trailing `\b` is what
    keeps `me.roles` / `user.roles` — the correct new reads — from matching,
    since `role` followed by `s` has no word boundary.
    """
    body = client.get(path).text
    assert not re.search(r"\bme\.role\b", body), f"{path} reads the removed me.role"
    assert not re.search(r"\.user\.role\b", body), (
        f"{path} reads the removed user.role"
    )


# ── Job client contract (V2 Slice 1) ──────────────────────────────────────


def test_jobs_page_exercises_the_whole_slice_1_workflow(client):
    """The minimal client must actually reach every Slice 1 endpoint — a page
    that silently lost its Reject or Score control would leave the workflow
    unexercised with every other gate still green."""
    body = client.get("/html/jobs.html").text
    for call in (
        "'/api/jobs'",            # list
        "method: 'POST'",         # create
        "method: 'PATCH'",        # edit
        "'/close'",               # close
        "'/score?consultant_username='",
        "'/reject'",
    ):
        assert call in body, f"jobs.html no longer performs {call}"


def test_jobs_page_disables_score_apply_and_reject_on_a_closed_job(client):
    """The server answers 409 for all three on a closed Job. The client must
    state the same rule rather than offering a control that can only fail.

    Score carries the closed flag directly; Apply and Reject carry it through
    their own flags, which fold the closed case in *first* so the one-live-
    decision reasons can never mask it."""
    body = client.get("/html/jobs.html").text
    assert "const closed = job.status === 'closed'" in body
    assert "data-act=\"score\"' + off +" in body, (
        "the Score control no longer carries the closed-Job disable"
    )
    for flag, control in (("applyOff", "apply"), ("rejectOff", "reject")):
        assert f"const {flag} = closed ? off" in body, (
            f"{flag} no longer starts from the closed-Job disable"
        )
        assert f"data-act=\"{control}\"' + {flag} +" in body, (
            f"the {control} control no longer carries {flag}"
        )


# ── Application client contract (V2 Slice 2) ──────────────────────────────


def test_jobs_page_exercises_the_whole_slice_2_workflow(client):
    """Apply and withdrawing a Reject must be reachable from the served page —
    otherwise the workflow is unexercised with every other gate green.

    The Application list/detail path and the bounded status transitions moved
    to their own focused dashboard in V3-P-003/S6
    (`test_applications_page_exercises_the_whole_tailored_cv_workflow` below);
    Apply itself (creating an Application) stays a Job-row action here.
    """
    body = client.get("/html/jobs.html").text
    for call in (
        "const APPS = '/api/applications'",   # apply
        "body: JSON.stringify({job_id: jobId, consultant_username: consultant})",
        "{method: 'DELETE'}",                 # withdraw a Reject
        "'/reject?consultant_username='",
    ):
        assert call in body, f"jobs.html no longer performs {call}"


def test_applications_page_offers_only_forward_application_transitions(client):
    """The service refuses a backwards or sideways move and accepts nothing at
    all from a terminal Application. The client must offer the same set rather
    than buttons that can only 409.

    Moved from jobs.html to applications.html in V3-P-003/S6 — the Application
    list/detail path and its status transitions are now their own dashboard."""
    body = client.get("/html/applications.html").text
    assert "const NEXT_STATUSES = {" in body
    for line in (
        "applied:   [['submitted', 'Mark submitted'], "
        "['closed', 'Close'], ['withdrawn', 'Withdraw']]",
        "submitted: [['closed', 'Close'], ['withdrawn', 'Withdraw']]",
        "closed:    []",
        "withdrawn: []",
    ):
        assert line in body, f"the transition map no longer contains: {line}"


def test_jobs_page_reads_the_consultant_context_from_the_policy(client):
    """Regression (S2-P1-R1): the page used `/api/ta/consultants` for TAs *and*
    Admins. That is a TA-assignment listing and 400s for an Admin without an
    explicit `ta_id`, so the pilot Admin+Consultant's picker silently collapsed
    to themselves. The picker must come from the policy's own set, with no role
    branching to get wrong."""
    body = client.get("/html/jobs.html").text
    assert "api('/api/me/consultants')" in body
    # The call, not the word — the comment explaining the defect names the old
    # path deliberately, and should stay.
    assert "api('/api/ta/consultants')" not in body, (
        "jobs.html is calling the TA-assignment listing again"
    )
    # The role branch that produced the wrong set is gone, not merely widened.
    assert "hasAnyRole(me, ['ta','admin'])" not in body


def test_jobs_page_scopes_decision_badges_to_the_selected_consultant(client):
    """Regression (S2-P1-R1): badges rendered whenever *any* visible consultant
    had acted, so a TA serving two consultants could see Mo's Rejected badge
    while being offered an enabled, valid Reject for Norberto — the row
    contradicting its own controls. Badges now come from the same `myReject`/
    `myApp` values the buttons do, and another consultant's decision is shown
    only when labelled as theirs."""
    body = client.get("/html/jobs.html").text
    assert "const badges =" in body
    for source in ("(myReject ?", "(myApp ?"):
        assert source in body, f"the badges no longer derive from {source}"
    # Another consultant's state is still visible — but as its own neutral
    # chip, explicitly labelled as theirs, never as the selected context's.
    assert 'class="pill other"' in body
    assert "Decisions by other consultants on this shared job" in body
    assert "k !== id" in body, (
        "the 'other consultants' count is no longer excluding the selected one"
    )


def test_jobs_page_states_the_one_live_decision_rule(client):
    """Apply and Reject are mutually exclusive for a pair; a control the server
    would refuse is disabled, and the tooltip says which action unblocks it."""
    body = client.get("/html/jobs.html").text
    assert 'title="Withdraw the reject first."' in body
    assert 'title="Withdraw or close the application first."' in body
    assert 'data-act="unreject"' in body


def test_jobs_page_offers_no_maybe_control(client):
    """P-008: Maybe is gone from the client contract too, not only the API."""
    body = client.get("/html/jobs.html").text.lower()
    assert "maybe" not in body


def test_the_retired_opportunity_dashboard_is_gone(client):
    """It was the Maybe/verdict client and read the JSON Job Store. Nothing may
    serve it or link to it any more."""
    assert client.get("/html/opportunity-dashboard.html").status_code == 404
    assert client.get("/opportunity-dashboard.html").status_code == 404
    for path in _IDENTITY_PAGES + ("/html/setup-password.html",):
        assert "opportunity-dashboard" not in client.get(path).text, path


# ── Tailored-CV client contract (V2 Slice 3) ──────────────────────────────


def test_applications_page_exercises_the_whole_tailored_cv_workflow(client):
    """Enqueue, poll, open, and download must all be reachable from the served
    page — otherwise the durable path is unexercised with every gate green.

    Moved from jobs.html to applications.html in V3-P-003/S6 (acceptance #3:
    Applications is its own focused dashboard, opening the existing tailored-CV
    editor rather than embedding this workflow in Jobs)."""
    body = client.get("/html/applications.html").text
    for call in (
        "const APPS = '/api/applications'",
        "'/status'",                      # bounded stage change
        "'/cv/generate'",                 # enqueue (202 + a run to poll)
        "'/cv/status'",                   # durable progress
        "pollTailoredCv",
        "data-act=\"open-cv\"",           # edit in the editor's application mode
        "data-act=\"print-cv\"",          # the PDF path
        "'?application=' + encodeURIComponent(app.id)",
    ):
        assert call in body, f"applications.html no longer performs {call}"


def test_applications_page_never_offers_generation_for_a_terminal_application(client):
    """The service answers 409 for a terminal Application. The client must state
    the same rule rather than offering a control that can only fail — while
    still letting an existing CV be opened and downloaded, because it is a
    record of what was sent."""
    body = client.get("/html/applications.html").text
    assert "if (!cv) {" in body
    assert "return app.is_live" in body, (
        "the Tailor-CV control no longer depends on the application being live"
    )
    assert "const again = app.is_live" in body, (
        "Regenerate no longer depends on the application being live"
    )
    assert "cv.has_content ? open : ''" in body, (
        "an existing tailored CV is no longer openable once the application ends"
    )


def test_the_editor_routes_saves_by_an_explicit_mode(client):
    """P-009's "explicit editor modes", asserted on the served page.

    The mode is chosen once from the URL and every save goes through the one
    writer that switches on it. A page that regressed to picking a URL per call
    site would be one forgotten branch away from writing a tailored CV into the
    consultant's base CV — the exact V1 behaviour this slice removes.
    """
    body = client.get("/html/cv-importer.html").text
    assert "function _persistCvState(keepalive)" in body
    assert "if(_appEditMode()){" in body
    assert "'/api/cv/base/'" in body
    assert "'/api/applications/'+encodeURIComponent(_appId)+'/cv'" in body
    # Exactly one place decides the destination: the two PUTs are the two modes
    # inside that one function, not two call sites.
    assert body.count("method:'PUT'") == 2, (
        "a second CV writer appeared outside _persistCvState()"
    )
    # `keepalive` lets a request outlive the page but caps the body at 64 KB, so
    # only the beforeunload path asks for it — an ordinary save has a live page.
    assert "_persistCvState(false)" in body
    assert "_persistCvState(true)" in body
    # And the two modes are mutually exclusive, decided before either loader.
    # A publication (ADR-009/D-013) is a read-only, server-derived snapshot of
    # a tailored CV — there is no separate publication-edit mode to route into.
    for branch in ("const applicationId = params.get('application')",
                   "await _enterApplicationEditMode(applicationId)",
                   "loadProfile();"):
        assert branch in body, f"the editor lost its mode routing: {branch}"
    # Every mode is representable in the variable the one writer switches on —
    # otherwise "the mode is decided once" would be true of only one of two.
    for assignment in ("_cvMode = 'base'", "_cvMode = 'application'"):
        assert assignment in body, f"the editor never enters {assignment}"
    assert "_cvMode = 'publication'" not in body, (
        "a publication-edit mode reappeared — publishing must stay a read-only "
        "snapshot of the tailored CV, not a third independently-editable copy"
    )


def test_the_editor_states_the_revision_every_save_is_based_on(client):
    """A write that did not say what it was based on is how two tabs used to
    overwrite each other silently."""
    body = client.get("/html/cv-importer.html").text
    assert "expected_revision: _cvRevision" in body
    assert "tailored_cv_id: _cvArtifactId" in body


def test_the_editor_no_longer_calls_the_retired_v1_routes(client):
    """Both were retired in this slice: the unversioned base cv-state pair, and
    the `verdicts.application_status` writer the editor called on generate and
    on print."""
    body = client.get("/html/cv-importer.html").text
    # The calls, not the words: two comments legitimately still say "cv-state"
    # while describing the editor's state, and deleting explanatory prose is not
    # what this guards.
    assert "'/cv-state'" not in body
    assert "+'/cv-state'" not in body
    assert "/cv-state', {" not in body
    assert "accepted-positions/" not in body, (
        "the editor still writes the retired verdicts application_status"
    )
    assert "'/api/generate-cv'" not in body


def test_the_application_mode_banner_does_not_print(client):
    """RCB-D1: the green Application-mode banner (`#appEditBanner`) is a
    screen-only affordance — a tailored CV downloaded as a PDF must show the
    CV sheet only. Same shape as cv-viewer.html's own `.viewer-header` print
    rule. Regression: there was no print rule at all for this element."""
    body = client.get("/html/cv-importer.html").text
    assert re.search(
        r"@media\s+print\s*\{\s*#appEditBanner\s*\{[^}]*display\s*:\s*none\s*!important",
        body,
    ), "the Application-mode banner has no @media print rule hiding it"


def test_viewer_beacon_fires_once(client):
    """The opened beacon must be guarded so a single page-load counts one view
    (prefetch/duplicate-event defence lives in the shell, not the server).

    The viewer's logic lives in an external file, not an inline <script> —
    that is what lets the public response's CSP hold `script-src 'self'` with
    no `'unsafe-inline'` exception (backend/cv_publish/routes.py)."""
    # The shell now lives outside the /html static mount (S5-P2) — fetch it
    # through the real route, not the retired header-less alias.
    shell = client.get("/cv/view").text
    assert 'src="/html/cv-viewer.js"' in shell
    # Every <script ...> tag in the shell must be an external src= reference —
    # no inline block, which is what lets the CSP omit 'unsafe-inline'.
    for tag in re.findall(r"<script\b[^>]*>", shell, flags=re.IGNORECASE):
        assert "src=" in tag.lower(), f"an inline <script> block reappeared: {tag}"
    body = client.get("/html/cv-viewer.js").text
    assert "beaconSent" in body
    assert "{once:true, passive:true}" in body
    # Beacon is armed on interaction, never on load.
    assert "armBeacon()" in body
    # The token is read from the fragment and sent as a header — never a path
    # or query string (the §1 safety floor).
    assert "location.hash" in body
    assert "X-CV-Publication-Token" in body


# ── Role-specific top-banner navigation (V3-P-003, S6) ────────────────────


def test_the_nav_matrix_pins_the_consultant_supercv_destination(client):
    """`html/auth-guard.js` is the one place the destination matrix is written
    down, shared by every surface. Pinning its literal order here is what
    would catch a silent drift from the approved navigation
    (docs/plans/v3/decisions.md), since every page renders from this same
    source rather than repeating its own copy."""
    body = client.get("/html/auth-guard.js").text
    assert "consultant: ['Jobs', 'Applications', 'Base CV', 'SuperCV', 'Published CVs']" in body
    assert "ta: ['Jobs', 'Consultants', 'Applications', 'Published CVs']" in body
    assert "'SuperCV': '/html/super-cv.html'" in body
    # S15 makes SuperCV reachable. The old Base-CV-only replacement emitted a
    # disabled chip that looked like navigation but went nowhere.
    assert "role === 'consultant' && name === 'Base CV'" not in body
    assert "label: 'SuperCV', href: null" not in body


def test_every_first_party_page_carries_an_mpya_surface_label(client):
    """Acceptance #4: every first-party surface has a top-left `MPYA <surface>`
    label."""
    for path, label in (
        ("/html/jobs.html", "MPYA Jobs"),
        ("/html/cv-importer.html", "MPYA Base CV"),
        ("/html/applications.html", "MPYA Applications"),
        ("/html/published-cvs.html", "MPYA Published CVs"),
        ("/html/consultants.html", "MPYA Consultants"),
        ("/html/admin-dashboard.html", "MPYA Admin"),
        ("/html/login.html", "MPYA Sign in"),
        ("/html/setup-password.html", "MPYA Set password"),
        ("/html/privacy.html", "MPYA Privacy"),
    ):
        assert label in client.get(path).text, f"{path} lost its MPYA surface label"


def test_the_public_viewer_carries_only_its_surface_label(client):
    """The public viewer gets a label and nothing else: no authenticated
    navigation, no capability-token handling change (S6 exclusions)."""
    body = client.get("/cv/view").text
    assert "MPYA CV" in body
    assert "topNav" not in body
    assert "renderTopNav" not in body


def test_create_job_and_create_consultant_each_belong_to_one_surface(client):
    """`+ Job` exists only on Jobs and `+ Consultant` only on Consultants —
    neither is a global top-banner action (V3-P-003)."""
    pages = {
        path: client.get(path).text
        for path in (
            "/html/jobs.html",
            "/html/consultants.html",
            "/html/applications.html",
            "/html/published-cvs.html",
            "/html/cv-importer.html",
        )
    }
    assert "+ Job" in pages["/html/jobs.html"]
    assert "+ Consultant" in pages["/html/consultants.html"]
    for path, body in pages.items():
        if path != "/html/jobs.html":
            assert "+ Job" not in body, f"{path} unexpectedly offers + Job"
        if path != "/html/consultants.html":
            assert "+ Consultant" not in body, f"{path} unexpectedly offers + Consultant"


def test_ta_dashboard_is_a_redirect_shim_to_consultants(client):
    """V3-P-003/S6 renamed the TA surface to Consultants. `ta-dashboard.html`
    survives only because the server's own post-login landing map is out of
    this packet's scope and still points here by path — see the file's own
    comment. It must not carry a second copy of the dashboard."""
    body = client.get("/html/ta-dashboard.html").text
    assert "location.replace('/html/consultants.html'" in body
    assert "addConsultantBtn" not in body
    assert "notifList" not in body


def test_applications_and_published_cvs_accept_a_plain_consultant_session(client):
    """V3-P-003's Consultant matrix includes Applications and Published CVs —
    both pages must authorize a Consultant-only session, not just TA/Admin."""
    for path in ("/html/applications.html", "/html/published-cvs.html"):
        body = client.get(path).text
        assert "requireAuth(['consultant','ta','admin'])" in body, (
            f"{path} no longer accepts a plain Consultant session"
        )
