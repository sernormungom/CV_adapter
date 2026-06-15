import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

from backend.config import DATA_DIR
from backend.cv_importer.profile_merger import merge_profiles
from backend.cv_assistant.profile_utils import assign_new_ids


# ── Strip fields removed in schema v1.2 ───────────────────────────────────────

def _strip_removed_fields(data: dict) -> dict:
    """Remove provenance, keywords, and tools_referenced — defensive against LLM hallucination."""
    for rg in (data.get("career_history") or {}).get("role_groups") or []:
        rg.pop("provenance", None)
        for block in rg.get("blocks") or []:
            block.pop("provenance", None)
            for ev in block.get("evidence_items") or []:
                ev.pop("keywords", None)
                ev.pop("tools_referenced", None)
    return data


# ── existing profile loader ────────────────────────────────────────────────────

def _load_existing_profile(profile_path: Path) -> dict | None:
    try:
        text = profile_path.read_text(encoding="utf-8")
        start = text.find("```yaml\n")
        end = text.rfind("\n```")
        if start == -1 or end == -1:
            return None
        yaml_text = text[start + 8 : end]
        data = yaml.safe_load(yaml_text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── consultant_id derivation ───────────────────────────────────────────────────

def _derive_consultant_id(full_name: str) -> str:
    normalized = unicodedata.normalize("NFKD", full_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return "_".join(part.lower() for part in ascii_name.split() if part)


# ── validation ─────────────────────────────────────────────────────────────────

def _validate(data: dict) -> list[str]:
    """
    Minimal hard-blocking checks only. Missing email/dates are now gaps (v1.1),
    not validation failures. We only block if we literally cannot proceed.
    """
    issues: list[str] = []
    if not data.get("identity", {}).get("full_name"):
        issues.append("identity.full_name is required — cannot derive consultant_id without a name")
    if not data.get("career_history", {}).get("role_groups"):
        issues.append("career_history.role_groups must have at least one entry")
    return issues


# ── style.md shell ─────────────────────────────────────────────────────────────

def _empty_style_shell(consultant_id: str, today: str) -> str:
    return f"""\
# Style Profile — {consultant_id}

_Last updated: {today} · Last consolidated: never_

## How to use this file
Rules below are applied to every CV. Observations are watched but NOT applied until promoted.

## Rules

### Tone & credit

### Phrasing & vocabulary

### Emphasis & foregrounding

### Hard avoidances

### Structure

## Observations (not yet applied)
"""


# ── date formatting ────────────────────────────────────────────────────────────

def _fmt_date(date_ym: str) -> str:
    if not date_ym:
        return ""
    if str(date_ym).lower() == "present":
        return "Present"
    try:
        dt = datetime.strptime(str(date_ym), "%Y-%m")
        return dt.strftime("%b %Y")
    except ValueError:
        return str(date_ym)


# ── form-fill data (for populateFromImport) ────────────────────────────────────

def profile_to_form_data(full_profile: dict) -> dict:
    identity = full_profile.get("identity", {})
    career = full_profile.get("career_history", {})
    education_list = full_profile.get("education", [])

    role_groups: list[dict] = career.get("role_groups", [])
    sorted_rgs = sorted(
        role_groups,
        key=lambda rg: rg.get("started") or "0000-00",
        reverse=True,
    )

    seen_main: set[str] = set()
    seen_other: set[str] = set()
    seen_tool: set[str] = set()
    main_skills: list[str] = []
    other_skills: list[str] = []
    tool_skills: list[str] = []

    for rg in sorted_rgs:
        sorted_blocks = sorted(
            rg.get("blocks", []),
            key=lambda b: b.get("started") or "0000-00",
            reverse=True,
        )
        for block in sorted_blocks:
            for lang in block.get("languages", []):
                if lang and lang not in seen_main:
                    main_skills.append(lang)
                    seen_main.add(lang)
            for tool in block.get("tools", []):
                if tool and tool not in seen_tool:
                    tool_skills.append(tool)
                    seen_tool.add(tool)
            for proc in block.get("processes_standards", []):
                if proc and proc not in seen_other:
                    other_skills.append(proc)
                    seen_other.add(proc)
            for domain in block.get("domains", []):
                if domain and domain not in seen_other:
                    other_skills.append(domain)
                    seen_other.add(domain)

    experience: list[dict] = []
    for rg in sorted_rgs:
        sorted_blocks = sorted(
            rg.get("blocks", []),
            key=lambda b: b.get("started") or "0000-00",
            reverse=True,
        )
        assignments = []
        for block in sorted_blocks:
            bullets = [
                ev.get("text", "")
                for ev in block.get("evidence_items", [])
                if ev.get("text")
            ]
            started = _fmt_date(block.get("started", ""))
            ended = _fmt_date(block.get("ended", ""))
            assignments.append({
                "role": block.get("role_title", ""),
                "period": f"{started} – {ended}" if started else ended,
                "bullets": bullets,
                "tools": ", ".join(block.get("tools", [])),
            })
        org = rg.get("organization", {})
        experience.append({
            "company": org.get("name", ""),
            "location": org.get("country_code", ""),
            "from": _fmt_date(rg.get("started", "")),
            "to": _fmt_date(rg.get("ended", "")),
            "assignments": assignments,
        })

    main_edu: dict = {}
    courses: list[str] = []
    for edu in education_list:
        if edu.get("type") in ("degree",) and not main_edu:
            main_edu = {
                "degree": edu.get("qualification", ""),
                "institution": edu.get("institution", ""),
                "from": _fmt_date(edu.get("started", "")),
                "to": _fmt_date(edu.get("ended", "")),
                "description": edu.get("description", ""),
            }
        elif edu.get("type") in ("certification", "course", "training", "bootcamp", "other"):
            name = edu.get("qualification", "")
            if name:
                courses.append(name)
    if not main_edu and education_list:
        edu = education_list[0]
        main_edu = {
            "degree": edu.get("qualification", ""),
            "institution": edu.get("institution", ""),
            "from": _fmt_date(edu.get("started", "")),
            "to": _fmt_date(edu.get("ended", "")),
            "description": edu.get("description", ""),
        }

    languages = [
        {"name": lang.get("language", ""), "level": lang.get("proficiency", "")}
        for lang in identity.get("spoken_languages", [])
    ]
    i_role = sorted_rgs[0].get("display_role_title", "") if sorted_rgs else ""

    return {
        "iName": identity.get("full_name", ""),
        "iRole": i_role,
        "iEmail": identity.get("email", ""),
        "iPhone": identity.get("phone", ""),
        "iAvail": "",
        "iSummary": "",
        "skills": {
            "main": main_skills[:12],
            "other": other_skills[:12],
            "tool": tool_skills[:12],
        },
        "experience": experience,
        "education": main_edu,
        "languages": languages,
        "courses": courses,
    }


# ── main entry point ───────────────────────────────────────────────────────────

async def write_profile(
    data: dict,
    source_tmp_path: Path | None = None,
    source_filename: str | None = None,
    username: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    as_of = datetime.now(timezone.utc).strftime("%Y-%m")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    issues = _validate(data)
    if issues:
        if source_tmp_path:
            source_tmp_path.unlink(missing_ok=True)
        return {"success": False, "consultant_id": None, "validation_errors": issues}

    full_name: str = data["identity"]["full_name"]
    consultant_id = username if username else _derive_consultant_id(full_name)

    # Archive the original uploaded CV before any merging (v1.1 §0.2)
    new_archive: str | None = None
    consultant_dir: Path = DATA_DIR / consultant_id
    consultant_dir.mkdir(parents=True, exist_ok=True)

    if source_tmp_path and source_tmp_path.exists():
        uploads_dir = consultant_dir / "uploads"
        uploads_dir.mkdir(exist_ok=True)
        dest = uploads_dir / (source_filename or source_tmp_path.name)
        shutil.move(str(source_tmp_path), str(dest))
        new_archive = f"uploads/{dest.name}"
    elif source_tmp_path:
        source_tmp_path.unlink(missing_ok=True)

    # Merge into existing profile if one already exists
    profile_path = consultant_dir / "profile.md"
    merge_errors: list[dict] = []
    existing_profile = _load_existing_profile(profile_path)

    if existing_profile is not None:
        merged_data, merge_errors = await merge_profiles(existing_profile, data)
        if merged_data is not None:
            data = merged_data
        # On merge failure fall through with the new extraction as-is

    data = _strip_removed_fields(data)
    data = assign_new_ids(data)

    # Build source_archives list: carry forward existing entries, append new one
    existing_archives: list[str] = []
    if existing_profile is not None:
        prev = existing_profile.get("metadata", {}).get("source_archives") or []
        if isinstance(prev, list):
            existing_archives = prev
        elif isinstance(prev, str) and prev:
            existing_archives = [prev]
        # also handle legacy scalar field name
        if not existing_archives:
            legacy = existing_profile.get("metadata", {}).get("source_archive")
            if isinstance(legacy, list):
                existing_archives = legacy
            elif isinstance(legacy, str) and legacy:
                existing_archives = [legacy]

    source_archives = existing_archives + ([new_archive] if new_archive else [])

    created_at = (
        existing_profile.get("metadata", {}).get("created_at", now)
        if existing_profile is not None
        else now
    )

    full_profile = {
        "metadata": {
            "schema_version": "1.3",
            "consultant_id": consultant_id,
            "created_at": created_at,
            "updated_at": now,
            "as_of": as_of,
            **({"source_archives": source_archives} if source_archives else {}),
        },
        "identity": data.get("identity", {}),
        "preferences": existing_profile.get("preferences", {}) if existing_profile else {},
        "education": data.get("education", []),
        "career_history": data.get("career_history", {}),
        "gaps": data.get("gaps", []),
    }

    # Write profile.md
    profile_path = consultant_dir / "profile.md"
    yaml_block = yaml.dump(full_profile, allow_unicode=True, sort_keys=False, default_flow_style=False)
    profile_path.write_text(
        f"# Consultant Profile — {full_name}\n\n```yaml\n{yaml_block}```\n",
        encoding="utf-8",
    )

    # Write style.md only on first import (preserve existing style rules)
    style_path = consultant_dir / "style.md"
    if not style_path.exists():
        style_path.write_text(_empty_style_shell(consultant_id, today), encoding="utf-8")

    # Stats
    role_groups = full_profile["career_history"].get("role_groups", [])
    tools: set[str] = set()
    for rg in role_groups:
        for block in rg.get("blocks", []):
            tools.update(block.get("tools", []))
    open_gaps = [g for g in full_profile["gaps"] if g.get("status") == "open"]
    blocking_gaps = [g for g in open_gaps if g.get("severity") == "blocking"]

    return {
        "success": True,
        "consultant_id": consultant_id,
        "roles_count": len(role_groups),
        "skills_count": len(tools),
        "open_gaps": len(open_gaps),
        "blocking_gaps": len(blocking_gaps),
        "merge_errors": merge_errors,
        "form_data": profile_to_form_data(full_profile),
    }
