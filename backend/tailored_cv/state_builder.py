"""Generated content + the base CV's presentation → a complete `cv_state`.

The browser used to do this. `POST /api/generate-cv` returned `form_data` and
the editor called `populateFromImport(form_data)` and then saved whatever the
form then held — so the *client* was what turned a generation into a stored CV.
That cannot survive Slice 3: the generation now finishes minutes later in a
background runner with no browser attached, and the artifact has to exist
whether or not anyone has the editor open.

So this mirrors `populateFromImport` (`html/cv-importer.html`), field for field,
including the two rules that make it non-destructive:

- **an empty generated value keeps what was there** (the editor's `sv()` helper
  only assigns a non-empty value), and
- **an empty generated list keeps the existing list** (`if(d.experience && …)`).

The "what was there" is the consultant's base CV state, carried in the
generation's input snapshot. That is what gives a tailored CV the consultant's
fonts, colours, footer, and photo instead of a blank template — and it is a
*copy*: nothing here writes back to the base artifact (P-009).

Bullets become the assignment's rich-text HTML, as in the editor. Unlike the
editor, the bullet text is HTML-escaped: it is LLM output, i.e. text, and a
posting containing `<` or `&` would otherwise reach the renderer as markup.
"""

from __future__ import annotations

import html
from typing import Any

#: Presentation and identity fields that belong to the consultant rather than to
#: any one position. Copied from the base CV so a tailored CV opens looking like
#: their CV. Content fields are deliberately absent — the generation owns those.
_CARRIED_FROM_BASE = (
    "pfFont", "pfSize", "pfColor", "pfWeight",
    "skFont", "skSize", "skColor", "skWeight",
    "exFont", "exSize", "exColor", "exWeight",
    "edFont", "edSize", "edColor", "edWeight",
    "iFtText", "iFtEmail", "iFtPhone",
    "photoSrc",
)

#: Text fields the generation may fill. An empty generated value leaves the base
#: value in place, matching the editor's `sv()`.
_TEXT_FIELDS = ("iName", "iRole", "iEmail", "iPhone", "iAvail", "iSummary")

_SKILL_BUCKETS = ("main", "other", "tool")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bullets_to_html(bullets: list) -> str:
    items = [html.escape(_text(b), quote=False) for b in bullets or []]
    items = [b for b in items if b.strip()]
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{b}</li>" for b in items) + "</ul>"


def _assignment(source: dict) -> dict:
    return {
        "role": _text(source.get("role")),
        "period": _text(source.get("period")),
        "descHtml": _bullets_to_html(source.get("bullets") or []),
        "tools": _text(source.get("tools")),
    }


def _experience(entries: list) -> list[dict]:
    return [
        {
            "company": _text(entry.get("company")),
            "location": _text(entry.get("location")),
            "from": _text(entry.get("from")),
            "to": _text(entry.get("to")),
            "assignments": [
                _assignment(a) for a in (entry.get("assignments") or [])
            ],
        }
        for entry in entries or []
    ]


def _education(value: Any) -> list[dict]:
    """The editor accepts either a single education block or a list of them."""
    if isinstance(value, dict):
        entries = [value] if any(_text(v).strip() for v in value.values()) else []
    elif isinstance(value, list):
        entries = value
    else:
        entries = []
    return [
        {
            "degree": _text(entry.get("degree")),
            "institution": _text(entry.get("institution")),
            "from": _text(entry.get("from")),
            "to": _text(entry.get("to")),
            "description": _text(entry.get("description")),
        }
        for entry in entries
    ]


def build_cv_state(form_data: dict, base_state: dict | None = None) -> dict:
    """A complete editor `cv_state` for a generated CV.

    `base_state` is the consultant's base CV as it stood when the generation was
    approved — read from the immutable input snapshot, never from disk, and
    never written back.
    """
    base = dict(base_state or {})
    state: dict[str, Any] = {}

    for field in _CARRIED_FROM_BASE:
        if field in base:
            state[field] = base[field]
    # `photoSrc` must be a stored reference, never an inline data: URL — the
    # editor applies the same rule when it captures state.
    photo = state.get("photoSrc")
    state["photoSrc"] = (
        photo if isinstance(photo, str) and not photo.startswith("data:") else None
    )

    for field in _TEXT_FIELDS:
        generated = _text(form_data.get(field)).strip()
        state[field] = generated or _text(base.get(field))

    skills_in = form_data.get("skills") or {}
    base_skills = base.get("skills") or {}
    state["skills"] = {
        bucket: (
            list(skills_in.get(bucket) or [])
            or list(base_skills.get(bucket) or [])
        )
        for bucket in _SKILL_BUCKETS
    }

    experience = _experience(form_data.get("experience") or [])
    state["expList"] = experience or list(base.get("expList") or [])

    languages = [
        {"name": _text(l.get("name")), "level": _text(l.get("level"))}
        for l in (form_data.get("languages") or [])
    ]
    state["langList"] = languages or list(base.get("langList") or [])

    courses = [
        {"name": c if isinstance(c, str) else _text((c or {}).get("name"))}
        for c in (form_data.get("courses") or [])
    ]
    courses = [c for c in courses if c["name"].strip()]
    state["courseList"] = courses or list(base.get("courseList") or [])

    education = _education(form_data.get("education"))
    state["eduList"] = education or list(base.get("eduList") or [])

    return state
