"""
Focused test confirming run_cycle returns both collection.expired and collection.purged.
All heavy dependencies are patched so no real job store or matching runs.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.opportunity_pipeline.dashboard.routes import router

_MODULE = "backend.opportunity_pipeline.dashboard.routes"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router, prefix="")
    return TestClient(app)


def test_run_cycle_returns_expired_and_purged(client):
    with (
        patch(f"{_MODULE}._load_sources", return_value=[]),
        patch(f"{_MODULE}.collect_from_sources", return_value=[]),
        patch(
            f"{_MODULE}.write_positions",
            return_value={"new": 0, "duplicate": 0, "error": 0, "total_processed": 0, "items": []},
        ),
        patch(
            f"{_MODULE}.mark_expired_positions",
            return_value={"marked_expired": 2},
        ),
        patch(
            f"{_MODULE}.purge_expired_positions",
            return_value={"purged": 1},
        ),
        patch(f"{_MODULE}.run_matching", return_value=([], {"total_scored": 0})),
    ):
        resp = client.post("/test_user/run-cycle")

    assert resp.status_code == 200
    data = resp.json()
    assert data["collection"]["expired"] == 2
    assert data["collection"]["purged"] == 1


def test_run_cycle_zero_purged_when_none_old_enough(client):
    with (
        patch(f"{_MODULE}._load_sources", return_value=[]),
        patch(f"{_MODULE}.collect_from_sources", return_value=[]),
        patch(
            f"{_MODULE}.write_positions",
            return_value={"new": 1, "duplicate": 0, "error": 0, "total_processed": 1, "items": []},
        ),
        patch(
            f"{_MODULE}.mark_expired_positions",
            return_value={"marked_expired": 1},
        ),
        patch(
            f"{_MODULE}.purge_expired_positions",
            return_value={"purged": 0},
        ),
        patch(f"{_MODULE}.run_matching", return_value=([], {})),
    ):
        resp = client.post("/test_user/run-cycle")

    assert resp.status_code == 200
    data = resp.json()
    assert data["collection"]["expired"] == 1
    assert data["collection"]["purged"] == 0
    assert data["collection"]["new"] == 1
