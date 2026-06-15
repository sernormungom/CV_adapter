"""
Apply targeted, consultant-confirmed changes to profile.yaml.

Design: instead of asking the LLM to regenerate the full YAML, we give it a small
set of structured tools (patch_field, add_evidence_item, resolve_gap). The LLM reads
the profile for context, calls the appropriate tools, and Python applies each operation
deterministically. The profile YAML is never regenerated wholesale.
"""
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

from backend.cv_assistant.profile_utils import (
    assign_new_ids,
    load_profile,
    parse_yaml,
    write_profile,
)

logger = logging.getLogger(__name__)

# ── Condensed vocabulary reference for the patcher ────────────────────────────
_SCHEMA_VOCAB = """\
Key constraints when writing field values:
- date fields (started, ended, as_of): "YYYY-MM" format, or "present" for ended only.
- evidence_type (closed): responsibility | achievement | impact | artifact | decision
- employment_type (closed): permanent | fixed_term | contract | consultant_via_employer | freelance | internship | academic | personal_project | open_source | other
- block_type (closed): technical_delivery | technical_leadership | people_management | project_management | innovation | teaching_mentoring | research | other
- seniority (closed): junior | mid | senior | staff | principal | lead | manager | director | executive
- gap status (closed): open | resolved | declined
- IDs for NEW objects must be "" (the writer assigns them). NEVER invent IDs.
- evidence_item text: past tense, ≤60 words, factual — no editorial language.
"""

_SYSTEM_PROMPT = f"""\
You are a profile editor. You receive the current profile.yaml and a change instruction. Apply the instruction by calling the appropriate tool(s). You may call multiple tools if the instruction requires it.

{_SCHEMA_VOCAB}
Rules:
- Call only the tools needed to carry out the instruction. Do not improve or restructure anything else.
- If the instruction resolves a gap, call resolve_gap in addition to the field patch.
- If a field you need to set does not exist yet, create it with the correct schema structure.
- Do not call a tool for something the instruction does not ask for.
"""

# ── Structured patch tools ─────────────────────────────────────────────────────
_PATCH_TOOLS: list[dict] = [
    {
        "name": "patch_field",
        "description": (
            "Update a single field on a profile object. "
            "object_type='identity': identity fields (full_name, email, phone, preferred_name, location.city, location.country, location.country_code, etc.). "
            "object_type='role_group': use the role_group_id. "
            "object_type='block': use the block_id. "
            "object_type='education': use the education_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {
                    "type": "string",
                    "enum": ["identity", "role_group", "block", "education"],
                },
                "object_id": {
                    "type": "string",
                    "description": "The ID of the object. Use '' for identity (there is only one).",
                },
                "field": {
                    "type": "string",
                    "description": "Field name. Use dot notation for nested fields, e.g. 'location.city'.",
                },
                "value": {
                    "description": "New value. Use the correct type per schema (string, list, object)."
                },
            },
            "required": ["object_type", "object_id", "field", "value"],
        },
    },
    {
        "name": "add_evidence_item",
        "description": "Append a new evidence item to an existing block.",
        "input_schema": {
            "type": "object",
            "properties": {
                "block_id": {"type": "string"},
                "evidence_type": {
                    "type": "string",
                    "enum": ["responsibility", "achievement", "impact", "artifact", "decision"],
                },
                "text": {
                    "type": "string",
                    "description": "The claim, past tense, ≤60 words, factual.",
                },
            },
            "required": ["block_id", "evidence_type", "text"],
        },
    },
    {
        "name": "resolve_gap",
        "description": "Mark a gap as resolved after the consultant has provided the missing information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "gap_id": {"type": "string"},
            },
            "required": ["gap_id"],
        },
    },
]


# ── Deterministic applicators ──────────────────────────────────────────────────

def _set_nested(obj: dict, path: str, value: Any) -> None:
    """Set a dot-separated path on a dict, creating intermediary dicts as needed."""
    parts = path.split(".")
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = value


def _apply_patch_field(
    profile: dict, object_type: str, object_id: str, field: str, value: Any
) -> bool:
    if object_type == "identity":
        _set_nested(profile.setdefault("identity", {}), field, value)
        return True

    if object_type == "role_group":
        for rg in (profile.get("career_history") or {}).get("role_groups", []):
            if rg.get("role_group_id") == object_id:
                _set_nested(rg, field, value)
                return True
        logger.warning("patch_field: role_group id=%s not found", object_id)
        return False

    if object_type == "block":
        for rg in (profile.get("career_history") or {}).get("role_groups", []):
            for block in rg.get("blocks", []):
                if block.get("block_id") == object_id:
                    _set_nested(block, field, value)
                    return True
        logger.warning("patch_field: block id=%s not found", object_id)
        return False

    if object_type == "education":
        for edu in profile.get("education", []):
            if edu.get("education_id") == object_id:
                _set_nested(edu, field, value)
                return True
        logger.warning("patch_field: education id=%s not found", object_id)
        return False

    return False


def _apply_add_evidence(
    profile: dict, block_id: str, evidence_type: str, text: str
) -> bool:
    for rg in (profile.get("career_history") or {}).get("role_groups", []):
        for block in rg.get("blocks", []):
            if block.get("block_id") == block_id:
                block.setdefault("evidence_items", []).append(
                    {"evidence_id": "", "type": evidence_type, "text": text}
                )
                return True
    logger.warning("add_evidence_item: block id=%s not found", block_id)
    return False


def _apply_resolve_gap(profile: dict, gap_id: str) -> bool:
    for gap in profile.get("gaps", []):
        if gap.get("gap_id") == gap_id:
            gap["status"] = "resolved"
            return True
    logger.warning("resolve_gap: gap id=%s not found", gap_id)
    return False


# ── Main entry point ───────────────────────────────────────────────────────────

async def apply_profile_patch(username: str, patch: dict[str, Any]) -> dict[str, Any]:
    """
    Apply a profile change described by patch["instruction"].
    Returns {"ok": True} or {"ok": False, "error": str}.
    """
    instruction = (patch.get("instruction") or "").strip()
    if not instruction:
        return {"ok": False, "error": "No instruction provided"}

    yaml_text, profile_path = load_profile(username)
    if not yaml_text:
        return {"ok": False, "error": "Profile not found for this user"}

    profile = parse_yaml(yaml_text)
    if profile is None:
        return {"ok": False, "error": "Could not parse existing profile YAML"}

    user_message = (
        f"Current profile.yaml (read-only context — do NOT regenerate it, only call tools):\n\n"
        f"{yaml_text}\n\n"
        f"Instruction: {instruction}"
    )

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=_SYSTEM_PROMPT,
        tools=_PATCH_TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": user_message}],
    )

    logger.info(
        "Profile patcher [%s] tool_calls=%d",
        username,
        sum(1 for b in response.content if b.type == "tool_use"),
    )

    # Apply each tool call deterministically.
    applied: int = 0
    for block in response.content:
        if block.type != "tool_use":
            continue
        inp = block.input or {}

        if block.name == "patch_field":
            ok = _apply_patch_field(
                profile,
                inp.get("object_type", ""),
                inp.get("object_id", ""),
                inp.get("field", ""),
                inp.get("value"),
            )
            if ok:
                applied += 1

        elif block.name == "add_evidence_item":
            ok = _apply_add_evidence(
                profile,
                inp.get("block_id", ""),
                inp.get("evidence_type", "responsibility"),
                inp.get("text", ""),
            )
            if ok:
                applied += 1

        elif block.name == "resolve_gap":
            ok = _apply_resolve_gap(profile, inp.get("gap_id", ""))
            if ok:
                applied += 1

    if applied == 0:
        logger.error("Profile patcher made no changes for %s (instruction: %s)", username, instruction)
        return {"ok": False, "error": "Patcher made no changes — check the instruction and IDs"}

    # Stamp updated_at, assign IDs for any new objects, persist.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    profile.setdefault("metadata", {})["updated_at"] = now

    assign_new_ids(profile)
    write_profile(profile_path, profile)

    logger.info("Profile patched (%d operation(s)) for %s", applied, username)
    return {"ok": True}
