"""Server-side allowlist sanitizer for every state-derived value that reaches
rendered markup or an HTML attribute in a publication
(`docs/plans/v2-pilot.md` Slice 5: "minimize/sanitize content"; S5-P2 widened
this from the one rich-text field to every such value after `S5-P1-R2` found
the presentation fields were still an open injection surface).

Two surfaces, two techniques:

**`expList[*].assignments[*].descHtml`** is the CV editor's one
`contenteditable` rich-text field (bold/italic/underline/lists via
`document.execCommand`, see `html/cv-importer.html`); `html/cv-render.js`
interpolates it verbatim into `sheet.innerHTML`. `sanitize_rich_text` is a
from-scratch allowlist HTML sanitizer (stdlib `html.parser.HTMLParser`, no
third-party dependency): it gives a real (lenient) tokenizer, including the
CDATA content model for `<script>`/`<style>` (their content arrives as inert
`handle_data` text, not re-parsed as markup). Safety comes from the *output*
side, not from perfectly modeling a browser's parser: every emitted tag is a
literal string from `_ALLOWED_TAGS` that this module writes itself — the
original tag bytes and every attribute are always discarded, allowed or not —
and every text node is re-escaped with `html.escape()`. So the result can
only ever contain the fixed allowed tag vocabulary plus escaped text,
regardless of how the input parses.

**The twelve presentation fields** (`{pf,sk,ex,ed}{Font,Color,Weight}`) are a
different shape of the same problem: `html/cv-render.js`'s `ts(prefix)`
builds a `style="..."` attribute string from them with no escaping at all
(`font-family:${font};...;color:${color};...;font-weight:${weight}`), so a
crafted value like `x"><img src=x onerror="alert(1)"><span style="` breaks out
of the attribute and injects a live element (`S5-P1-R2` finding 1's worked
case). These are never free text in the real editor — each is set from a
fixed `<select>` (Font, Weight) or a native `<input type="color">` (Color;
always `#rrggbb`) — so the allowlist below is transcribed from those controls
(`html/cv-importer.html`'s `pfFont`/`skFont`/`exFont`/`edFont`,
`*Weight`, and the color-input pattern), not invented. `*Size` needs no
sanitizing: `ts()` reads it through `parseInt(...)||10`, which is already
injection-proof (it stops at the first non-digit character).

A value outside the allowlist is replaced with `ts()`'s own already-safe
default for a *missing* field, not refused: the only way a legitimate
tailored CV ever has an out-of-allowlist value is a value that predates this
sanitizer, and defaulting reuses behaviour the renderer already exercises for
an absent field, so it can never change how a genuinely curated CV renders
(every real value already lies inside the allowlist by construction) while
still turning an attack payload into an inert, on-brand fallback rather than
blocking the publish outright.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

#: Deliberately small: enough for the RTE's own output (bold/italic/underline/
#: lists), nothing that carries attributes or executes. No `<a>`, `<img>`,
#: `<svg>`, `<style>`, `<script>`, `<iframe>`, `<object>`, `<embed>` — and no
#: attributes on anything, including `style=`, which is how a color-picker
#: value could otherwise smuggle a CSS-based payload.
_ALLOWED_TAGS = frozenset(
    {"b", "i", "u", "strong", "em", "ul", "ol", "li", "br", "p", "div", "span"}
)
_VOID_TAGS = frozenset({"br"})


class _AllowlistSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        t = tag.lower()
        if t not in _ALLOWED_TAGS:
            return  # dropped: no tag, no attributes — inner text still flows
        self._out.append(f"<{t}>")
        if t not in _VOID_TAGS:
            self._open.append(t)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        t = tag.lower()
        if t not in _ALLOWED_TAGS:
            return
        self._out.append(f"<{t}>" if t in _VOID_TAGS else f"<{t}></{t}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t not in _ALLOWED_TAGS or t in _VOID_TAGS:
            return
        if t not in self._open:
            return  # stray close tag — nothing to close, drop it
        # Close back through any unbalanced opens, matching browser recovery.
        while self._open:
            top = self._open.pop()
            self._out.append(f"</{top}>")
            if top == t:
                break

    def handle_data(self, data: str) -> None:
        self._out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def sanitize_rich_text(raw: object) -> str:
    """Allowlist-sanitize one rich-text field. Non-string input becomes ``""``."""
    if not isinstance(raw, str) or not raw:
        return ""
    parser = _AllowlistSanitizer()
    parser.feed(raw)
    parser.close()
    return parser.result()


# ── Presentation fields: transcribed from html/cv-importer.html's own
# controls, not invented — see the module docstring. ──────────────────────

#: The `<select id="{prefix}Font">` options, identical across all four
#: presentation groups (`html/cv-importer.html` lines ~326-338, 383-395,
#: 425-437, 467-479).
_ALLOWED_FONTS = frozenset(
    {
        "'Source Sans 3',sans-serif",
        "'Source Sans 3',sans-serif|italic",
        "'Lato',sans-serif",
        "'Lato',sans-serif|italic",
        "'Raleway',sans-serif",
        "'Raleway',sans-serif|italic",
        "'Source Serif 4',serif",
        "'Source Serif 4',serif|italic",
        "'Playfair Display',serif",
        "'Playfair Display',serif|italic",
        "Georgia,serif",
        "Georgia,serif|italic",
    }
)
#: The `<select id="{prefix}Weight">` options (Light/Regular/Bold), identical
#: across all four groups.
_ALLOWED_WEIGHTS = frozenset({"300", "400", "600"})
#: `<input type="color">` always yields exactly this shape.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: `html/cv-render.js`'s `ts(prefix)` falls back to these exact values when a
#: field is empty/missing (`v(id)||default`) — reusing them here means an
#: out-of-allowlist value renders identically to "not set", not differently.
_DEFAULT_FONT = "'Source Sans 3',sans-serif"
_DEFAULT_COLOR = "#444"
_DEFAULT_WEIGHT = "400"

#: The four presentation groups `ts()` is called with.
_PRESENTATION_PREFIXES = ("pf", "sk", "ex", "ed")


def _sanitized_presentation_value(
    value: object, *, allowed: frozenset[str] | None, pattern: re.Pattern | None, default: str
) -> str:
    if isinstance(value, str):
        if allowed is not None and value in allowed:
            return value
        if pattern is not None and pattern.match(value):
            return value
    return default


def sanitize_cv_state(cv_state: dict) -> dict:
    """Return a copy of `cv_state` with every value the renderer places in
    markup or an HTML attribute allowlisted or context-safe.

    `descHtml` is the only field interpolated as raw HTML
    (`html/cv-render.js`'s `bodyHTML` line); the twelve presentation fields
    are interpolated unescaped into a `style="..."` attribute (`ts()`). Every
    other field in `cv_state` is plain text the renderer already runs through
    `esc()`, so sanitizing it here would be redundant, not additional safety.
    """
    exp_list = cv_state.get("expList")
    if isinstance(exp_list, list):
        new_exp_list = []
        for entry in exp_list:
            if not isinstance(entry, dict):
                new_exp_list.append(entry)
                continue
            assignments = entry.get("assignments")
            if isinstance(assignments, list):
                new_assignments = []
                for a in assignments:
                    if isinstance(a, dict) and "descHtml" in a:
                        a = {**a, "descHtml": sanitize_rich_text(a.get("descHtml"))}
                    new_assignments.append(a)
                entry = {**entry, "assignments": new_assignments}
            new_exp_list.append(entry)
        cv_state = {**cv_state, "expList": new_exp_list}

    updates = {}
    for prefix in _PRESENTATION_PREFIXES:
        font_key, color_key, weight_key = f"{prefix}Font", f"{prefix}Color", f"{prefix}Weight"
        if font_key in cv_state:
            updates[font_key] = _sanitized_presentation_value(
                cv_state.get(font_key), allowed=_ALLOWED_FONTS, pattern=None, default=_DEFAULT_FONT
            )
        if color_key in cv_state:
            updates[color_key] = _sanitized_presentation_value(
                cv_state.get(color_key), allowed=None, pattern=_COLOR_RE, default=_DEFAULT_COLOR
            )
        if weight_key in cv_state:
            updates[weight_key] = _sanitized_presentation_value(
                cv_state.get(weight_key), allowed=_ALLOWED_WEIGHTS, pattern=None, default=_DEFAULT_WEIGHT
            )
    if updates:
        cv_state = {**cv_state, **updates}
    return cv_state
