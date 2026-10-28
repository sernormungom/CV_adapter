from pathlib import Path
from backend.config import DATA_DIR
from backend.profile_reader import load_profile, profile_to_yaml


def assemble_context(consultant_id: str, job_description: str) -> dict:
    """Read profile + style.md from disk; return context dict for the generator."""
    style_path: Path = DATA_DIR / consultant_id / "style.md"
    style_md = style_path.read_text(encoding="utf-8") if style_path.exists() else ""

    return {
        "consultant_id": consultant_id,
        "profile_yaml": profile_to_yaml(load_profile(consultant_id)),
        "style_md": style_md,
        "job_description": job_description,
    }
