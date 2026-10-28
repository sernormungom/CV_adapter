"""Session 4 of docs/plans/cv-publish.md: the app previously had no scheduled
path for `retention.service.run_retention_purges` (only the admin-triggered
`POST /api/admin/retention/run`), so an expired CV publication's row + photo
file could sit indefinitely if no admin remembered to click Purge. This covers
the daily background loop added to `backend/main.py`'s lifespan.
"""

from fastapi.testclient import TestClient

from backend import main


def test_lifespan_runs_purge_at_startup_and_cancels_cleanly(monkeypatch):
    calls = []

    async def fake_purge_once():
        calls.append(1)

    monkeypatch.setattr(main, "_run_retention_purge_once", fake_purge_once)
    monkeypatch.setattr(main, "_RETENTION_PURGE_INTERVAL_SECONDS", 3600)
    # The Site guard is separately tested with a disposable database; this
    # lifespan-only test deliberately has no configured Postgres connection.

    # Entering as a context manager triggers the lifespan startup (spawns the
    # loop task); exiting triggers shutdown (cancels it). A request in between
    # gives the event loop a chance to actually run the background task.
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200

    assert calls, "the purge loop should have run at least once during startup"
