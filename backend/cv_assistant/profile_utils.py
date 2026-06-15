"""Shared utilities for reading, parsing, and writing profile.md."""
import uuid
from pathlib import Path

import yaml

from backend.config import DATA_DIR


def load_profile(username: str) -> tuple[str | None, Path]:
    """Return (raw YAML text, profile_path). Text is None if file absent or unparseable."""
    profile_path = DATA_DIR / username / "profile.md"
    if not profile_path.exists():
        return None, profile_path
    text = profile_path.read_text(encoding="utf-8")
    start = text.find("```yaml\n")
    end = text.rfind("\n```")
    if start == -1 or end == -1:
        return None, profile_path
    return text[start + 8 : end], profile_path


def parse_yaml(yaml_text: str) -> dict | None:
    try:
        data = yaml.safe_load(yaml_text)
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def write_profile(profile_path: Path, data: dict) -> None:
    # Compact resolved gaps — strip verbose fields, keep only gap_id + status.
    for gap in data.get("gaps", []):
        if gap.get("status") == "resolved":
            gap.pop("description", None)
            gap.pop("suggested_question", None)
            gap.pop("target_ref", None)
            gap.pop("kind", None)
            gap.pop("severity", None)

    full_name = data.get("identity", {}).get("full_name", "Consultant")
    yaml_block = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    profile_path.write_text(
        f"# Consultant Profile — {full_name}\n\n```yaml\n{yaml_block}```\n",
        encoding="utf-8",
    )


def assign_new_ids(data: dict) -> dict:
    """Mint IDs for any new objects whose ID fields are blank or absent."""
    def _new() -> str:
        return uuid.uuid4().hex[:8]

    for edu in data.get("education", []):
        if not edu.get("education_id"):
            edu["education_id"] = f"e_{_new()}"
    for rg in (data.get("career_history") or {}).get("role_groups", []):
        if not rg.get("role_group_id"):
            rg["role_group_id"] = f"rg_{_new()}"
        for block in rg.get("blocks", []):
            if not block.get("block_id"):
                block["block_id"] = f"b_{_new()}"
            for ev in block.get("evidence_items", []):
                if not ev.get("evidence_id"):
                    ev["evidence_id"] = f"ev_{_new()}"
    for gap in data.get("gaps", []):
        if not gap.get("gap_id"):
            gap["gap_id"] = f"g_{_new()}"
    return data
