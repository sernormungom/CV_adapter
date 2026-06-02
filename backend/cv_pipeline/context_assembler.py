from pathlib import Path
from backend.config import DATA_DIR


def assemble_context(consultant_id: str, job_description: str) -> dict:
    """Read profile.md + style.md from disk; return context dict for the generator."""
    consultant_dir: Path = DATA_DIR / consultant_id

    profile_path = consultant_dir / "profile.md"
    style_path = consultant_dir / "style.md"

    profile_md = profile_path.read_text(encoding="utf-8")
    # Extract the YAML block from the markdown fence
    profile_yaml = _extract_yaml_block(profile_md)

    style_md = style_path.read_text(encoding="utf-8") if style_path.exists() else ""

    return {
        "consultant_id": consultant_id,
        "profile_yaml": profile_yaml,
        "style_md": style_md,
        "job_description": job_description,
    }


def _extract_yaml_block(profile_md: str) -> str:
    """Pull the content between ```yaml and ``` fences in profile.md."""
    lines = profile_md.splitlines()
    in_block = False
    yaml_lines: list[str] = []
    for line in lines:
        if line.strip() == "```yaml" and not in_block:
            in_block = True
            continue
        if line.strip() == "```" and in_block:
            break
        if in_block:
            yaml_lines.append(line)
    # Fallback: return the whole file if no fence found
    return "\n".join(yaml_lines) if yaml_lines else profile_md
