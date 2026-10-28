"""
Reads and validates matching_config.yaml for a consultant.
Falls back to the previous backup if the current file is malformed.
Returns None if no config exists (caller triggers bootstrap).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from backend.config import DATA_DIR
from shared.matching_config import validate_config as _validate


def _config_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.yaml"


def _backup_path(consultant_id: str) -> Path:
    return DATA_DIR / consultant_id / "matching_config.backup.yaml"


def load_config(consultant_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the matching config dict for consultant_id, or None if absent.
    Tries current → backup on parse/validation failure.
    """
    path = _config_path(consultant_id)
    backup = _backup_path(consultant_id)

    for candidate in (path, backup):
        if not candidate.exists():
            continue
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if _validate(data):
                return data
        except Exception:
            pass
    return None


def _atomic_write(path: Path, text: str) -> None:
    """Publish a complete document; failed writes leave the destination intact."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_config(consultant_id: str, cfg: Dict[str, Any], make_backup: bool = True) -> None:
    """Validate before touching disk, preserve a valid fallback, then replace.

    Callers coordinating concurrent generations must hold the consultant's
    exclusive profile-write lock around this operation.
    """
    try:
        valid = isinstance(cfg, dict) and _validate(cfg)
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid matching configuration")
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=110)
    path = _config_path(consultant_id)
    # Do not overwrite a good backup with a malformed current document.
    if make_backup and path.exists():
        current = path.read_text(encoding="utf-8")
        try:
            previous = yaml.safe_load(current)
            valid_previous = isinstance(previous, dict) and _validate(previous)
        except (yaml.YAMLError, AttributeError, TypeError, ValueError):
            valid_previous = False
        if valid_previous:
            _atomic_write(_backup_path(consultant_id), current)
    _atomic_write(path, text)
