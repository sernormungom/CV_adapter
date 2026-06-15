"""
Central profile reader.

All code that needs a consultant profile calls this module.
Nothing reads profile.md directly except the Writer (which owns the stored format).

    load_profile(consultant_id)  →  cleaned dict
    profile_to_yaml(data)        →  YAML string for LLM prompt injection

Cleaning applied at read time (not stored):
  - All *_id fields removed — Writer-only references, not signal for any consumer
  - Block-level `industries` suppressed when identical to the parent org's industries,
    avoiding repetition for direct employment while preserving the distinction that
    matters for consultants (client industry differs from the employer's industry)
"""

import re
from pathlib import Path

import yaml

from backend.config import DATA_DIR

_FENCE_RE = re.compile(r"```yaml[ \t]*\n(.*?)```", re.DOTALL)

_ID_KEYS = frozenset({"education_id", "role_group_id", "block_id", "evidence_id", "gap_id"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_profile(consultant_id: str) -> dict:
    """
    Load, parse, and clean a consultant's profile.md.

    Raises FileNotFoundError if the profile does not exist.
    Raises ValueError if the YAML fence or mapping is missing.
    """
    profile_path = DATA_DIR / consultant_id / "profile.md"
    if not profile_path.exists():
        raise FileNotFoundError(f"profile.md not found for consultant: {consultant_id}")

    md = profile_path.read_text(encoding="utf-8")
    m = _FENCE_RE.search(md)
    if not m:
        raise ValueError(f"No ```yaml fence in profile.md for consultant: {consultant_id}")

    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise ValueError(f"Profile YAML is not a mapping for consultant: {consultant_id}")

    return _clean(data)


def profile_to_yaml(data: dict) -> str:
    """Serialize a cleaned profile dict to YAML for LLM prompt injection."""
    return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)


# ---------------------------------------------------------------------------
# Cleaning (pure, no I/O)
# ---------------------------------------------------------------------------

def _strip_ids(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _ID_KEYS}


def _clean_evidence_item(ev: dict) -> dict:
    return _strip_ids(ev)


def _clean_block(block: dict, org_industries: frozenset) -> dict:
    block = _strip_ids(block)

    block_industries = frozenset(block.get("industries") or [])
    if block_industries and block_industries == org_industries:
        block = {k: v for k, v in block.items() if k != "industries"}

    if "evidence_items" in block:
        block["evidence_items"] = [_clean_evidence_item(ev) for ev in block["evidence_items"]]

    return block


def _clean_role_group(rg: dict) -> dict:
    rg = _strip_ids(rg)
    org_industries = frozenset((rg.get("organization") or {}).get("industries") or [])

    if "blocks" in rg:
        rg["blocks"] = [_clean_block(b, org_industries) for b in rg["blocks"]]

    return rg


def _clean(data: dict) -> dict:
    if "education" in data:
        data["education"] = [_strip_ids(edu) for edu in data["education"]]

    ch = data.get("career_history")
    if isinstance(ch, dict) and "role_groups" in ch:
        data["career_history"] = {
            **ch,
            "role_groups": [_clean_role_group(rg) for rg in ch["role_groups"]],
        }

    if "gaps" in data:
        data["gaps"] = [_strip_ids(gap) for gap in data["gaps"]]

    return data
