"""
Migration 1.1 → 1.2  —  Remove provenance, keywords, tools_referenced.

Run from the project root:
    python scripts/migrate_profile_1_1_to_1_2.py
    python scripts/migrate_profile_1_1_to_1_2.py --dry-run   # preview only

What it does per profile.md found under data/profiles/:
  - Removes provenance objects from role_groups and blocks
  - Promotes provenance.reviewed_by_consultant: true → verified: true on the block
  - Removes keywords and tools_referenced from all evidence_items
  - Bumps metadata.schema_version to "1.2"

Safe to re-run: profiles already at 1.2 are skipped.
A .bak file is written next to each changed profile before modifying it.
"""

import re
import shutil
import sys
from pathlib import Path

import yaml

SOURCE_VERSION = "1.1"
TARGET_VERSION = "1.2"
DATA_DIR = Path("data/profiles")


# ---------------------------------------------------------------------------
# Migration logic (pure functions, no I/O)
# ---------------------------------------------------------------------------

def _migrate_evidence_items(items: list) -> list:
    result = []
    for ev in items or []:
        ev = dict(ev)
        ev.pop("keywords", None)
        ev.pop("tools_referenced", None)
        result.append(ev)
    return result


def _migrate_block(block: dict) -> dict:
    block = dict(block)
    prov = block.pop("provenance", None)
    if isinstance(prov, dict) and prov.get("reviewed_by_consultant") is True:
        block["verified"] = True
    if "evidence_items" in block:
        block["evidence_items"] = _migrate_evidence_items(block["evidence_items"])
    return block


def _migrate_role_group(rg: dict) -> dict:
    rg = dict(rg)
    rg.pop("provenance", None)
    if "blocks" in rg:
        rg["blocks"] = [_migrate_block(b) for b in rg["blocks"] or []]
    return rg


def migrate_data(data: dict) -> dict:
    """Apply the 1.1 → 1.2 migration to a parsed profile dict. Returns a new dict."""
    data = dict(data)

    if isinstance(data.get("metadata"), dict):
        data["metadata"] = {**data["metadata"], "schema_version": TARGET_VERSION}

    ch = data.get("career_history")
    if isinstance(ch, dict) and "role_groups" in ch:
        data["career_history"] = {
            **ch,
            "role_groups": [_migrate_role_group(rg) for rg in ch["role_groups"] or []],
        }

    return data


# ---------------------------------------------------------------------------
# YAML fence extraction and reassembly
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"(```yaml[ \t]*\n)(.*?)([ \t]*```)", re.DOTALL)


def _split_fence(md: str):
    """Return (before, yaml_text, after) or None if no fence found."""
    m = _FENCE_RE.search(md)
    if not m:
        return None
    before = md[: m.start()] + m.group(1)
    after = m.group(3) + md[m.end() :]
    return before, m.group(2), after


def _reassemble(before: str, yaml_text: str, after: str) -> str:
    return before + yaml_text + after


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(path: Path, dry_run: bool) -> str:
    """
    Process one profile.md.
    Returns one of: "migrated", "skipped_version", "skipped_no_fence",
                    "skipped_parse_error", "skipped_already_migrated".
    """
    md = path.read_text(encoding="utf-8")

    parts = _split_fence(md)
    if parts is None:
        return "skipped_no_fence"

    before, yaml_text, after = parts

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return "skipped_parse_error"

    if not isinstance(data, dict):
        return "skipped_parse_error"

    version = (data.get("metadata") or {}).get("schema_version")
    if version == TARGET_VERSION:
        return "skipped_already_migrated"
    if version != SOURCE_VERSION:
        return "skipped_version"

    migrated_data = migrate_data(data)
    new_yaml = yaml.dump(
        migrated_data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    new_md = _reassemble(before, new_yaml, after)

    if not dry_run:
        bak = path.with_suffix(".md.bak")
        shutil.copy2(path, bak)
        path.write_text(new_md, encoding="utf-8")

    return "migrated"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> None:
    profiles = sorted(DATA_DIR.glob("*/profile.md"))
    if not profiles:
        print(f"No profile.md files found under {DATA_DIR}/")
        return

    if dry_run:
        print("DRY RUN — no files will be changed.\n")

    counts: dict[str, int] = {}
    for path in profiles:
        status = process_file(path, dry_run=dry_run)
        counts[status] = counts.get(status, 0) + 1

        label = path.parent.name
        if status == "migrated":
            note = "(preview)" if dry_run else "(backup written)"
            print(f"  migrated  {label}  {note}")
        elif status == "skipped_already_migrated":
            print(f"  already 1.2  {label}")
        elif status == "skipped_no_fence":
            print(f"  WARN  {label}: no ```yaml fence found")
        elif status == "skipped_parse_error":
            print(f"  WARN  {label}: YAML parse error — skipped")
        elif status == "skipped_version":
            v = "(unknown)"
            try:
                import re as _re
                md = path.read_text(encoding="utf-8")
                m = _re.search(r"schema_version:\s*['\"]?(\S+?)['\"]?\s*$", md, _re.M)
                if m:
                    v = m.group(1)
            except Exception:
                pass
            print(f"  SKIP  {label}: schema_version {v} (expected {SOURCE_VERSION})")

    migrated = counts.get("migrated", 0)
    total = len(profiles)
    verb = "would be migrated" if dry_run else "migrated"
    print(f"\n{migrated}/{total} profile(s) {verb}.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
