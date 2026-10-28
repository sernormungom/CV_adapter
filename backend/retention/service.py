"""Combines the retention purges into one callable.

Each purge is independent and lives next to the store it cleans (uploads in
`cv_importer/experience_writer.py`, published CVs in `cv_publish/service.py`);
this module just runs both and reports a combined result for the admin
endpoint. The verdict purge that used to run here was removed with the
`verdicts` table (V3 packet S2, `docs/plans/v3/decisions.md` V3-P-008
decision 5) — the verdict-fed learning path it backed has no live writer or
reader left.

A fourth purge — a daily sweep of the async importer's `.cv_import_tmp/`
scratch directory — was built and reviewed (`S4-P1-R7`/`R8`) and then removed
as a deliberate pilot-scope decision: it existed only to recover a stray file
left by a rare double failure (a delete that fails, on a consultant who never
imports again), which costs disk and nothing else, and its own "which
reference is still active" logic was where two High review findings lived.
`backend/cv_importer/service.py`'s module docstring records the accepted
residual risk. A real (non-pilot) rollout that actually needs this guarantee
should design it fresh rather than resurrect the removed one from history —
its "protect only genuinely active work" logic is exactly the part worth
rethinking, not merely restoring.

If `cv_importer.service` ever needs to be imported here again, note that it
imports `backend.workflows`, whose package `__init__` imports
`cv_importer.handler` — a cycle `cv_importer.keys` (a genuine leaf module)
exists specifically to avoid; see its docstring before adding a new
`cv_importer.service` dependency anywhere that might load before
`backend.workflows` does.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.cv_importer.experience_writer import purge_old_uploads
from backend.cv_publish.service import purge_revoked as purge_revoked_publications


def run_retention_purges(db: Session) -> dict:
    """Run the upload and publication purges and report each result.

    The publication purge deletes **revoked** rows only — expiry alone blocks
    access (`is_serveable`) but never deletes anything
    (`docs/plans/v2-pilot.md` Slice 5); a row is only ever revoked by an
    explicit Revoke or a confirmed terminal Application transition.
    """
    uploads = purge_old_uploads()
    publications = purge_revoked_publications(db)
    return {
        "uploads": uploads,
        "publications": publications,
    }
