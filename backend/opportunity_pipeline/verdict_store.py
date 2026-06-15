"""Safe storage helpers for consultant verdict history."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from backend.config import APPLICATION_TRACKER_DIR


class VerdictStoreError(RuntimeError):
    """Raised when verdict history cannot be read or written safely."""


def verdicts_path(consultant_id: str) -> Path:
    return APPLICATION_TRACKER_DIR / f"{consultant_id}_verdicts.json"


def load_verdicts(consultant_id: str) -> Dict[str, Any]:
    path = verdicts_path(consultant_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VerdictStoreError(
            f"Could not read verdict history at {path}. Refusing to continue so existing history is not overwritten."
        ) from exc
    if not isinstance(data, dict):
        raise VerdictStoreError(f"Verdict history at {path} must be a JSON object.")
    return data


def _backup_existing(path: Path) -> None:
    if not path.exists():
        return
    backup_dir = path.parent / "_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, backup_path)


def save_verdicts(consultant_id: str, data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise VerdictStoreError("Verdict history must be saved as a JSON object.")

    path = verdicts_path(consultant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_existing(path)

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp_path = path.with_name(f"{path.stem}.tmp.{os.getpid()}{path.suffix}")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        finally:
            raise VerdictStoreError(f"Could not save verdict history at {path}.") from exc
