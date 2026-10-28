"""Behavioural tests for the CV editor's own script (V2 Slice 3, `S3-P1-R1`).

The rest of the `static` gate asserts that markers appear in the served HTML.
That cannot see a **control-flow** defect, and the first review round found one
of exactly that shape in the P-009 boundary this slice exists to build:

The editor holds three actions that write the consultant's *base* profile
material — the photo upload, the chat profile patch, and CV import (V2 Slice 4
restored it as `handleAsyncImportFile`, the durable replacement for the
retired synchronous importer). None of them went through `_persistCvState()`,
the one writer that switches on the
artifact mode, so each was left checking the mode for itself: the photo refused
publication mode only, and the profile patch refused nothing at all. Editing one
Application could therefore replace the photo and the profile facts every other
CV renders from, while every artifact's revision and hash stayed exactly where
they were. Worse, `_cvMode` was not set to `application` until *after* two
network round trips, so even a correct guard was reading the wrong mode during
the window a user is most likely to click something.

Every assertion in this module is about **which URLs the page's own code asked
`fetch` for**. That is deliberate: it is a question a small stub can answer
faithfully, unlike anything about rendering or layout. See the probe's header for
why the stub does not try to model browser behaviour, and the last section for
the guards that keep it that way.

Node is not a documented requirement of the `static` gate, so an absent
interpreter **skips** here, the way an absent Docker skips the integration gate.
CI runners ship Node, so it runs there.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.static

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROBE = _REPO_ROOT / "tests" / "client" / "cv_editor_probe.js"
_PAGE = _REPO_ROOT / "html" / "cv-importer.html"
_RENDER = _REPO_ROOT / "html" / "cv-render.js"


@pytest.fixture(scope="module")
def editor_behaviour() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the editor behaviour probe needs it")
    result = subprocess.run(
        [node, str(_PROBE), str(_PAGE), str(_RENDER)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"the editor probe failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


# ── The control ───────────────────────────────────────────────────────────


def test_base_mode_really_does_write_base_profile_material(editor_behaviour):
    """The probe can see these writes happen.

    Every other test in this module asserts an *absence*, and an absence is only
    evidence if the probe would have seen the presence. A page that never left
    base mode must write both endpoints; if this ever fails, the rest of the
    module is passing for the wrong reason and should not be trusted.
    """
    assert editor_behaviour["base_mode_writes"] == [
        "POST /api/profiles/nora/photo",
        "POST /api/chat/profile-patch",
        "POST /api/profiles/nora/cv-import",
    ]
    assert editor_behaviour["base_mode_gate"] is True


def test_the_base_profile_write_gate_exists_as_one_named_function(
    editor_behaviour,
):
    """Three call sites asking the same question must ask it in one place.

    The finding was not that a particular check was missing; it was that each
    mutator carried its own opinion about which modes were risky, and two of the
    three opinions were wrong. A named gate is what makes the next such action
    inherit the answer instead of re-deriving it.
    """
    assert editor_behaviour["has_base_gate"] == "function"


def test_the_durable_async_importer_is_wired_up(editor_behaviour):
    """The replacement for the retired synchronous importer actually exists —
    the absence assertions below are only evidence once this is true."""
    assert editor_behaviour["has_async_import"] == "function"


# ── The P-009 boundary in Application mode ────────────────────────────────


def test_application_mode_writes_no_base_profile_material(editor_behaviour):
    assert editor_behaviour["app_mode_after_load"] is True
    assert editor_behaviour["application_mode_gate"] is False
    assert editor_behaviour["application_mode_base_writes"] == []


def test_the_mode_is_claimed_before_the_first_await(editor_behaviour):
    """The window, not just the guard (`S3-P1-R1` finding 1).

    The probe parks `_enterApplicationEditMode` inside its first request, which
    never resolves — the page is then in exactly the state it held for two round
    trips. The mode must already read as Application there, and a base-profile
    write attempted right then must still be refused. Asserting only the settled
    state would have been green on the defect.
    """
    assert editor_behaviour["app_mode_claimed_before_await"] is True
    assert editor_behaviour["writes_during_app_load"] == []


# ── The retired synchronous importer ──────────────────────────────────────


def test_the_editor_has_no_synchronous_import_path_left(editor_behaviour):
    """Release Cut A disabled `POST /api/import-cv` (`docs/plans/v2-pilot.md`
    Slice 3), and V2 Slice 4 restores CV import as a durable, differently-shaped
    flow (`handleAsyncImportFile`, upload → poll → preview) rather than reusing
    these three names — see `test_the_durable_async_importer_is_wired_up` for
    the replacement, and `docs/handoffs/slice-4-async-import.md`.
    `tests/test_auth_enforcement.py` holds the server half (the old route
    itself is gone, permanently, not just for Release Cut A).
    """
    assert editor_behaviour["import_symbols"] == {
        "handleImportFile": "undefined",
        "populateFromImport": "undefined",
        "importBtnState": "undefined",
    }


def test_the_editor_loads_no_third_party_script_for_the_retired_importer():
    """Mammoth is gone (`S3-P1-R2`, the reviewer's optional improvement).

    It read DOCX in the browser, was orphaned when import moved to server-side
    extraction *before* this slice, and kept executing on every editor load. A
    third-party script on a page that renders CV content earns its place or goes.
    Asserted against the file rather than through the probe, because the point is
    that the browser is never asked for it — a stub DOM cannot observe that.
    """
    page = _PAGE.read_text(encoding="utf-8")
    assert "mammoth" not in page.lower().replace("mammoth (client-side docx", "")

    # The remaining third-party origins, named so that adding one is a decision.
    origins = set(re.findall(r'<(?:script|link)[^>]+(?:src|href)="(https?://[^/"]+)', page))
    assert origins == {"https://fonts.googleapis.com"}, sorted(origins)


# ── S10: Generate CV and the per-section style controls are gone ─────────


def test_generate_cv_and_its_modal_are_deleted_not_hidden(editor_behaviour):
    """Acceptance 1: the header control, its modal, and every function that
    only existed to serve it are gone as top-level declarations — not merely
    unreachable through a hidden button."""
    assert editor_behaviour["generate_modal_symbols"] == {
        "openGenerateModal": "undefined",
        "closeGenerateModal": "undefined",
        "generateCV": "undefined",
        "_genAppRow": "undefined",
        "_generateApplications": "undefined",
        "_selectedApplicationIdx": "undefined",
    }


def test_no_generate_cv_control_or_modal_markup_in_either_mode():
    """The header shows no "Generate CV" control in base or Application-edit
    mode — both modes share the same header markup, so one static check
    covers both (acceptance 1, 2)."""
    page = _PAGE.read_text(encoding="utf-8")
    assert "Generate CV" not in page
    assert 'id="genModal"' not in page
    assert 'id="genPositionsList"' not in page
    assert 'id="genSelectedBadge"' not in page
    assert 'id="genBtn"' not in page


def test_no_style_box_markup_or_css_left(editor_behaviour):
    """The four per-section text-style panels and their dedicated CSS are
    deleted, not hidden (acceptance 3). The shared `.sc-row`/`.sc-label`/
    `.sc-items`/`.sc-add` classes stay — they are also used by the Hard
    Skills/Soft Skills/Tools list editor, unrelated content editing."""
    page = _PAGE.read_text(encoding="utf-8")
    assert "style-box" not in page
    for field_id in (
        "pfFont", "pfSize", "pfColor", "pfWeight",
        "skFont", "skSize", "skColor", "skWeight",
        "exFont", "exSize", "exColor", "exWeight",
        "edFont", "edSize", "edColor", "edWeight",
    ):
        assert f'id="{field_id}"' not in page

    # Shared list-editing UI (Hard Skills/Soft Skills/Tools) stays.
    assert ".sc-row" in page
    assert ".sc-items" in page


def test_body_text_ignores_stale_per_section_style_values(editor_behaviour):
    """Acceptance 4 and 7: a fixture that still carries old per-section style
    values (as an existing `cv_state` document might) renders with the shared
    fixed style, not those values — no migration is needed because they are
    simply never read again."""
    sheet_html = editor_behaviour["profile_style_sheet_html"]
    assert "Playfair" not in sheet_html
    assert "22px" not in sheet_html
    assert "#ff0000" not in sheet_html
    assert "font-family:'Source Sans 3',sans-serif" in sheet_html
    assert "font-size:10px" in sheet_html
    assert "color:#444444" in sheet_html
    assert "font-weight:400" in sheet_html


# ── CV-state saves still route by mode ────────────────────────────────────


def test_a_cv_state_save_goes_to_the_artifact_its_mode_names(editor_behaviour):
    """The one-writer switch, executed rather than read.

    This was already asserted from the served text. Executing it is what proves
    the Application branch is *taken* — and, in the same run, that it is taken
    by a page that reached Application mode through its real entry point rather
    than by a test setting a variable.
    """
    assert editor_behaviour["application_mode_state_save"] == [
        "PUT /api/applications/app-1/cv"
    ]
    assert editor_behaviour["base_mode_state_save"] == ["PUT /api/cv/base/nora"]


def test_a_saved_photo_reference_carries_no_per_load_cache_buster(
    editor_behaviour,
):
    """S22: an idle autosave must send the same content every time.

    The server treats a content-identical save as a no-op, but the editor gave
    its in-memory photo a fresh `?t=<now>` on every page load and saved it
    as-is, so the first autosave after each load always looked like an edit:
    the revision climbed and a tailored CV's `content_changed_at` moved just
    from opening the editor.
    The query is for the `<img>` alone. Both load paths that set the photo are
    covered: restoring a stored reference (stored *with* a stale query, as
    pre-S22 saves left it) and falling back to the backend photo.
    """
    assert editor_behaviour["saved_photo_from_stored_state"] == "/api/profiles/nora/photo"
    assert editor_behaviour["saved_photo_from_backend_fallback"] == "/api/profiles/nora/photo"


# ── The stub may not become a browser ─────────────────────────────────────
#
# Same rule and same reasoning as `tests/test_jobs_client_behaviour.py`, and it
# matters more here: this probe drives a 1900-line page, so the temptation to
# teach the stub "just one more" browser behaviour is correspondingly larger. It
# resists that by being uniform — one element class that answers every id and
# every selector identically, with no per-element branches at all. That is what
# keeps it honest at this size, and it is affordable precisely because every
# assertion above is about a URL rather than about the DOM.
#
# Adding *scenarios* is free and is what the probe is for. Adding behaviour is
# not: the moment an assertion depends on the stub acting like a browser, this
# gate asserts fiction. If the page needs that, it needs a real browser harness.
#
# These guards need no Node and never skip.

_STUB_START = "// ── A uniform recording stub"
_STUB_END = "// ── A recording fetch"

_MAX_STUB_LINES = 80


def _probe_source() -> str:
    return _PROBE.read_text(encoding="utf-8")


def _stub(source: str) -> str:
    for marker in (_STUB_START, _STUB_END):
        assert marker in source, (
            f"the probe no longer contains {marker!r}, so these guards cannot "
            "find the stub. Restore the marker rather than deleting the guard."
        )
    return source[source.index(_STUB_START) : source.index(_STUB_END)]


def test_the_stub_element_has_no_per_element_behaviour():
    """No branch on an element's identity, anywhere in the stub.

    This is the specific thing that turns a lookup table into a bad simulation
    of a browser: `if (this.id === 'someField')` encodes one page detail that a
    real browser gets right for every element, and it is invisibly wrong for all
    the others. The jobs probe carries exactly one such branch, grandfathered
    with a comment; this one carries none and must keep carrying none.
    """
    stub = _stub(_probe_source())
    branches = re.findall(r"this\.id\s*===|id\s*===\s*['\"]", stub)
    assert branches == [], (
        f"the stub grew {len(branches)} per-element branch(es). A uniform stub "
        "is what makes a probe this size trustworthy — see the section comment."
    )


def test_the_stub_stays_small_enough_to_read():
    """A line cap is crude, but it is the limit an unfamiliar session will see
    before it starts growing the stub instead of questioning the assertion."""
    lines = len(_stub(_probe_source()).splitlines())
    assert lines <= _MAX_STUB_LINES, (
        f"the probe's stub is {lines} lines, over the {_MAX_STUB_LINES} cap. "
        "Raising the cap is fine for a genuinely bigger *page*; it is not the "
        "way to make one assertion pass."
    )


def test_the_probe_cannot_set_the_editors_mode_directly():
    """The mode must be reached through the page, not assigned by the test.

    The first version of this probe set `_cvMode` on the vm context and asserted
    the guards. Every one of those assertions was vacuous: a `let` in a vm script
    is script-scoped, so the probe was writing an unrelated global while the page
    kept its own value — and the tests "passed" against code that had no guard at
    all. Reaching the mode through the page's real entry point is the only form
    of this test that proves anything, so the shortcut is barred here.
    """
    source = _probe_source()
    for forbidden in ("ctx._cvMode =", "context._cvMode =", "_pubEditMode ="):
        assert forbidden not in source, (
            f"the probe assigns {forbidden!r}. A `let` in a vm script is not "
            "reachable from the host, so this asserts nothing about the page; "
            "drive the mode through its entry point instead."
        )
