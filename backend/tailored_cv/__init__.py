"""The Application-owned tailored CV (V2 Slice 3, `docs/DECISIONS.md` P-009).

- `service` — every write to `tailored_cvs`, plus the generation key that ties
  an artifact to its durable run.
- `handler` — the workflow handler that produces the content. Importing it
  registers the handler, which `backend/workflows/__init__.py` does.
- `routes` — `/api/applications/{id}/cv…`, the Application half of the editor's
  explicit mode routing. The base half is `backend/cv_state/`.
- `state_builder` — generated content + the base CV's presentation → the
  editor's `cv_state` shape (the mapping the browser used to perform).
- `keys` — the generation key, deliberately a leaf.

This file imports nothing. `backend/workflows/__init__.py` imports `handler`
from here to register it, and `service` imports `backend.workflows` — so a
convenience re-export here would put a third module in that cycle for no gain.
"""
