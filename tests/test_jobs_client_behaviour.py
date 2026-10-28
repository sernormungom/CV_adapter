"""Behavioural tests for the Jobs page's own script (V2 Slice 2, `S2-P1-R2`).

The rest of the `static` gate asserts that markers appear in the served HTML.
That cannot see a **control-flow** defect, and two S2-P1 review findings were
exactly that shape:

- a consultant-context load failure whose red banner was immediately
  overwritten by the blue "ask an admin to assign one" guidance, so a transient
  API failure was presented to the user as a valid empty assignment; and
- decision badges rendered from the whole visible map instead of the selected
  Consultant, so a row could show another Consultant's Rejected badge while
  offering an enabled, valid Reject for the selected one.

Both were green under text assertions. `tests/client/jobs_client_probe.js` runs
the page's real script under a stub DOM and reports what it actually did; this
module asserts on that. No browser, no framework, no build step — a few dozen
lines of stub is enough to drive the two functions, and anything heavier would
be a new dependency for a gate that documents needing none.

Node is not a documented requirement of the `static` gate, so an absent
interpreter **skips** here the way an absent Docker skips the integration gate.
CI runners ship Node, so it runs there.

The last section guards the probe itself: a stub DOM is only trustworthy while
it stays small enough that its agreement with a real browser is structural
rather than guessed, and that boundary is enforced here instead of being left to
each session's judgement. Those guards need no Node and never skip.
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
_PROBE = _REPO_ROOT / "tests" / "client" / "jobs_client_probe.js"
_PAGE = _REPO_ROOT / "html" / "jobs.html"


@pytest.fixture(scope="module")
def client_behaviour() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the client behaviour probe needs it")
    result = subprocess.run(
        [node, str(_PROBE), str(_PAGE)],
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


# ── The consultant-context banner ─────────────────────────────────────────


def test_a_failed_context_load_leaves_the_error_visible(client_behaviour):
    """Regression (S2-P1-R2): the failure branch showed the error and then fell
    through to the unconditional "no consultant available" message, which
    replaced it. A failed load is not an empty assignment list, and the user
    must not be told to ask an admin for an assignment they already have."""
    banner = client_behaviour["context_failure"]
    assert "err" in banner["className"], banner
    assert banner["text"].startswith("Could not load your consultant list")
    assert "Ask an admin" not in banner["text"]


def test_a_genuinely_empty_context_still_shows_the_assignment_guidance(
    client_behaviour,
):
    """The other half: fixing the failure path must not swallow the real
    empty-assignment case, which is the one that *should* say this."""
    banner = client_behaviour["context_empty"]
    assert "info" in banner["className"], banner
    assert "Ask an admin to assign one" in banner["text"]


def test_a_loaded_context_shows_no_banner_and_selects_the_first(client_behaviour):
    ok = client_behaviour["context_ok"]
    assert ok["className"] == "result", ok        # hidden
    assert ok["selected"] == "selected.consultant"


# ── Decision badges follow the selected Consultant ────────────────────────


def test_another_consultants_decision_is_not_badged_as_the_selected_ones(
    client_behaviour,
):
    """Regression (S2-P1-R1 finding 4), executed rather than pattern-matched.

    A shared Job where only the *non-selected* Consultant has rejected: no
    Rejected badge, a neutral "other" chip instead, and the Reject control stays
    enabled because the selected Consultant may still legitimately use it.
    """
    row = client_behaviour["badges_other_consultant_only"]
    assert row["rejected_badge"] is False
    assert row["applied_badge"] is False
    assert row["other_chip"] is True
    assert row["reject_button_disabled"] is False, (
        "the row badged one consultant's state while offering another an action"
    )


def test_the_selected_consultants_own_reject_is_badged_and_blocks_apply(
    client_behaviour,
):
    row = client_behaviour["badges_selected_consultant"]
    assert row["rejected_badge"] is True
    assert row["other_chip"] is False
    # The one-live-decision rule, stated by the control the server would refuse.
    assert row["apply_button_disabled"] is True


def test_the_selected_consultants_own_application_is_badged_and_blocks_reject(
    client_behaviour,
):
    row = client_behaviour["badges_selected_applied"]
    assert row["applied_badge"] is True
    assert row["reject_button_disabled"] is True


# ── Decided Jobs leave the selected Consultant's list (S21, V3-P-032) ──────


def test_the_selected_consultants_decided_jobs_are_hidden_by_default(
    client_behaviour,
):
    """A Reject marker or a live Application for the *selected* Consultant
    hides the row; another Consultant's decision on the same shared Job does
    not. Both the Open and the All views apply it."""
    for view in ("hide_open", "hide_all"):
        assert client_behaviour[view]["shown"] == ["undecided", "other-rejected"], view


def test_the_header_counts_still_come_from_the_server(client_behaviour):
    """S21 exclusion: `(total; unscored)` is the server's number and includes
    the hidden Jobs. A client recompute is a separate plan candidate."""
    assert client_behaviour["hide_open"]["count"] == "(5; 0 unscored)"


def test_show_rejected_never_sends_a_rejected_status_to_the_server(
    client_behaviour,
):
    """The API answers 400 "Unknown job status" to `status=rejected`. The view
    asks for every status and filters here, so a rejected *closed* Job is
    reachable too, and its Undo reject control is still rendered."""
    view = client_behaviour["show_rejected"]
    assert view["urls"], "the Rejected view made no list request"
    for url in view["urls"]:
        assert "status=" not in url, url
    assert view["shown"] == ["mine-rejected", "mine-rejected-closed"]
    assert 'data-act="unreject"' in view["list"]
    # The default views still ask the server for their status.
    assert any("status=open" in u for u in client_behaviour["hide_open"]["urls"])


def test_a_fully_decided_list_explains_where_the_jobs_went(client_behaviour):
    listing = client_behaviour["hide_everything_decided"]
    assert listing["shown"] == []
    assert "No jobs yet" not in listing["list"]
    assert "Show → Rejected jobs" in listing["list"]
    assert "Applications" in listing["list"]


def test_the_decision_confirmation_survives_the_reload(client_behaviour):
    """The row, and its in-row message, disappear on the reload that follows a
    decision. The page-level line must still say what happened and where the
    Job went — and it takes the title as text, not HTML."""
    rejected = client_behaviour["reject_confirmation"]
    assert "ok" in rejected["className"] and "show" in rejected["className"]
    assert rejected["text"] == (
        "Rejected 'O'Brien <b>Role</b>' for selected.consultant. "
        "Find it under Show → Rejected jobs."
    )
    assert client_behaviour["reject_confirmation_after_reload"] == rejected
    applied = client_behaviour["apply_confirmation"]
    assert applied["text"] == (
        "Applied to 'O'Brien <b>Role</b>' for selected.consultant. See Applications."
    )


def test_switching_consultant_clears_the_decision_confirmation(client_behaviour):
    """Review finding (S21): "Rejected X for A" stayed on screen over consultant
    B's list after the picker changed."""
    assert client_behaviour["confirmation_after_switch"]["className"] == "result"


def test_with_no_consultant_selected_nothing_is_hidden(client_behaviour):
    everything = ["undecided", "mine-rejected", "mine-applied", "other-rejected",
                  "mine-rejected-closed"]
    assert client_behaviour["no_consultant_open"]["shown"] == everything
    assert client_behaviour["no_consultant_rejected"]["shown"] == everything
    for url in client_behaviour["no_consultant_rejected"]["urls"]:
        assert "status=" not in url, url


# ── The stub may not become a browser ──────────────────────────────────────
#
# The tests below guard the probe itself, and they are the reason it is safe to
# keep in a gate that many short-lived sessions will edit. They need no Node, so
# unlike the behavioural tests above they never skip: the boundary is enforced
# even on a machine that cannot run the probe.
#
# The failure this prevents is not the loud one. A page that starts calling a
# DOM method the stub lacks raises a `TypeError` with a stack trace naming it —
# obvious, and either a three-line addition or a signal to stop. The dangerous
# move is teaching the stub to *emulate browser semantics*, because the stub
# then agrees with a real browser on the cases someone thought about and
# disagrees silently everywhere else, and a green gate that asserts fiction is
# worse than no gate at all.
#
# Two such emulations are grandfathered (the `<select>` value adoption in
# `set innerHTML`, and `closest()` returning null). They are counted, not
# forgiven in general: adding a third must be a decision someone makes on
# purpose, not the shortest path from a red test to a green one.


_STUB_START = "// ── A stub DOM"
_STUB_END = "// ── Scenarios"

#: Every member the stub `El` may expose. Adding one here is a claim that the
#: page needs it *and* that a bare container can honestly provide it.
_ALLOWED_EL_MEMBERS = frozenset(
    {"constructor", "addEventListener", "closest", "get innerHTML", "set innerHTML"}
)

#: The stub `document` is a lookup table, nothing more. `querySelector` and
#: friends belong to a real DOM, not to this.
_ALLOWED_DOCUMENT_MEMBERS = frozenset({"getElementById"})

#: Lines of stub *code* — the file header and the scenarios are both outside
#: the count. Scenarios are the growth this harness is for, and prose that
#: explains the boundary should never be what trips the guard protecting it.
_MAX_STUB_LINES = 90

#: `set innerHTML`'s `<select>` value adoption — the only one. `closest()`'s
#: flat null is the other grandfathered emulation but is not an identity
#: branch, so it is held by the member allowlist above, not by this budget.
#: Counting it here would leave exactly one branch of unused slack, which is
#: the whole drift this guard exists to refuse.
_GRANDFATHERED_IDENTITY_BRANCHES = 1


def _probe_source() -> str:
    return _PROBE.read_text(encoding="utf-8")


def _stub(source: str) -> str:
    """The stub's code: between the two section markers, header excluded."""
    for marker in (_STUB_START, _STUB_END):
        assert marker in source, (
            f"the probe lost its {marker!r} section marker, which is how these "
            "guards find the stub; restore it rather than deleting the guard"
        )
    return source[source.index(_STUB_START) : source.index(_STUB_END)]


def _class_body(source: str, header: str) -> str:
    """The brace-balanced body of `header`, e.g. ``class El {`` or ``= {``."""
    start = source.index(header) + len(header)
    depth = 1
    for offset, char in enumerate(source[start:]):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth == 0:
            return source[start : start + offset]
    raise AssertionError(f"unbalanced braces after {header!r} in the probe")


def _members(body: str) -> set[str]:
    """Member names declared directly in a class or object-literal body."""
    pattern = re.compile(r"^  (?:(get|set) )?([A-Za-z_$][\w$]*)\s*[(:]", re.MULTILINE)
    return {
        f"{kind} {name}".strip() for kind, name in pattern.findall(body)
    }


def test_the_stub_element_exposes_only_the_allowlisted_members():
    """A bigger `El` is how a stub DOM becomes a bad browser.

    If the page under test needs a DOM API this does not have, that is a real
    decision: add it here *and* to the allowlist when the stub can provide it
    honestly, or move to a browser harness when it cannot. What must not happen
    is the API arriving silently because it made one red test green.
    """
    members = _members(_class_body(_probe_source(), "class El {"))
    assert members == _ALLOWED_EL_MEMBERS, (
        "the stub DOM element changed shape.\n"
        f"  added:   {sorted(members - _ALLOWED_EL_MEMBERS)}\n"
        f"  removed: {sorted(_ALLOWED_EL_MEMBERS - members)}\n"
        "See this section's comment before widening the allowlist."
    )


def test_the_stub_document_stays_a_lookup_table():
    members = _members(_class_body(_probe_source(), "const document = {"))
    assert members == _ALLOWED_DOCUMENT_MEMBERS, (
        f"the stub document grew {sorted(members - _ALLOWED_DOCUMENT_MEMBERS)}; "
        "selector queries mean the page has outgrown a lookup table"
    )


def test_the_stub_does_not_emulate_more_browser_semantics():
    """The specific drift this harness is vulnerable to.

    Branching on an element's identity is the stub reproducing what a browser
    *does* rather than holding what the page *set* — the point where its
    agreement with a real browser stops being structural and starts being a
    guess. Exactly one such branch exists and is grandfathered.
    """
    found = len(re.findall(r"this\.id ===", _stub(_probe_source())))
    assert found <= _GRANDFATHERED_IDENTITY_BRANCHES, (
        f"the stub now branches on element identity {found} times, up from "
        f"{_GRANDFATHERED_IDENTITY_BRANCHES}. Emulating another browser "
        "behaviour by hand makes this gate assert fiction; use a real browser "
        "harness instead."
    )


def test_the_stub_stays_small_enough_to_read():
    """A line cap is a crude proxy, but it is the one an unfamiliar session
    trips over first, and its message points at the rule the other guards state
    precisely. Scenario growth is exempt by construction."""
    lines = len(_stub(_probe_source()).splitlines())
    assert lines <= _MAX_STUB_LINES, (
        f"the probe's stub is {lines} lines, over the {_MAX_STUB_LINES} cap. "
        "Raising the cap is fine for a genuinely bigger *page*; it is not the "
        "fix for a stub that is turning into a browser."
    )
