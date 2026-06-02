from pathlib import Path
import anthropic
import yaml

_CV_SCHEMA_PATH = Path(__file__).parent.parent.parent / "project_context" / "cv-content.schema.md"
_PROFILE_SCHEMA_PATH = Path(__file__).parent.parent.parent / "project_context" / "consultant-profile.schema.md"

_SYSTEM_TEMPLATE = """\
You are a professional CV writer generating a tailored CV for a specific job application.

=== CV CONTENT SCHEMA (your output must conform to this) ===
{cv_schema}
=== END CV CONTENT SCHEMA ===

=== PROFILE SCHEMA (reference for understanding the input) ===
{profile_schema}
=== END PROFILE SCHEMA ===

GENERATION RULES:
1. Return ONLY raw YAML — no markdown fences, no commentary, no preamble.
2. Your output must conform exactly to the cv-content schema above.
3. Produce a complete document: all required sections, correct field names and types.
4. Tailor everything to the job description: select the most relevant experience blocks and evidence items, rewrite bullets to emphasise fit, choose a job_title that matches the position vocabulary while staying truthful.
5. Apply EVERY rule in the style.md Rules section. Hard avoidances are absolute.
6. Do NOT invent claims. Bullets must be traceable to profile evidence_items — rephrased, not fabricated.
7. Competencies: 6–12 items derived from the most relevant block skills (languages, tools, domains, processes_standards).
8. Experience: ordered most-recent first, 3–5 bullets each. Personal projects (employment_type: personal_project / open_source) must include is_personal_project: true.
9. section_order must list every populated section exactly once. sidebar_sections ⊆ section_order.
10. If you cannot produce a valid document (e.g. no relevant experience), emit a structured error:
    error:
      kind: "no_relevant_experience" | "insufficient_evidence" | "other"
      context: "description"
      suggested_action: "what would help"
"""


async def generate_cv_content(context: dict) -> tuple[dict | None, list[dict]]:
    from datetime import datetime, timezone

    cv_schema = _CV_SCHEMA_PATH.read_text(encoding="utf-8")
    profile_schema = _PROFILE_SCHEMA_PATH.read_text(encoding="utf-8")
    system_prompt = _SYSTEM_TEMPLATE.format(cv_schema=cv_schema, profile_schema=profile_schema)

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

    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text.strip()

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
        return None, [{"kind": "parse_error", "context": str(exc)}]

    if not isinstance(data, dict):
        return None, [{"kind": "parse_error", "context": "LLM response is not a YAML mapping"}]

    if "error" in data:
        err = data["error"]
        return None, [err] if isinstance(err, dict) else [{"kind": "other", "context": str(err)}]

    errors = _validate_cv_content(data)
    if errors:
        return None, errors

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

    if not data.get("meta", {}).get("consultant_id"):
        err("meta.consultant_id is required")
    if not data.get("header", {}).get("full_name"):
        err("header.full_name is required")
    if not data.get("header", {}).get("job_title"):
        err("header.job_title is required")
    if not data.get("summary"):
        err("summary is required")

    competencies = data.get("competencies", [])
    if not (6 <= len(competencies) <= 12):
        err(f"competencies must have 6–12 items (got {len(competencies)})")

    experience = data.get("experience", [])
    if not experience:
        err("experience must have at least one entry")
    for i, exp in enumerate(experience):
        bullets = exp.get("bullets", [])
        if not (3 <= len(bullets) <= 5):
            err(f"experience[{i}].bullets must have 3–5 items (got {len(bullets)})")

    languages = data.get("languages", [])
    if not languages:
        err("languages must have at least one entry")

    render = data.get("render", {})
    section_order = render.get("section_order", [])
    sidebar_sections = render.get("sidebar_sections", [])
    if not render.get("length"):
        err("render.length is required")
    if not section_order:
        err("render.section_order is required")
    for s in sidebar_sections:
        if s not in section_order:
            err(f"render.sidebar_sections contains '{s}' which is not in section_order")

    return issues
