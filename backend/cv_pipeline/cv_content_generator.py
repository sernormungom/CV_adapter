import logging
from pathlib import Path
import yaml

from backend.config import DEBUG_LLM_LOGGING
from backend.llm import MODEL_DEFAULT, WORKFLOW_TIMEOUT_SECONDS, create_message

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_CV_SCHEMA_PATH = _REPO_ROOT / "project_context" / "cv-content.schema.md"
_PROMPT_PATH = Path(__file__).parent / "cv_content_prompt.md"

# The CV-writing methodology lives in cv_content_prompt.md (single source of truth, and
# already generation-native). We load its body as the system prompt and append an OUTPUT
# CONTRACT that pins the exact YAML the renderer needs.
# The consultant profile is injected as YAML in the user message and the prompt's
# "Source of Truth" section already enumerates its fields, so the (large, importer-
# oriented) consultant-profile schema is deliberately NOT included here.
_OUTPUT_CONTRACT = """\

=== OUTPUT CONTRACT (machine output format) ===
Return ONLY raw YAML conforming to the CV CONTENT SCHEMA below — no markdown fences, no
commentary, no preamble. Produce the COMPLETE document (every applicable section) in one response.
Do NOT invent claims; tailor and rephrase only what the profile supports.

How the methodology above maps to the required YAML structure:
- CV Title           → header.job_title (tailored, 2–4 words).
- Summary            → summary.
- Tech Competence    → competencies.hard_skills / competencies.soft_skills / competencies.tools.
- Each Experience    → an experience[] item: organization, role_title, date_range,
                       optional is_personal_project, relevant_items (4–8 role tags) and
                       bullets (3–5 prose sentences).
- Education/Courses/Languages → education[], courses[], languages[].
- Always include meta with consultant_id filled from the input.
- render.section_order lists every populated section exactly once; sidebar_sections ⊆ section_order.
- If you cannot produce a valid document, emit the structured `error` block defined in the schema.

=== CV CONTENT SCHEMA (your output MUST conform exactly to this) ===
{cv_schema}
=== END CV CONTENT SCHEMA ===
"""


def _strip_frontmatter(md: str) -> str:
    """Drop a leading YAML frontmatter block (--- ... ---) — it's agent-discovery metadata only."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            nl = md.find("\n", end + 1)
            return md[nl + 1:].lstrip() if nl != -1 else ""
    return md


def _build_system_prompt() -> str:
    methodology = _strip_frontmatter(_PROMPT_PATH.read_text(encoding="utf-8"))
    cv_schema = _CV_SCHEMA_PATH.read_text(encoding="utf-8")
    return methodology + _OUTPUT_CONTRACT.format(cv_schema=cv_schema)


async def generate_cv_content(context: dict) -> tuple[dict | None, list[dict]]:
    from datetime import datetime, timezone

    system_prompt = _build_system_prompt()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    user_message = (
        f"Generate a tailored CV using the information below.\n\n"
        f"=== CONSULTANT PROFILE (YAML) ===\n{context['profile_yaml']}\n\n"
        f"=== STYLE RULES ===\n{_extract_rules_section(context['style_md'])}\n\n"
        f"=== TARGET JOB DESCRIPTION ===\n{context['job_description'][:4000]}\n\n"
        f"generated_at: {now}\n"
        f"consultant_id: {context['consultant_id']}\n\n"
        "Return only the cv-content YAML — no explanation."
    )

    # The system prompt (methodology + cv-content schema) is identical across generations, so cache it;
    # only the per-request profile/style/job in the user message varies.
    message = await create_message(
        model=MODEL_DEFAULT,
        label="cv_generate",
        max_tokens=6000,
        timeout=WORKFLOW_TIMEOUT_SECONDS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text.strip()
    if DEBUG_LLM_LOGGING:
        logger.info("CV generator raw response (first 400 chars): %s", raw[:400])

    # Strip markdown fences if model wrapped output anyway
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner)

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.error("CV YAML parse failed: %s", exc)
        if DEBUG_LLM_LOGGING:
            logger.error("Raw:\n%s", raw[:800])
        return None, [{"kind": "parse_error", "context": str(exc)}]

    if not isinstance(data, dict):
        logger.error("CV generator returned non-dict (%s)", type(data).__name__)
        if DEBUG_LLM_LOGGING:
            logger.error("Raw: %s", raw[:400])
        return None, [{"kind": "parse_error", "context": f"Expected YAML mapping, got {type(data).__name__}"}]

    if "error" in data:
        err = data["error"]
        logger.error("CV generator reported hard error: %s", err)
        return None, [err] if isinstance(err, dict) else [{"kind": "other", "context": str(err)}]

    errors = _validate_cv_content(data)
    if errors:
        logger.error("CV content validation failed: %s", errors)
        logger.error("Failing data keys: %s", list(data.keys()))
        return None, errors

    logger.info("CV content generated and validated OK")
    return data, []


def _extract_rules_section(style_md: str) -> str:
    """Return only the Rules section of style.md to keep the prompt focused."""
    if not style_md:
        return "(no style rules defined yet)"
    lines = style_md.splitlines()
    in_rules = False
    out: list[str] = []
    for line in lines:
        if line.strip() == "## Rules":
            in_rules = True
        elif line.startswith("## ") and in_rules:
            break
        if in_rules:
            out.append(line)
    return "\n".join(out) if out else style_md


def _validate_cv_content(data: dict) -> list[dict]:
    issues: list[dict] = []

    def err(msg: str) -> None:
        issues.append({"kind": "validation_error", "context": msg})

    # Required structural fields
    if not data.get("header", {}).get("full_name"):
        err("header.full_name is required")
    if not data.get("header", {}).get("job_title"):
        err("header.job_title is required")
    if not data.get("summary"):
        err("summary is required")

    # Competencies: a mapping of three columns. hard_skills must be non-empty; the
    # others are optional lists. Warn (don't block) if the total looks thin.
    competencies = data.get("competencies")
    if not isinstance(competencies, dict):
        err("competencies must be a mapping with hard_skills / soft_skills / tools lists")
    else:
        for col in ("hard_skills", "soft_skills", "tools"):
            val = competencies.get(col)
            if val is not None and not isinstance(val, list):
                err(f"competencies.{col} must be a list")
        if not competencies.get("hard_skills"):
            err("competencies.hard_skills must have at least 1 item")
        total = sum(len(competencies.get(c) or []) for c in ("hard_skills", "soft_skills", "tools"))
        if 0 < total < 6:
            logger.warning("competencies has only %d items total (schema recommends ~8–10 per column)", total)

    # Experience: require at least 1 entry; bullets require at least 1 (warn if <3).
    # relevant_items is optional but must be a list when present.
    experience = data.get("experience", [])
    if not experience:
        err("experience must have at least one entry")
    for i, exp in enumerate(experience):
        bullets = exp.get("bullets", [])
        if not bullets:
            err(f"experience[{i}] ({exp.get('organization','?')}) has no bullets")
        elif len(bullets) < 3:
            logger.warning(
                "experience[%d] (%s) has only %d bullets (schema recommends 3–5)",
                i, exp.get("organization", "?"), len(bullets)
            )
        ri = exp.get("relevant_items")
        if ri is not None and not isinstance(ri, list):
            err(f"experience[{i}] ({exp.get('organization','?')}) relevant_items must be a list")

    # Languages: require at least 1
    if not data.get("languages"):
        err("languages must have at least one entry")

    # Render: add defaults if missing rather than blocking
    render = data.get("render")
    if not render:
        logger.warning("render section missing — injecting defaults")
        data["render"] = {
            "length": "two_page",
            "section_order": ["summary", "competencies", "experience", "education", "languages"],
            "sidebar_sections": ["competencies", "languages"],
        }
    else:
        if not render.get("length"):
            render["length"] = "two_page"
        if not render.get("section_order"):
            render["section_order"] = ["summary", "competencies", "experience", "education", "languages"]
        if not render.get("sidebar_sections"):
            render["sidebar_sections"] = ["competencies", "languages"]

    # Inject consultant_id into meta if LLM forgot it
    if not data.get("meta"):
        data["meta"] = {}
    if not data["meta"].get("consultant_id"):
        logger.warning("meta.consultant_id missing from LLM output — will be patched by caller")

    return issues
