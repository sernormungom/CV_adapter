"""Workflow type → handler mapping.

A handler is `(db, run) -> result_ref | None` and must be **idempotent**: it can
be called again after a crash that happened between its business commit and the
runner marking the run succeeded. See `handlers/probe.py` for the shape.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from backend.db.models.workflow import WorkflowRun

WorkflowHandler = Callable[[Session, WorkflowRun], "str | None"]

_HANDLERS: dict[str, WorkflowHandler] = {}


def register(workflow_type: str, handler: WorkflowHandler) -> None:
    _HANDLERS[workflow_type] = handler


def get_handler(workflow_type: str) -> WorkflowHandler | None:
    return _HANDLERS.get(workflow_type)


def registered_types() -> frozenset[str]:
    return frozenset(_HANDLERS)
