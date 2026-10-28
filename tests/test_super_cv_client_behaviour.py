"""Behavioural tests for the SuperCV page's own script (S16/S17, V3-P-017 P2a/P2b).

The `static` gate's text assertions can see that a Save button and a Discard
button exist in the served HTML; they cannot see whether Save actually reads
the fields the consultant just edited, whether Discard reverts the in-memory
document or only the banner text, or whether a 409 shows the server's message
without silently retrying. `tests/client/super_cv_probe.js` runs the page's
real script — `applyLoadedArtifact`, `saveProfile`, `discardProfile`,
`dismissGap` — under a stub DOM and a programmable fetch, driving those
functions directly rather than the auth-gated `init()` IIFE, the same
"call the top-level functions" convention `jobs_client_probe.js` and
`applications_client_probe.js` use. This module asserts on what actually
happened.

S17 (P2b) adds career-history editing on the same probe: `saveIdentity`/
`discardIdentity` are renamed `saveProfile`/`discardProfile` below (they
already saved the whole document, not just Identity — s17.md's brief asked
for the rename once, rather than after a third section exists), and new
scenarios drive `addChip`, `addEvidence`/`deleteEvidence`, and
`deleteRoleGroup`'s confirmation the same way — calling the page's own
top-level functions under the stub DOM.

S18 (P2c) completes manual editing the same way: education/miscellaneous
entries, `personal_interests`, and career goals, driving `addEducationEntry`,
the `type` select's `change` listener, `addTargetRole`, and
`addInterestRow`/`removeInterestRow` directly.

S19 covers the compact chip adder by firing the real `AddBtn`/`Input`
listeners `wireChipAdder` attaches (open, type, Enter/Escape) rather than
calling `addChip` directly, plus the nested-role career-gap regression via
`careerHoleWarnings`, called directly since it takes plain data in and out.

Node is not a documented requirement of the `static` gate, so an absent
interpreter **skips** here, the way an absent Docker skips the integration
gate. CI runners ship Node, so it runs there.

The last section guards the probe itself, the same way
`tests/test_jobs_client_behaviour.py` does: a stub DOM is only trustworthy
while it stays small enough that its agreement with a real browser is
structural rather than guessed. See that file's header for the reasoning;
these guards apply it to this probe's own (smaller) stub.
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
_PROBE = _REPO_ROOT / "tests" / "client" / "super_cv_probe.js"
_PAGE = _REPO_ROOT / "html" / "super-cv.html"


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


# ── Identity edit reaches the save payload ─────────────────────────────────


def test_an_identity_edit_and_a_language_row_reach_the_save_payload(client_behaviour):
    payload = client_behaviour["edit_save_payload"]
    assert payload["full_name"] == "New Name"
    assert payload["email"] == "new@example.invalid"
    # The untouched existing row survives alongside the newly added one — a
    # structural array edit is not "replace the whole array with the new row".
    assert payload["languages"] == ["English", "Swedish"]
    assert payload["revision"] == 0
    assert payload["content_hash"] == "h0"


def test_a_successful_save_shows_a_confirmation(client_behaviour):
    assert client_behaviour["identity_msg_after_save"] == "Saved."


# ── Discard reverts real state ─────────────────────────────────────────────


def test_discard_reverts_the_in_memory_document_not_just_the_banner(
    client_behaviour,
):
    """Regression this guards against: a Discard that only resets displayed
    text but leaves a dismissed gap declined in the working document would
    silently persist that dismissal on the very next Save, even though the
    consultant explicitly discarded it."""
    assert "info" in client_behaviour["discard_msg_class"]
    assert client_behaviour["gap_status_after_discard"] == "open"


# ── The 409 path ────────────────────────────────────────────────────────────


def test_a_409_shows_the_servers_message_and_a_reload_control(client_behaviour):
    html = client_behaviour["conflict_message_html"]
    assert "This profile was changed by someone else." in html
    assert "reloadStaleBtn" in html


def test_a_409_does_not_silently_retry_or_adopt_a_new_revision(client_behaviour):
    assert client_behaviour["conflict_put_count"] == 1
    assert client_behaviour["conflict_kept_original_revision"] is True


# ── S17 (P2b): career-history editing (acceptance #11) ─────────────────────


def test_a_block_field_edit_reaches_the_save_payload_under_the_right_block_id(
    client_behaviour,
):
    assert client_behaviour["block_edit_block_id"] == "blk1"
    assert client_behaviour["block_edit_role_title"] == "New Title"


def test_adding_and_deleting_an_evidence_item_leaves_exactly_the_new_one(
    client_behaviour,
):
    """The added item's `evidence_id` stays blank client-side — minting is a
    server-side `assign_new_ids()` job (schema §2.6), never the browser's."""
    assert client_behaviour["evidence_add_delete_ids"] == [""]
    assert client_behaviour["evidence_add_delete_text"] == "Fresh evidence"


def test_a_chip_added_to_one_typed_group_lands_only_in_that_field(client_behaviour):
    """Regression this guards against: the six chip fields sharing one
    write-back path (the retired `tagsFor` merge) would land a Tools addition
    in every group, or in none."""
    assert client_behaviour["chip_added_tools"] == ["Docker"]
    assert client_behaviour["chip_added_domains"] == ["embedded systems"]


def test_editing_a_blocks_content_clears_its_verified_flag(client_behaviour):
    assert client_behaviour["verified_after_content_edit"] is False


def test_deleting_a_role_group_asks_first(client_behaviour):
    assert client_behaviour["role_groups_after_declined_confirm"] == 1
    assert client_behaviour["role_groups_after_confirmed_delete"] == 0


def test_the_evidence_word_counter_updates_live_while_typing(client_behaviour):
    """Regression this guards against: the word counter and over-60 advisory
    rendered once from the loaded document and never again, so a consultant
    typing (or pasting) past the guidance never saw it before Save — acceptance
    #4 requires it live, not only correct on the next unrelated re-render."""
    assert client_behaviour["word_meta_before_typing"] == "3 words"
    after = client_behaviour["word_meta_after_typing"]
    assert after.startswith("65 words")
    assert "over the ~60-word guidance" in after
    assert "will not block a save" in after
    assert client_behaviour["word_meta_color_after_typing"] == "var(--warn)"


# ── S18 (P2c): education, miscellaneous, personal interests, career goals ──


def test_an_education_field_edit_reaches_the_save_payload_under_the_right_id(
    client_behaviour,
):
    assert client_behaviour["education_edit_id"] == "edu1"
    assert client_behaviour["education_edit_institution"] == "New Institute"


def test_adding_an_entry_in_each_section_seeds_the_expected_default_type(
    client_behaviour,
):
    """Acceptance #3: Education defaults a new entry's `type` to `degree`,
    Miscellaneous to `course`."""
    assert sorted(client_behaviour["education_added_types"]) == ["course", "degree"]


def test_retyping_an_entry_to_degree_moves_it_to_the_education_section(
    client_behaviour,
):
    """Acceptance #2: which section an entry renders under is derived from
    its own `type` on every render, never stored separately — re-typing to
    `degree` must move the card into Education on the very next render, not
    only after Save."""
    assert client_behaviour["education_after_retype_in_education"] is True
    assert client_behaviour["education_after_retype_in_misc"] is False


def test_needs_review_clears_for_a_completed_edit_and_stays_for_an_incomplete_one(
    client_behaviour,
):
    """Acceptance #7: clearing only happens for an entry the human actually
    edited and only once nothing required is still missing — a required
    field left empty keeps the flag rather than silently dropping it.

    Also: an entry the human left untouched keeps its flag even though it has
    no `location` and the read-back materializes one as
    {city: '', country_code: ''} — that normalization is not a content edit
    (regression: this used to clear the flag on any save touching the page,
    not just a save that changed this specific entry)."""
    assert client_behaviour["needs_review_cleared"] is True
    assert client_behaviour["needs_review_retained"] is True
    assert client_behaviour["needs_review_untouched_retained"] is True


def test_a_career_goal_reaches_preferences_without_disturbing_unrendered_fields(
    client_behaviour,
):
    """Acceptance #5/#6: `target_roles` and `seniority_target` reach the save
    payload, while `avoid_industries` — deliberately unrendered — and
    `availability`, `work_mode`, `employment_types`, `location_preference` —
    schema-deleted unknown keys kept only by lossless whole-document
    passthrough — round-trip untouched."""
    assert client_behaviour["career_goal_target_roles"] == ["Embedded engineer", "Staff engineer"]
    assert client_behaviour["career_goal_seniority"] == "staff"
    assert client_behaviour["career_goal_unrendered_survive"] is True


def test_a_personal_interest_row_is_added_and_removed(client_behaviour):
    assert client_behaviour["personal_interests_after_edit"] == ["Hiking"]


# ── S19: compact chip adders, and the nested-role career-gap fix ───────────


def test_opening_the_adder_hides_the_compact_control_and_shows_the_input(
    client_behaviour,
):
    assert client_behaviour["chip_adder_open_display"] == ""
    assert client_behaviour["chip_adder_compact_display_while_open"] == "none"


def test_open_type_enter_adds_a_chip(client_behaviour):
    assert client_behaviour["chip_enter_added_tools"] == ["Docker"]


def test_open_escape_adds_nothing_and_closes_the_adder(client_behaviour):
    assert client_behaviour["chip_adder_open_display_after_escape"] == "none"
    assert client_behaviour["chip_escape_domains"] == ["embedded systems"]


def test_an_open_adders_text_is_absent_from_the_save_payload(client_behaviour):
    assert client_behaviour["chip_open_unsaved_tools"] == []


def test_a_role_group_contained_in_an_earlier_one_does_not_manufacture_a_gap(
    client_behaviour,
):
    """Acceptance #3's regression case: ITESM (2009-01 to 2009-06) sits
    entirely inside General Electric (2005-09 to 2020-01). The only real gap
    is General Electric to Mpya (2021-10), 20 months — measured from GE's
    end, not ITESM's, since ITESM's end is earlier than GE's."""
    warnings = client_behaviour["nested_role_gap_warnings"]
    assert warnings == [
        "20-month unexplained career gap between General Electric and Mpya."
    ]


# ── The stub may not become a browser ──────────────────────────────────────
#
# Same rule and reasoning as `tests/test_cv_editor_behaviour.py` (the other
# probe built on a per-scenario `loadPage()`, rather than one global stub
# `jobs_client_probe.js` uses): a uniform element with no per-id branches is
# what keeps a hand-rolled DOM honest, because every assertion above is about
# a URL or a payload rather than about the DOM agreeing with a real browser.
# Adding *scenarios* is free; teaching the stub a per-element behaviour is not.
#
# These guards need no Node and never skip.

_STUB_START = "// ── A stub DOM"
_STUB_END = "// ── Scenarios"

_MAX_STUB_LINES = 90


def _probe_source() -> str:
    return _PROBE.read_text(encoding="utf-8")


def _stub(source: str) -> str:
    for marker in (_STUB_START, _STUB_END):
        assert marker in source, (
            f"the probe lost its {marker!r} section marker, which is how these "
            "guards find the stub; restore it rather than deleting the guard"
        )
    return source[source.index(_STUB_START) : source.index(_STUB_END)]


def test_the_stub_element_has_no_per_element_behaviour():
    """No branch on an element's identity, anywhere in the stub.

    `if (this.id === 'someField')` encodes one page detail that a real
    browser gets right for every element (parsing a rendered `value="..."`
    attribute back into a live input) and is invisibly wrong everywhere else.
    This probe avoids needing that at all — see the probe's header comment on
    why scenarios seed a row's value explicitly instead — so zero is the
    right number here, not a grandfathered allowance the way
    `jobs_client_probe.js` carries one.
    """
    branches = re.findall(r"this\.id\s*===|id\s*===\s*['\"]", _stub(_probe_source()))
    assert branches == [], (
        f"the stub grew {len(branches)} per-element branch(es). A uniform stub "
        "is what makes this gate trustworthy — see the section comment."
    )


def test_the_stub_stays_small_enough_to_read():
    lines = len(_stub(_probe_source()).splitlines())
    assert lines <= _MAX_STUB_LINES, (
        f"the probe's stub is {lines} lines, over the {_MAX_STUB_LINES} cap. "
        "Raising the cap is fine for a genuinely bigger *page*; it is not the "
        "fix for a stub that is turning into a browser."
    )
