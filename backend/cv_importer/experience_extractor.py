import logging
from pathlib import Path
import yaml

from backend.config import DEBUG_LLM_LOGGING
from backend.llm import MODEL_DEFAULT, WORKFLOW_TIMEOUT_SECONDS, create_message

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "project_context" / "consultant-profile.schema.md"

_SYSTEM_TEMPLATE = """\
You are an expert CV parser. Extract structured career data from the provided CV text and return it as valid YAML.

=== CONSULTANT PROFILE SCHEMA (v1.3) ===
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
10. CONSULTANT ENGAGEMENTS (employment_type: consultant_via_employer):
    - The role_group organisation is the EMPLOYER (e.g. "Mpya Sci & Tech"). Each block's `client.name`
      is the END CLIENT where the work actually happens (e.g. "Volvo Group"). Set `client.department`
      if the CV names a unit or team within the client.
    - role_title describes the role only (e.g. "Senior Software Function Developer") — do NOT embed
      the client name in role_title; it belongs in client.name.
    - Block started/ended are the CLIENT ASSIGNMENT dates, not the employer's tenure dates. These two
      date ranges are different and must not be conflated.
    - If the CV does not state explicit start/end dates for a client assignment, create an
      `ambiguous_dates` gap — do NOT copy or default to the employer's dates.
"""


async def extract_experience(raw_text: str) -> tuple[dict | None, list[dict], str]:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    system_prompt = _SYSTEM_TEMPLATE.format(schema=schema)

    message = await create_message(
        model=MODEL_DEFAULT,
        label="cv_extract",
        # Raised alongside the 20,000-char input cap below (was 6000/10,000)
        # -- a longer CV needs more room to extract into as well as more
        # room to read, or a bigger input just trades "truncated CV" for
        # "truncated YAML" (an unparseable response, extraction fails
        # outright) instead of actually fixing the problem.
        max_tokens=8000,
        timeout=WORKFLOW_TIMEOUT_SECONDS,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract the consultant profile from this CV text. "
                    "Return only the YAML — no explanation.\n\n"
                    + raw_text[:20000]
                ),
            }
        ],
    )

    raw = message.content[0].text.strip()
    if DEBUG_LLM_LOGGING:
        logger.info("LLM raw response (first 400 chars): %s", raw[:400])

    # A response cut off by the max_tokens budget is guaranteed-invalid YAML
    # (mid-list, mid-string, etc) — don't even attempt to parse it and blame
    # "parse_error" on what's really "this CV is too large/detailed to
    # summarize within max_tokens=8000 in one pass". Checked before fence
    # stripping since a truncated response may never reach a closing fence.
    if message.stop_reason == "max_tokens":
        logger.error(
            "Extraction response truncated at max_tokens (input was %d chars)",
            len(raw_text),
        )
        return None, [{
            "kind": "cv_too_large",
            "context": "response truncated at max_tokens",
        }], raw

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
        logger.error("YAML parse failed: %s", exc)
        if DEBUG_LLM_LOGGING:
            logger.error("Raw output was:\n%s", raw[:800])
        return None, [{"kind": "parse_error", "context": str(exc)}], raw

    if not isinstance(data, dict):
        logger.error("LLM returned non-dict (%s)", type(data).__name__)
        if DEBUG_LLM_LOGGING:
            logger.error("Raw: %s", raw[:400])
        return None, [{"kind": "parse_error", "context": f"Expected YAML mapping, got {type(data).__name__}"}], raw

    # Genuine hard errors (contradictory/unparseable input) still block persisting
    if "error" in data:
        err = data["error"]
        logger.error("LLM reported a hard error: %s", err)
        errors = [err] if isinstance(err, dict) else [{"kind": "other", "context": str(err)}]
        return None, errors, raw

    logger.info(
        "Extraction OK — top-level keys: %s, gaps: %d",
        list(data.keys()),
        len(data.get("gaps", [])),
    )
    # Gaps and needs_review flags are normal v1.2 output — they are NOT errors.
    # Return the data as-is; the Writer will handle gaps and assign IDs.
    return data, [], raw
