"""Storage-limitation purges (GDPR Art 5(1)(c),(e) — docs/legal/decisions.md Session 3).

Runs the retention purges (archived upload files, expired/revoked CV
publications — the latter replaced the retired `cv_share` module's share
tokens). Both run automatically (a daily lifespan task in `backend/main.py`)
and on demand (`POST /api/admin/retention/run`). The verdict-row purge added
in Session 3 was removed with the `verdicts` table (V3 packet S2).

Relational Jobs are deliberately not purged here. V1's JSON Job Store expired
and hard-deleted job files on a grace timer; a V2 Job is closed by a person
(`backend/jobs/service.py::close_job`) and kept, because operational Delete
versus hard delete is an open product/legal decision (`docs/DEFERRED.md`).
"""
