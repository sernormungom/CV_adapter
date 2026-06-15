import logging
from pathlib import Path

import anthropic
import yaml

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "project_context" / "consultant-profile.schema.md"

_MERGE_SYSTEM = """\
You are merging two consultant profile datasets into one canonical profile.
You receive the EXISTING profile (already structured, possibly consultant-reviewed) and a NEW EXTRACTION
from a second uploaded CV. Your job is to produce a single enriched YAML that is strictly richer than
either source — never losing information that exists in the existing profile.

=== CONSULTANT PROFILE SCHEMA (v1.3) ===
{schema}
=== END SCHEMA ===

MERGE RULES:

IDENTITY:
- Start from the existing values as the baseline.
- If the new CV provides a value for a field that already exists AND the values differ (e.g. different
  email, phone, LinkedIn URL, or location), take the NEW value in the output.
- For every such conflict, add a gap entry: kind=data_conflict, severity=valuable, status=open, gap_id="",
  with a suggested_question asking the consultant to confirm which value is correct and note both values.
- If the new CV supplies a field that was absent in the existing profile (e.g. LinkedIn was missing), add
  it without a conflict gap.

EDUCATION:
- Match entries by (institution + qualification). Same institution + same qualification = same entry.
- For matched entries: keep the existing education_id. Enrich description or dates if the new CV is more
  specific (e.g. adds a month that was previously unknown).
- For new entries not in the existing profile: add them with education_id: "" (the writer assigns IDs).
- Never drop an education entry that exists in the current profile.

CAREER HISTORY — role_groups:
- Match role_groups by organization name similarity AND date range overlap. Do NOT match solely by
  display_role_title — titles vary between CVs.
- For MATCHED role_groups:
  - Keep the existing role_group_id.
  - Keep the existing display_role_title unless it is clearly a placeholder (e.g. "unknown" or blank).
  - Merge the date range: use the widest span that is supported by both sources.
- For NEW role_groups not present in the existing profile: add wholesale with role_group_id: "".

CAREER HISTORY — blocks within a matched role_group:
- For consultant_via_employer role_groups: match blocks using client.name + role_title similarity
  (not date range alone). Two blocks with the same client.name and same role_title whose date ranges
  OVERLAP or are CONTIGUOUS (one's ended equals or is within one month of the other's started) represent
  the same engagement — treat as a CONTINUATION: keep the existing block_id, extend ended to the later
  date, union all fields, do NOT create a new block.
- For all other role_groups: match blocks by date range overlap within the same role_group.
- For MATCHED blocks (either rule above):
  - Keep the existing block_id.
  - Union tools, languages, domains, processes_standards, verification_validation — add items from the
    new extraction that are not already present; no duplicates.
  - Union evidence_items — add only items that are NOT semantically equivalent to an existing item.
    Preserve all existing evidence_ids exactly. New evidence_items get evidence_id: "".
- For NEW blocks within a matched role_group (genuinely new client or date range): add with block_id: "",
  evidence_id: "".

GAPS:
- Keep ALL existing gaps as-is (do not alter gap_id, status, or content of existing entries).
- Add new gaps from the new extraction only if they are not already covered by an existing gap
  (match by kind + similar description).
- New gaps get gap_id: "".

OUTPUT RULES:
1. Return ONLY raw YAML — no markdown fences, no commentary, no preamble.
2. Emit ONLY these top-level keys: identity, education, career_history, gaps.
   Do NOT emit: metadata, preferences (those are system/consultant-owned).
3. Preserve ALL existing IDs exactly as they appear. Assign "" to any ID field on new items.
4. Dates: YYYY-MM format. Use "present" for current positions only.
5. Do not invent or guess values. If uncertain, omit and add a gap entry.
"""


async def merge_profiles(
    existing_profile: dict,
    new_extraction: dict,
) -> tuple[dict | None, list[dict]]:
    """
    Merge new_extraction into existing_profile.
    Returns (merged_data, errors) where merged_data contains only
    identity/education/career_history/gaps (no metadata/preferences).
    """
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    system_prompt = _MERGE_SYSTEM.format(schema=schema)

    existing_data = {
        "identity": existing_profile.get("identity", {}),
        "education": existing_profile.get("education", []),
        "career_history": existing_profile.get("career_history", {}),
        "gaps": existing_profile.get("gaps", []),
    }

    existing_yaml = yaml.dump(existing_data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    new_yaml = yaml.dump(new_extraction, allow_unicode=True, sort_keys=False, default_flow_style=False)

    user_message = (
        "EXISTING PROFILE:\n"
        + existing_yaml
        + "\nNEW EXTRACTION (from newly uploaded CV):\n"
        + new_yaml
        + "\nProduce the merged profile YAML now."
    )

    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text.strip()
    logger.info("Merger LLM response (first 400 chars): %s", raw[:400])

    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner)

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.error("YAML parse failed in merger: %s\nRaw: %s", exc, raw[:800])
        return None, [{"kind": "parse_error", "context": str(exc)}]

    if not isinstance(data, dict):
        logger.error("Merger returned non-dict (%s). Raw: %s", type(data).__name__, raw[:400])
        return None, [{"kind": "parse_error", "context": f"Expected YAML mapping, got {type(data).__name__}"}]

    logger.info("Merge OK — top-level keys: %s", list(data.keys()))
    return data, []
