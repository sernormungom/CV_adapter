"""The context-routing documentation must point at files that exist.

`CLAUDE.md`'s symptom index, `AGENTS.md`'s subsystem-contract pointer, and
`docs/architecture/areas/README.md` exist so a session loads one subsystem
contract instead of the whole encyclopedia. A routing entry that points at a
moved or renamed file silently returns the reader to "search everything",
which is the failure this structure exists to prevent — and nothing else in
the suite would notice.

Deliberately narrow: link targets and orphan area files only. It does not pin
prose, headings, section names, or document length, so ordinary documentation
improvement never has to route around a test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AREAS_DIR = _REPO_ROOT / "docs" / "architecture" / "areas"

# [text](target) — skip external schemes and pure in-page anchors.
_MD_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_EXTERNAL = ("http://", "https://", "mailto:", "#")

_ROUTING_FILES = [
    _REPO_ROOT / "CLAUDE.md",
    _REPO_ROOT / "AGENTS.md",
    *sorted(_AREAS_DIR.glob("*.md")),
]


def _link_targets(path: Path) -> list[str]:
    targets = []
    for raw in _MD_LINK.findall(path.read_text(encoding="utf-8")):
        target = raw.strip()
        if target.startswith(_EXTERNAL):
            continue
        targets.append(target.split("#", 1)[0].rstrip())
    return [t for t in targets if t]


@pytest.mark.parametrize("source", _ROUTING_FILES, ids=lambda p: p.name)
def test_every_relative_documentation_link_resolves(source: Path) -> None:
    broken = []
    for target in _link_targets(source):
        # Relative to the linking file first, then to the repository root:
        # root-level files legitimately write repo-relative paths.
        if (source.parent / target).exists() or (_REPO_ROOT / target).exists():
            continue
        broken.append(target)
    assert not broken, (
        f"{source.relative_to(_REPO_ROOT)} links to missing path(s): {broken}. "
        "Context routing that points nowhere sends the reader back to reading "
        "everything."
    )


def test_every_area_contract_is_reachable_from_the_routing_index() -> None:
    index = _AREAS_DIR / "README.md"
    listed = index.read_text(encoding="utf-8")
    orphans = [
        doc.name
        for doc in sorted(_AREAS_DIR.glob("*.md"))
        if doc.name != "README.md" and doc.name not in listed
    ]
    assert not orphans, (
        f"area contract(s) {orphans} exist but are not listed in "
        "docs/architecture/areas/README.md, so no routing path reaches them."
    )
