"""The generation key — the one fact that ties an artifact to a durable run.

A leaf module on purpose: it imports nothing from `backend.workflows` or
`backend.tailored_cv.service`, so the handler can register itself against
`GENERATION_WORKFLOW_TYPE` at import time no matter which of the two packages
Python happens to load first. Reaching into the service for that constant made
registration depend on import order, and a partially-initialised module is the
kind of failure that shows up only in whichever test module runs first.

The key is *also* the workflow's idempotency key, deliberately. `workflow_runs`
guarantees one row per key, and the handler's effect is guarded by the same
`(input_hash, attempt)` pair — so "one run per business intent" and "one
document per generation" are the same fact rather than two that could drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

#: The workflow type the runner dispatches on.
GENERATION_WORKFLOW_TYPE = "tailored_cv_generate"

_KEY_PREFIX = "tailored_cv"


@dataclass(frozen=True)
class GenerationKey:
    """Which artifact, from which frozen inputs, on which attempt."""

    tailored_cv_id: uuid.UUID
    input_hash: str
    attempt: int

    def format(self) -> str:
        return (
            f"{_KEY_PREFIX}:{self.tailored_cv_id}:{self.input_hash}:{self.attempt}"
        )


def parse_generation_key(key: str) -> GenerationKey | None:
    """The inverse of `GenerationKey.format`, or None for anything else."""
    parts = (key or "").split(":")
    if len(parts) != 4 or parts[0] != _KEY_PREFIX:
        return None
    try:
        return GenerationKey(
            tailored_cv_id=uuid.UUID(parts[1]),
            input_hash=parts[2],
            attempt=int(parts[3]),
        )
    except (ValueError, AttributeError, TypeError):
        return None


def input_ref(tailored_cv_id) -> str:
    """The workflow row's pointer at this artifact. A reference, never content."""
    return f"{_KEY_PREFIX}:{tailored_cv_id}"
