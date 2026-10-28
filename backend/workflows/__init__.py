"""Durable background work: one Postgres-backed queue, one in-process leader.

- `service` — state transitions (enqueue / claim / finish / recover).
- `runner` — the single asyncio loop; `run_once`/`drain` are the testable core.
- `leader` — the Postgres advisory lock that makes leadership exclusive.
- `registry` + `handlers/` — workflow type → idempotent handler.

Importing this package registers the built-in handlers, so a runner started
from the app lifespan finds them.
"""

from backend.workflows import leader, registry, runner, service
from backend.workflows.handlers import probe  # noqa: F401  (registers handlers)

# Domain handlers register the same way. Imported *after* the names above are
# bound, because a domain service legitimately imports `backend.workflows` —
# this package is then partially initialised, and `service` has to already be
# an attribute of it. The handler modules themselves only touch leaf constants
# at import time (`backend/tailored_cv/keys.py`, `backend/cv_importer/keys.py`),
# so registration never depends on which package Python loaded first.
from backend.tailored_cv import handler as tailored_cv_handler  # noqa: E402,F401
from backend.cv_importer import handler as cv_importer_handler  # noqa: E402,F401

__all__ = [
    "leader",
    "probe",
    "registry",
    "runner",
    "service",
    "tailored_cv_handler",
    "cv_importer_handler",
]

from backend.matching.config_generation import handle_config, WORKFLOW_TYPE
registry.register(WORKFLOW_TYPE, handle_config)
