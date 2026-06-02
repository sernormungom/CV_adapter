from pathlib import Path
import anthropic
import yaml

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "project_context" / "consultant-profile.schema.md"

_SYSTEM_TEMPLATE = """\
You are an expert CV parser. Extract structured career data from the provided CV text and return it as valid YAML.

=== CONSULTANT PROFILE SCHEMA (v1.1) ===
{schema}
=== END SCHEMA ===

OUTPUT RULES:
1. Return ONLY raw YAML — no markdown fences, no commentary, no preamble.
2. Populate ONLY these top-level keys: identity, education, career_history, gaps.
   Do NOT emit: metadata (system-assigned), preferences (consultant-owned — never from CV).
3. Leave all ID fields blank ("") — the Writer assigns them: education_id, role_group_id, block_id, evidence_id, gap_id.
4. Dates: YYYY-MM format. Use "present" for current positions only.
5. FLAG-DON'T-GUESS (§4.4 of schema): When a REQUIRED field cannot be sourced:
   - Omit the field (do NOT guess or invent a value).
   - Set needs_review: true on the containing object.
   - Add a gap entry under the top-level `gaps` array with:
       gap_id: ""   (Writer assigns)
       kind: missing_required | ambiguous_dates | unattributed_skill | ...
       severity: blocking | valuable | minor
       target_ref: ""  (fill with role_group_id/block_id when known)
       description: "Human-readable description of what is missing"
       suggested_question: "Ready-to-ask question for the consultant"
       status: open
   Reserve errors (error: key) ONLY for truly unusable input (contradictory data that has no defensible reading, or a section that cannot be parsed at all).
6. Extract ALL work experience and ALL education entries — do not drop or summarize.
7. Each distinct organization = one role_group. Multiple roles at the same org = multiple blocks.
8. Apply COMPRESSION (§5.1 of schema):
   - Keep skeleton (org, role, dates) always.
   - Keep evidence atoms specific and verbatim in meaning — do NOT blur specific claims into vague summaries.
   - Drop reconstructable phrasing: polished CV sentences, boilerplate responsibilities ("covered the full lifecycle"), zero-value specifics.
   - Grade by recency: recent/core roles keep all atoms; old/minor roles compress toward skeleton + a summary line.
   - Distribute flat skill lists onto the blocks where context supports them; skills that map to no role → unattributed_skill gap.
9. evidence_items: plain factual claims, past tense, ≤60 words. No editorializing.
10. Set provenance.source: "cv_import" on every role_group and block.
"""


async def extract_experience(raw_text: str) -> tuple[dict | None, list[dict]]:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    system_prompt = _SYSTEM_TEMPLATE.format(schema=schema)

    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract the consultant profile from this CV text. "
                    "Return only the YAML — no explanation.\n\n"
                    + raw_text[:10000]
                ),
            }
        ],
    )

    raw = message.content[0].text.strip()

    # Strip markdown fences if model wrapped the output anyway
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner)

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return None, [{"kind": "parse_error", "context": str(exc)}]

    if not isinstance(data, dict):
        return None, [{"kind": "parse_error", "context": "LLM response is not a YAML mapping"}]

    # Genuine hard errors (contradictory/unparseable input) still block persisting
    if "error" in data:
        err = data["error"]
        errors = [err] if isinstance(err, dict) else [{"kind": "other", "context": str(err)}]
        return None, errors

    # Gaps and needs_review flags are normal v1.1 output — they are NOT errors.
    # Return the data as-is; the Writer will handle gaps and assign IDs.
    return data, []
