"""The allowlist sanitizer for every state-derived value a publication places
in rendered markup or an HTML attribute (`docs/plans/v2-pilot.md` Slice 5:
"minimize/sanitize content"; widened in S5-P2 per `S5-P1-R2` finding 1).

Negative tests for the concrete attack shapes a hiring-manager viewer must
never be exposed to: a public publication's `cv_state` is attacker-reachable
content (`POST /api/publications` derives it from a tailored CV a caller with
API access could shape directly), `html/cv-render.js` interpolates `descHtml`
verbatim into `sheet.innerHTML`, and its `ts(prefix)` builds a `style="..."`
attribute from the twelve presentation fields with no escaping at all.
"""

import pytest

from backend.cv_publish.sanitize import sanitize_cv_state, sanitize_rich_text


# ── sanitize_rich_text: the concrete attack shapes ─────────────────────────


def test_script_tag_is_removed():
    out = sanitize_rich_text('<script>alert(1)</script>hello')
    assert "<script" not in out.lower()
    assert "</script" not in out.lower()
    assert "hello" in out


def test_event_handler_attribute_is_stripped_with_its_tag():
    out = sanitize_rich_text('<img src=x onerror="alert(1)">text')
    assert "onerror" not in out.lower()
    assert "<img" not in out.lower()
    assert "text" in out


def test_javascript_href_is_removed_with_its_tag():
    out = sanitize_rich_text('<a href="javascript:alert(1)">click me</a>')
    assert "javascript:" not in out.lower()
    assert "<a" not in out.lower()
    assert "click me" in out  # text content survives, just not as a link


def test_svg_onload_is_removed():
    out = sanitize_rich_text('<svg onload=alert(1)></svg>')
    assert "onload" not in out.lower()
    assert "<svg" not in out.lower()


def test_style_tag_and_css_injection_removed():
    out = sanitize_rich_text('<style>body{display:none}</style>bold text')
    assert "<style" not in out.lower()
    assert "bold text" in out


def test_no_attribute_survives_on_an_allowed_tag():
    """Even a harmless-looking attribute on an allowed tag is dropped —
    the allowlist is tags only, never attributes (a `style=` on `<span>`
    is exactly how a color-picker value could otherwise smuggle CSS)."""
    out = sanitize_rich_text('<span style="color:red" onclick="alert(1)">hi</span>')
    assert "style=" not in out
    assert "onclick" not in out
    assert out == "<span>hi</span>"


def test_iframe_object_embed_are_all_removed():
    for tag, attrs in (
        ("iframe", 'src="javascript:alert(1)"'),
        ("object", 'data="javascript:alert(1)"'),
        ("embed", 'src="javascript:alert(1)"'),
    ):
        out = sanitize_rich_text(f"<{tag} {attrs}></{tag}>safe")
        assert f"<{tag}" not in out.lower()
        assert "javascript:" not in out.lower()
        assert "safe" in out


def test_entity_encoded_markup_is_re_escaped_not_revived():
    """Literal text that was properly entity-encoded in the source (the
    author's intent was the visible string "<script>", not a live tag) must
    come back out re-escaped, not as live markup — no double-decode."""
    out = sanitize_rich_text("&lt;script&gt;alert(1)&lt;/script&gt;")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ── The RTE's own legitimate output survives ───────────────────────────────


def test_allowed_formatting_tags_pass_through():
    out = sanitize_rich_text("<b>bold</b> <i>italic</i> <u>under</u>")
    assert out == "<b>bold</b> <i>italic</i> <u>under</u>"


def test_allowed_lists_pass_through():
    out = sanitize_rich_text("<ul><li>one</li><li>two</li></ul>")
    assert out == "<ul><li>one</li><li>two</li></ul>"


def test_br_void_tag_passes_through():
    assert sanitize_rich_text("line1<br>line2") == "line1<br>line2"


def test_plain_text_is_untouched_besides_escaping():
    assert sanitize_rich_text("just plain text") == "just plain text"


def test_ampersand_and_angle_bracket_text_is_escaped():
    out = sanitize_rich_text("Q&A: 5 < 10 > 2")
    assert "&amp;" in out
    assert "&lt;" in out
    assert "&gt;" in out


# ── Edge cases ──────────────────────────────────────────────────────────


def test_non_string_input_becomes_empty_string():
    assert sanitize_rich_text(None) == ""
    assert sanitize_rich_text(123) == ""
    assert sanitize_rich_text({"a": 1}) == ""
    assert sanitize_rich_text([1, 2]) == ""


def test_empty_string_stays_empty():
    assert sanitize_rich_text("") == ""


def test_unbalanced_tags_close_out_cleanly():
    # A stray close with nothing open is dropped rather than emitted.
    assert sanitize_rich_text("</b>text") == "text"
    # An open tag never closed is closed at the end, not left dangling.
    assert sanitize_rich_text("<b>text") == "<b>text</b>"


# ── sanitize_cv_state: descHtml and the twelve presentation fields ─────────


def test_sanitize_cv_state_cleans_every_assignment_deschtml():
    state = {
        "iName": "<script>alert(1)</script>Alice",  # not sanitized — see below
        "expList": [
            {
                "company": "Acme",
                "assignments": [
                    {"role": "Dev", "descHtml": "<script>alert(1)</script>ok"},
                    {"role": "Lead", "descHtml": "<b>fine</b>"},
                ],
            }
        ],
    }
    out = sanitize_cv_state(state)
    assert "<script" not in out["expList"][0]["assignments"][0]["descHtml"].lower()
    assert out["expList"][0]["assignments"][0]["descHtml"].endswith("ok")
    assert out["expList"][0]["assignments"][1]["descHtml"] == "<b>fine</b>"
    # iName is a rendering sink html/cv-render.js escapes itself (esc()), not
    # one this module owns — sanitizing it here would be redundant work, not
    # additional safety, so it is left as the caller gave it.
    assert out["iName"] == "<script>alert(1)</script>Alice"


# ── The twelve presentation fields: an attribute-context injection, not a
# markup one — S5-P2 (`S5-P1-R2` finding 1) ────────────────────────────────


def test_a_malicious_font_value_is_replaced_with_the_renderer_default():
    """The reviewer's worked case: breaks out of the `style="..."` attribute
    `ts()` builds and injects a live element."""
    payload = 'x"><img src=x onerror="alert(1)"><span style="'
    out = sanitize_cv_state({"pfFont": payload})
    assert out["pfFont"] == "'Source Sans 3',sans-serif"
    assert "<img" not in out["pfFont"]
    assert "onerror" not in out["pfFont"]


@pytest.mark.parametrize("prefix", ["pf", "sk", "ex", "ed"])
def test_a_malicious_value_is_replaced_on_every_presentation_group(prefix):
    """All four groups (`ts('pf')`, `ts('sk')`, `ts('ex')`, `ts('ed')`) share
    the same unescaped `style="..."` construction in `html/cv-render.js`."""
    payload = '"><script>alert(1)</script>'
    out = sanitize_cv_state(
        {f"{prefix}Font": payload, f"{prefix}Color": payload, f"{prefix}Weight": payload}
    )
    assert out[f"{prefix}Font"] == "'Source Sans 3',sans-serif"
    assert out[f"{prefix}Color"] == "#444"
    assert out[f"{prefix}Weight"] == "400"
    for value in out.values():
        assert "<script" not in value.lower()


def test_every_real_font_option_survives_unchanged():
    """Transcribed from html/cv-importer.html's own <select> — every one of
    the editor's real choices must pass through untouched (check 3's
    unit-level half; the end-to-end half is
    tests/test_cv_publish.py::test_a_real_generated_tailored_cv_publishes_its_presentation_unchanged)."""
    fonts = [
        "'Source Sans 3',sans-serif", "'Source Sans 3',sans-serif|italic",
        "'Lato',sans-serif", "'Lato',sans-serif|italic",
        "'Raleway',sans-serif", "'Raleway',sans-serif|italic",
        "'Source Serif 4',serif", "'Source Serif 4',serif|italic",
        "'Playfair Display',serif", "'Playfair Display',serif|italic",
        "Georgia,serif", "Georgia,serif|italic",
    ]
    for font in fonts:
        assert sanitize_cv_state({"pfFont": font})["pfFont"] == font


def test_every_real_weight_option_survives_unchanged():
    for weight in ("300", "400", "600"):
        assert sanitize_cv_state({"skWeight": weight})["skWeight"] == weight


def test_a_real_color_value_survives_unchanged():
    assert sanitize_cv_state({"exColor": "#a1b2c3"})["exColor"] == "#a1b2c3"
    assert sanitize_cv_state({"exColor": "#FFFFFF"})["exColor"] == "#FFFFFF"


def test_a_malformed_color_is_replaced_not_passed_through():
    for bad in ('red', '#fff', '#gggggg', '"><script>', '#12345', '#1234567'):
        assert sanitize_cv_state({"edColor": bad})["edColor"] == "#444"


def test_a_missing_presentation_field_is_left_absent():
    """No key in, no key out — the renderer's own `v(id)||default` already
    handles absence safely; this module only touches a field that is present
    with a bad value, matching how `descHtml` behaves."""
    out = sanitize_cv_state({"iName": "Alice"})
    assert "pfFont" not in out
    assert out == {"iName": "Alice"}


def test_a_non_string_presentation_value_is_replaced():
    assert sanitize_cv_state({"pfFont": None})["pfFont"] == "'Source Sans 3',sans-serif"
    assert sanitize_cv_state({"pfWeight": 400})["pfWeight"] == "400"
    assert sanitize_cv_state({"pfColor": ["#fff"]})["pfColor"] == "#444"


def test_sanitize_cv_state_tolerates_missing_or_malformed_shape():
    assert sanitize_cv_state({}) == {}
    assert sanitize_cv_state({"expList": "not-a-list"}) == {"expList": "not-a-list"}
    out = sanitize_cv_state({"expList": [{"assignments": "not-a-list"}]})
    assert out["expList"][0]["assignments"] == "not-a-list"
    out2 = sanitize_cv_state({"expList": [{"assignments": [{"role": "x"}]}]})
    assert out2["expList"][0]["assignments"][0] == {"role": "x"}


def test_sanitize_cv_state_does_not_mutate_the_input():
    state = {"expList": [{"assignments": [{"descHtml": "<script>x</script>"}]}]}
    sanitize_cv_state(state)
    assert state["expList"][0]["assignments"][0]["descHtml"] == "<script>x</script>"
