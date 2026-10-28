"""Phase 2e (backend): consultant-access enforcement, gated by AUTH_ENFORCED.

Deny paths (401/403) are checked through the real app via the client — the
dependency raises before the handler runs, so no data layer is touched.
"""

import pytest
from fastapi import HTTPException

from backend import config, main
from backend.auth import service
from backend.db.models import UserRole
from backend.authz.dependencies import check_consultant_access


def _activate(db, role, email):
    user, invite = service.create_user(db, email=email, role=role)
    service.accept_invite(db, invite, "Password123!x")
    return user


# ── Unit: the flag-aware check ────────────────────────────────────────────

def test_noop_when_flag_off(db, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", False)
    # Even with no user and an unknown consultant, nothing is raised.
    check_consultant_access(db, None, "whoever")


def test_requires_auth_when_on(db, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    with pytest.raises(HTTPException) as e:
        check_consultant_access(db, None, "alice")
    assert e.value.status_code == 401


def test_self_allowed_others_blocked(db, monkeypatch):
    alice = _activate(db, UserRole.consultant, "alice@x.com")
    _activate(db, UserRole.consultant, "bob@x.com")
    ta = _activate(db, UserRole.ta, "ta@x.com")
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)

    check_consultant_access(db, alice, "alice")  # self → ok

    with pytest.raises(HTTPException) as other:
        check_consultant_access(db, alice, "bob")
    assert other.value.status_code == 403

    with pytest.raises(HTTPException) as unassigned:
        check_consultant_access(db, ta, "alice")  # TA not assigned
    assert unassigned.value.status_code == 403


# ── Integration: deny paths through the real app ──────────────────────────

# The consultant-scoped router mounted behind `require_consultant_access` used
# to be exercised here through the learning router — the only route ever
# mounted with that dependency (Slice 1 retired the V1 dashboard router that
# stood in for it before that). V3 packet S2 deleted the learning router
# entirely along with the rest of the verdict-fed learning path
# (`docs/plans/v3/decisions.md` V3-P-008 decision 5), so there is no longer
# any route mounted behind `require_consultant_access` to probe at the HTTP
# level. The dependency's own logic (`check_consultant_access`) keeps its
# direct unit coverage above; a future consultant-scoped route should restore
# an HTTP-level pair of tests like the two this comment used to introduce.


def test_profile_route_401_when_enforced(client, monkeypatch):
    """Probes `require_username_access` through a live route.

    Used to probe the now-removed `GET /api/profiles/{username}/accepted-
    positions` (STAB-7 — the route had zero callers in any shipped page).
    Repointed to the CV-import preview route, gated by the identical
    dependency, so this gate keeps real route-level coverage rather than
    losing the assertion with the route it used to ride on.
    """
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    assert client.get("/api/profiles/alice/cv-import/preview").status_code == 401


# ── Phase 4a sweep: CV pipeline, assistant, and the profiles list ─────────
#
# `POST /api/generate-cv` used to be swept here. V2 Slice 3 retired it: it
# generated synchronously into whatever CV the editor held (the base-overwrite
# P-009 forbids) over a connection the timeout inventory records as "routinely
# exceeds the budget on a full CV" (`docs/evidence/m1-timeout-inventory.md` #2).
# Generation is now durable and Application-owned. The base CV artifact keeps
# the same consultant-scoped gate, so it is swept in its place.


def test_the_retired_synchronous_generate_route_is_gone(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", False)
    r = client.post(
        "/api/generate-cv",
        json={"consultant_id": "alice", "job_description": "x"},
    )
    assert r.status_code == 404


def test_base_cv_state_401_when_enforced(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    assert client.get("/api/cv/base/alice").status_code == 401


def test_base_cv_state_403_for_other_consultant(client, Session, monkeypatch):
    s = Session()
    _activate(s, UserRole.consultant, "alice@x.com")
    _activate(s, UserRole.consultant, "bob@x.com")
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "alice", "password": "Password123!x"}
    )
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    assert client.get("/api/cv/base/bob").status_code == 403
    r = client.put(
        "/api/cv/base/bob", json={"state": {"iName": "x"}, "expected_revision": 0}
    )
    assert r.status_code == 403


def test_chat_401_when_enforced(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    r = client.post(
        "/api/chat", json={"username": "alice", "message": "hi"}
    )
    assert r.status_code == 401


def test_the_synchronous_cv_importer_is_disabled_for_release_cut_a(client):
    """`POST /api/import-cv` is gone (V2 Slice 3, Release Cut A, `S3-P1-R1`).

    `docs/plans/v2-pilot.md` Slice 3 requires the old synchronous importer
    disabled until Slice 4 "rather than carrying a known 504 path". It ran text
    extraction plus an LLM call on the open request, over the connection
    `docs/evidence/m1-timeout-inventory.md` measures at a 60-second ceiling, with
    no idempotency key — so a client that retried a timed-out import duplicated
    the work at the origin. It was also the last editor path that wrote the
    consultant's `profile.md` regardless of which artifact was being edited,
    which is the P-009 boundary this slice exists to establish.

    Two tests were replaced by this one: they asserted the route's authorization
    and its error-shaping, both of which are moot once nothing is routed. Slice 4
    reinstates the behaviour against the durable contract, and its own tests with
    it. This test is what makes the boundary fail loudly if the synchronous route
    is reintroduced by a merge rather than by that slice.
    """
    routes = {
        getattr(route, "path", None) for route in main.app.routes
    }
    assert "/api/import-cv" not in routes

    # And it is not reachable under any method.
    for call in (client.post, client.get, client.put):
        assert call("/api/import-cv").status_code == 404

    # The extraction components Slice 4 reuses are deliberately untouched.
    import backend.cv_importer.cv_parser  # noqa: F401
    import backend.cv_importer.experience_extractor  # noqa: F401
    import backend.cv_importer.experience_writer  # noqa: F401


def test_profiles_list_requires_ta_or_admin(client, Session, monkeypatch):
    # No session → 401.
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    assert client.get("/api/profiles").status_code == 401
    monkeypatch.setattr(config, "AUTH_ENFORCED", False)

    # Consultant → 403.
    s = Session()
    _activate(s, UserRole.consultant, "alice@x.com")
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "alice", "password": "Password123!x"}
    )
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    assert client.get("/api/profiles").status_code == 403


def test_profiles_list_allowed_for_ta(client, Session, monkeypatch):
    s = Session()
    _activate(s, UserRole.ta, "ta@x.com")
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "ta", "password": "Password123!x"}
    )
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    assert client.get("/api/profiles").status_code == 200
