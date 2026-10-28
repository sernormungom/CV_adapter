"""The import key — the one fact that ties a `CVImport` row to a durable run.

A leaf module, deliberately, for the same reason `tailored_cv.keys` is one: it
imports nothing from `backend.workflows` or `backend.cv_importer.service`, so
the handler can register itself against `IMPORT_WORKFLOW_TYPE` at import time
no matter which of the two packages Python loads first.

The key is also the workflow's idempotency key. `workflow_runs` guarantees one
row per key, and the handler's effect is guarded by the same
`(input_hash, attempt)` pair — "one run per import request" and "one profile
write per attempt" are the same fact rather than two that could drift.

**`SCRATCH_DIRNAME`/`PROFILE_LOCK_FILENAME` live here too, not in
`service.py`, for exactly this module's own reason (`S4-P1-R8` finding 3).**
`backend/privacy/service.py` needs both at import time, and `service.py`
itself imports `backend.workflows` — a package whose own `__init__` imports
`backend.cv_importer.handler`, which needs `PROFILE_LOCK_FILENAME`. Whichever
of `cv_importer.service` or `backend.workflows` starts loading first, the
other re-enters it mid-load; defining these constants in `service.py` after
that import meant a caller reaching `cv_importer.service` before anything
touches `backend.workflows` (a fresh interpreter importing
`backend.privacy.service` directly, skipping the app's own
`backend.workflows`-first startup order) hit
`ImportError: cannot import name 'PROFILE_LOCK_FILENAME' from partially
initialized module`. Both constants are plain strings with no further
dependency, so a leaf module is the actual fix — not reordering statements in
a file that already carries the same fragility for anything defined after that
import. `service.py` and `handler.py` both still expose/import them the same
way as before.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

#: The workflow type the runner dispatches on.
IMPORT_WORKFLOW_TYPE = "cv_import"

#: The scratch subdirectory a consultant's transient uploads live in while an
#: import is processing. Deliberately **not** `uploads/` — that directory is
#: the permanent raw-CV backstop archive
#: (`project_context/consultant-profile.schema.md` §0.2) with its own 30-day
#: retention purge (`experience_writer.purge_old_uploads`); V2 Pilot's
#: transient-source-deletion floor (`docs/plans/v2-pilot.md` §1) means an
#: async import's upload must not join that archive at all.
SCRATCH_DIRNAME = ".cv_import_tmp"

#: The exclusive-write lock file for `profile.md`, beside it in the profile
#: directory — a sibling of `cv_state.service.LOCK_FILENAME`, not the same
#: file, so an import and a base-CV save never contend with each other for no
#: reason. See `cv_state.service.exclusive_profile_write`.
PROFILE_LOCK_FILENAME = ".profile_import.lock"

_KEY_PREFIX = "cv_import"


@dataclass(frozen=True)
class ImportKey:
    """Which import row, from which uploaded bytes, on which attempt."""

    cv_import_id: uuid.UUID
    input_hash: str
    attempt: int

    def format(self) -> str:
        return f"{_KEY_PREFIX}:{self.cv_import_id}:{self.input_hash}:{self.attempt}"


def parse_import_key(key: str) -> ImportKey | None:
    """The inverse of `ImportKey.format`, or None for anything else."""
    parts = (key or "").split(":")
    if len(parts) != 4 or parts[0] != _KEY_PREFIX:
        return None
    try:
        return ImportKey(
            cv_import_id=uuid.UUID(parts[1]),
            input_hash=parts[2],
            attempt=int(parts[3]),
        )
    except (ValueError, AttributeError, TypeError):
        return None


def input_ref(cv_import_id) -> str:
    """The workflow row's pointer at this import. A reference, never content."""
    return f"{_KEY_PREFIX}:{cv_import_id}"


def result_ref(cv_import_id, attempt: int) -> str:
    """The workflow row's pointer at what an attempt produced.

    Lives here, beside `input_ref`, because two callers must produce the byte-
    identical string: the handler, on the ordinary path, and
    `service._try_reconcile_stuck_attempt`, which finalizes a stuck attempt's
    run without the handler running again (`S4-P1-R4`). A reference, never
    content.
    """
    return f"{input_ref(cv_import_id)}:attempt:{attempt}"
