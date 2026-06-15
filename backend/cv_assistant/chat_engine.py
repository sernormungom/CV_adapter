import logging
import re
from typing import Any

import anthropic

from backend.cv_assistant.profile_utils import load_profile

logger = logging.getLogger(__name__)

_MAX_HISTORY_TURNS = 10  # keep last 5 exchanges; system prompt always has fresh context

# ── Static instructions — cached on every call ───────────────────────────────
_STATIC_INSTRUCTIONS = """\
You are a CV Assistant embedded in a professional CV builder. You help consultants tune their CV for a specific job and keep their profile.yaml accurate.

You have two tools — call whichever apply; call both in the same response when a message warrants it:

  update_cv_fields       — Patch CV editor fields directly (ephemeral, scoped to this job only).
  propose_profile_update — Propose a lasting factual change to profile.yaml. Shown to the consultant as a confirm card; they must accept before it is written.

Guidelines:
1. profile.yaml is permanent factual memory. Any factual correction (wrong date, missing info, new skill) goes through propose_profile_update — never applied silently.
2. CV editor fields are ephemeral. Apply directly via update_cv_fields.
3. Answering a question → text only, no tools. Tuning CV text → update_cv_fields. Correcting a profile fact → propose_profile_update. Often both tools apply together.
4. When you call a tool, always include a brief text explanation before or after it so the consultant knows what changed.
5. The consultant can see the CV while you talk — do not quote large sections back at them.
6. For update_cv_fields, only use these field IDs: iName, iRole, iEmail, iPhone, iAvail, iSummary. Experience bullets and skills cannot be patched by field ID — suggest the change in text and/or propose a profile update.
7. When the consultant provides corrections for multiple gaps or facts, call propose_profile_update ONCE PER CHANGE — one card per correction. Include the specific object ID (block_id, role_group_id, gap_id) in the instruction field whenever you can identify it from the profile.
"""

# ── Two real tools — no fake "answer" tool ────────────────────────────────────
_TOOLS: list[dict] = [
    {
        "name": "update_cv_fields",
        "description": (
            "Patch one or more text fields in the CV editor. "
            "Valid field IDs: iName (full name), iRole (job title), iEmail, iPhone, "
            "iAvail (availability), iSummary (profile summary). "
            "Scoped to this job only — does not touch profile.yaml."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "Map of field_id -> new value string.",
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["fields"],
        },
    },
    {
        "name": "propose_profile_update",
        "description": (
            "Propose a lasting factual change to profile.yaml. "
            "The consultant sees a confirm card and must accept before it is written. "
            "Use for: correcting dates, adding missing contact info, recording new skills or experience, resolving gaps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Human-readable description shown on the confirm card.",
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "Precise instruction for the profile writer. "
                        "Specify: what field/object to modify, the exact new value, "
                        "and the block_id / role_group_id / gap_id if known."
                    ),
                },
            },
            "required": ["description", "instruction"],
        },
    },
]


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def _format_cv_state(cv_state: dict) -> str:
    """Compact text snapshot of the current CV editor fields — goes in the user message."""
    lines: list[str] = []

    for fid, label in [
        ("iName", "Name"), ("iRole", "Job title"), ("iEmail", "Email"),
        ("iPhone", "Phone"), ("iAvail", "Availability"),
    ]:
        val = cv_state.get(fid, "")
        if val:
            lines.append(f"{label}: {val}")

    summary = cv_state.get("iSummary", "")
    if summary:
        lines.append(f"Profile summary:\n{summary}")

    skills = cv_state.get("skills", {})
    for kind in ("main", "other", "tool"):
        items = skills.get(kind, [])
        if items:
            lines.append(f"Skills ({kind}): {', '.join(str(i) for i in items)}")

    for exp in (cv_state.get("expList") or [])[:6]:
        company = exp.get("company", "")
        loc = exp.get("location", "")
        frm, to_ = exp.get("from", ""), exp.get("to", "")
        period = f"{frm}–{to_}" if frm or to_ else ""
        header = company + (f", {loc}" if loc else "") + (f" ({period})" if period else "")
        lines.append(f"\nEmployer: {header}")
        for assign in (exp.get("assignments") or [])[:4]:
            role = assign.get("role", "")
            ap = assign.get("period", "")
            lines.append(f"  Role: {role}" + (f" ({ap})" if ap else ""))
            desc = _strip_html(assign.get("descHtml", ""))[:400]
            if desc:
                lines.append(f"  Description: {desc}")
            tools_str = assign.get("tools", "")
            if tools_str:
                lines.append(f"  Tools: {tools_str}")

    lang_list = cv_state.get("langList") or []
    if lang_list:
        langs = ", ".join(
            f"{l.get('name','')} ({l.get('level','')})" for l in lang_list if l.get("name")
        )
        if langs:
            lines.append(f"\nLanguages: {langs}")

    return "\n".join(lines) if lines else "(empty — no fields filled yet)"


def _build_system(profile_yaml: str | None, job_description: str) -> list[dict]:
    """
    Returns the system prompt as a list of blocks, structured for prompt caching.

    Block 0 — static instructions + profile (stable per session) → cached.
    Block 1 — job description (changes per job, not per message) → not cached.

    Separating them means a job change only invalidates block 1 while
    block 0 (the large profile YAML) stays in cache.
    """
    if profile_yaml:
        profile_section = (
            "=== CONSULTANT PROFILE (profile.yaml) ===\n"
            + profile_yaml
            + "\n=== END PROFILE ==="
        )
    else:
        profile_section = (
            "=== CONSULTANT PROFILE ===\n"
            "(No profile found — the consultant has not yet imported a CV)\n"
            "=== END PROFILE ==="
        )

    cached_block = {
        "type": "text",
        "text": _STATIC_INSTRUCTIONS + "\n\n" + profile_section,
        "cache_control": {"type": "ephemeral"},
    }

    job_text = job_description.strip()
    job_block = {
        "type": "text",
        "text": (
            "=== TARGET JOB DESCRIPTION ===\n"
            + (job_text[:3000] if job_text else "(None — CV not yet linked to a specific job)")
            + "\n=== END JOB DESCRIPTION ==="
        ),
    }

    return [cached_block, job_block]


async def chat(
    username: str,
    message: str,
    history: list[dict],
    cv_state: dict,
    job_description: str,
) -> dict[str, Any]:
    """
    Process one chat turn.
    Returns {answer: str, cv_patch?: dict, profile_update?: dict}.
    `answer` is always present.
    """
    yaml_text, _ = load_profile(username)
    system = _build_system(yaml_text, job_description)

    # Cap history to avoid unbounded context growth; fresh system prompt preserves factual accuracy.
    capped = (history or [])[-_MAX_HISTORY_TURNS:]
    messages: list[dict] = [
        {"role": t["role"], "content": t["content"]}
        for t in capped
        if t.get("role") in ("user", "assistant") and t.get("content")
    ]

    # CV state goes in the user message — it changes too frequently to cache.
    cv_state_block = (
        "=== CURRENT CV FIELDS ===\n"
        + _format_cv_state(cv_state)
        + "\n=== END CV FIELDS ===\n\n"
    )
    messages.append({"role": "user", "content": cv_state_block + message})

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system,
        tools=_TOOLS,
        tool_choice={"type": "auto"},
        messages=messages,
    )

    logger.info(
        "Chat [%s] stop_reason=%s usage=%s",
        username,
        response.stop_reason,
        response.usage,
    )

    # Parse content: text blocks → answer, tool_use blocks → actions.
    answer_parts: list[str] = []
    cv_patch: dict[str, str] = {}
    profile_updates: list[dict] = []

    for block in response.content:
        if block.type == "text":
            answer_parts.append(block.text)

        elif block.type == "tool_use":
            inp = block.input or {}

            if block.name == "update_cv_fields":
                fields = {k: str(v) for k, v in (inp.get("fields") or {}).items()}
                if fields:
                    cv_patch.update(fields)

            elif block.name == "propose_profile_update":
                profile_updates.append({
                    "description": inp.get("description", ""),
                    "patch": {"instruction": inp.get("instruction", "")},
                })

    answer_text = " ".join(answer_parts).strip()

    # Synthesize a fallback answer if the model only called tools without text.
    if not answer_text:
        n = len(profile_updates)
        if cv_patch and n:
            answer_text = "Done — I've updated the CV and proposed profile changes above."
        elif cv_patch:
            answer_text = "Done — I've updated the CV fields."
        elif n > 1:
            answer_text = f"I've proposed {n} profile updates — review the cards above."
        elif n == 1:
            answer_text = "I've proposed a profile update — review the card above."
        else:
            answer_text = "Done."

    result: dict[str, Any] = {"answer": answer_text}
    if cv_patch:
        result["cv_patch"] = cv_patch
    if profile_updates:
        result["profile_updates"] = profile_updates

    return result
