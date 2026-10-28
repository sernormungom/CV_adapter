"""Read the current `profile.md` — the preview route, and the crash-recovery
check `backend.cv_importer.handler` uses before deciding whether to merge.

A separate, tiny module rather than folding this into `experience_writer.py`,
which Slice 4 otherwise reuses **unchanged** (`docs/plans/v2-pilot.md`). It
calls the writer's already-public `profile_to_form_data`, and reads the YAML
fence out of `profile.md` itself — the same dozen lines
`experience_writer._load_existing_profile` uses, duplicated rather than
imported so this module never depends on that file's private surface.

`read_profile_document` is public (not underscore-prefixed) because two
call sites need it now: `load_preview` below (what the editor shows after an
import), and the handler's `_last_import_key` check (whether a *this exact
attempt's* merge already landed on disk before a crash — see the handler's
module docstring).
"""

from __future__ import annotations

import yaml

from backend import config
from backend.cv_importer.experience_writer import profile_to_form_data


class PreviewError(RuntimeError):
    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def read_profile_document(username: str) -> dict | None:
    path = config.DATA_DIR / username / "profile.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreviewError("This consultant's profile could not be read.") from exc

    start = text.find("```yaml\n")
    end = text.rfind("\n```")
    if start == -1 or end == -1:
        raise PreviewError("This consultant's profile could not be read.")
    try:
        data = yaml.safe_load(text[start + 8 : end])
    except yaml.YAMLError as exc:
        raise PreviewError("This consultant's profile could not be read.") from exc
    return data if isinstance(data, dict) else None


def load_preview(username: str) -> dict | None:
    """Editor form-data for `username`'s current profile, or None if absent."""
    profile = read_profile_document(username)
    if profile is None:
        return None
    return profile_to_form_data(profile)
