"""Lossless, revision-checked access to a consultant's ``profile.md``.

The scoring/generation reader in :mod:`backend.profile_reader` deliberately
removes stable IDs and a little repeated data.  SuperCV cannot use that view:
an eventual whole-document editor must round-trip every field it did not edit.
This service therefore reuses the assistant's fenced-YAML parser and returns
the complete document, except for its own reserved identity marker.

The identity follows the base-CV precedent: it lives in the same file as the
content, is invisible to callers, and makes the revision check and write one
operation under the importer's existing ``profile.md`` lock.  The importer
already uses that lock, so an import and a chat/SuperCV save cannot write the
file at the same time.
"""

from __future__ import annotations

import copy
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend.cv_assistant.profile_utils import (
    assign_new_ids,
    load_profile as load_wrapped_profile,
    parse_yaml,
)
from backend.cv_importer.keys import PROFILE_LOCK_FILENAME
from backend.cv_state.service import content_hash, exclusive_profile_write

IDENTITY_KEY = "_profile_identity"

STALE_PROFILE_MESSAGE = (
    "This profile was changed by someone else since it was loaded. "
    "Reload before saving so their change is not overwritten."
)


class ProfileServiceError(RuntimeError):
    """A caller-fixable problem reading or saving ``profile.md``."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ProfileArtifact:
    """The full profile document plus the identity callers may compare."""

    username: str
    revision: int
    content_hash: str
    document: dict[str, Any]

    @property
    def profile(self) -> dict[str, Any]:
        """A readable alias for API callers."""
        return self.document

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "profile": self.document,
        }


def _without_identity(document: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with only SuperCV's reserved marker removed."""
    clean = copy.deepcopy(document)
    metadata = clean.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop(IDENTITY_KEY, None)
    return clean


def load_profile(username: str) -> ProfileArtifact:
    """Read the complete parsed profile without exposing its reserved marker.

    A document created before this service has no marker and therefore starts
    at revision zero.  As with ``cv_state.json``, the bytes represented by the
    parsed content are authoritative over a stored hash: an out-of-band writer
    may preserve an older marker, but callers still receive the actual hash.
    """
    try:
        yaml_text, profile_path = load_wrapped_profile(username)
    except (OSError, UnicodeError) as exc:
        raise ProfileServiceError(
            "The stored profile for this consultant could not be read.", status=409
        ) from exc
    if yaml_text is None:
        if not profile_path.exists():
            raise ProfileServiceError(
                "Profile not found for this consultant.", status=404
            )
        raise ProfileServiceError(
            "The stored profile for this consultant could not be read.", status=409
        )

    document = parse_yaml(yaml_text)
    if document is None:
        raise ProfileServiceError(
            "The stored profile for this consultant could not be read.", status=409
        )

    metadata = document.get("metadata")
    identity = metadata.get(IDENTITY_KEY) if isinstance(metadata, dict) else None
    identity = identity if isinstance(identity, dict) else {}
    try:
        revision = int(identity.get("revision") or 0)
    except (TypeError, ValueError) as exc:
        raise ProfileServiceError(
            "The stored profile identity is invalid.", status=409
        ) from exc
    if revision < 0:
        raise ProfileServiceError(
            "The stored profile identity is invalid.", status=409
        )

    clean = _without_identity(document)
    actual_hash = content_hash(clean)
    stored_hash = identity.get("content_hash")
    return ProfileArtifact(
        username=username,
        revision=revision,
        content_hash=actual_hash if stored_hash != actual_hash else stored_hash,
        document=clean,
    )


def save_profile(
    username: str,
    document: dict[str, Any],
    *,
    expected_revision: int,
    expected_content_hash: str | None = None,
) -> ProfileArtifact:
    """Save the whole profile, refusing a stale revision or content hash.

    The current revision is loaded *inside* the same cross-process lock as the
    atomic replace.  Every new object receives its server-owned ID before the
    hash and marker are written.  A caller-supplied ``_profile_identity`` is
    always discarded, so callers cannot forge either side of the comparison.

    ``expected_content_hash`` is optional and, when given, is compared
    alongside the revision: a mismatch raises the same 409.  This closes the
    hole the revision check alone leaves open — the CV importer writes
    ``profile.md`` under this same lock but never bumps ``_profile_identity``,
    so a completed import changes the actual content without changing the
    revision a caller loaded, while ``load_profile`` already returns the
    document's *actual* hash.  Stamps ``metadata.updated_at`` so every writer
    of this document funnels through one place that sets it.
    """
    if not isinstance(document, dict):
        raise ProfileServiceError("A profile must be a YAML mapping.")
    if expected_revision < 0:
        raise ProfileServiceError("The expected revision must be non-negative.")

    with exclusive_profile_write(username, PROFILE_LOCK_FILENAME):
        current = load_profile(username)
        if current.revision != expected_revision:
            raise ProfileServiceError(STALE_PROFILE_MESSAGE, status=409)
        if (
            expected_content_hash is not None
            and current.content_hash != expected_content_hash
        ):
            raise ProfileServiceError(STALE_PROFILE_MESSAGE, status=409)

        clean = _without_identity(document)
        metadata = clean.get("metadata")
        if not isinstance(metadata, dict):
            raise ProfileServiceError(
                "The profile metadata must be a YAML mapping.", status=409
            )
        assign_new_ids(clean)
        metadata["updated_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )

        revision = current.revision + 1
        actual_hash = content_hash(clean)
        stored = copy.deepcopy(clean)
        stored["metadata"][IDENTITY_KEY] = {
            "revision": revision,
            "content_hash": actual_hash,
        }
        _atomic_write_profile(profile_path=_profile_path(username), document=stored)

    return ProfileArtifact(
        username=username,
        revision=revision,
        content_hash=actual_hash,
        document=clean,
    )


def _profile_path(username: str) -> Path:
    """Resolve through the shared reader so test data-root bindings agree."""
    _text, path = load_wrapped_profile(username)
    return path


def _atomic_write_profile(*, profile_path: Path, document: dict[str, Any]) -> None:
    """Render the fenced YAML to a sibling temp file and atomically replace."""
    full_name = str((document.get("identity") or {}).get("full_name") or "Consultant")
    heading = full_name.splitlines()[0]
    yaml_block = yaml.dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    rendered = f"# Consultant Profile — {heading}\n\n```yaml\n{yaml_block}```\n"

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=profile_path.parent,
        delete=False,
        suffix=".tmp",
    )
    try:
        with handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, profile_path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
