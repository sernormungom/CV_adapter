"""Phase 2b tests: consultant authz and tenancy routes.

Job sources were removed when collection moved to the local desktop tools, so the
source CRUD/loader tests are gone; the RBAC checks now exercise the consultants
route (same `_resolve_ta_id` rules).
"""

import pytest
from fastapi import HTTPException

from backend.auth import service
from backend.db.models import UserRole
from backend.authz import policy as authz


def _user(db, role, email, **kwargs):
    user, invite = service.create_user(db, email=email, role=role, **kwargs)
    service.accept_invite(db, invite, "Password123!x")
    return user


def _login(client, Session, role, email, **kwargs):
    s = Session()
    _user(s, role, email, **kwargs)
    s.close()
    r = client.post(
        "/api/auth/login",
        json={"identifier": email.split("@")[0], "password": "Password123!x"},
    )
    assert r.status_code == 200


# ── Authorization ─────────────────────────────────────────────────────────

def test_consultant_access_rules(db):
    ta = _user(db, UserRole.ta, "ta@x.com")
    other_ta = _user(db, UserRole.ta, "ta2@x.com")
    admin = _user(db, UserRole.admin, "admin@x.com")
    consultant, _ = service.create_user(
        db, email="c@x.com", role=UserRole.consultant, created_by=ta
    )

    assert authz.can_access_consultant(db, ta, consultant) is True
    assert authz.can_access_consultant(db, other_ta, consultant) is False
    assert authz.can_access_consultant(db, admin, consultant) is True
    assert authz.can_access_consultant(db, consultant, consultant) is True
    assert authz.consultant_ids_for_ta(db, ta.id) == {consultant.id}
    assert authz.ta_id_for_consultant_username(db, consultant.username) == ta.id


def test_ensure_consultant_access_raises(db):
    ta = _user(db, UserRole.ta, "ta@x.com")
    other_ta = _user(db, UserRole.ta, "ta2@x.com")
    consultant, _ = service.create_user(
        db, email="c@x.com", role=UserRole.consultant, created_by=ta
    )
    with pytest.raises(HTTPException) as missing:
        authz.ensure_consultant_access(db, ta, "nobody")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as forbidden:
        authz.ensure_consultant_access(db, other_ta, consultant.username)
    assert forbidden.value.status_code == 403


# ── Routes / RBAC ─────────────────────────────────────────────────────────

def test_consultant_cannot_access_ta_routes(client, Session):
    _login(client, Session, UserRole.consultant, "c@x.com")
    assert client.get("/api/ta/consultants").status_code == 403


def test_admin_must_specify_ta_id(client, Session):
    _login(client, Session, UserRole.admin, "admin@x.com")
    assert client.get("/api/ta/consultants").status_code == 400


# ── ta_id must name an actual TA ───────────────────────────────────────────
# The TA↔consultant relationship is an authorization input: a TA reaches a
# consultant *through* it, and CV-view notifications route through it. A
# well-formed UUID that points at a consultant (or at nothing) must therefore be
# refused, not merely parsed.

def test_admin_ta_id_pointing_at_a_consultant_is_refused(client, Session):
    s = Session()
    ta, invite = service.create_user(s, email="ta@x.com", role=UserRole.ta)
    service.accept_invite(s, invite, "Password123!x")
    consultant, _ = service.create_user(
        s, email="c@x.com", role=UserRole.consultant, created_by=ta
    )
    consultant_id = str(consultant.id)
    admin, admin_invite = service.create_user(
        s, email="admin@x.com", role=UserRole.admin
    )
    service.accept_invite(s, admin_invite, "Password123!x")
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "admin", "password": "Password123!x"}
    )

    r = client.get(f"/api/ta/consultants?ta_id={consultant_id}")
    assert r.status_code == 404, r.text


def test_admin_ta_id_for_a_nonexistent_user_is_refused(client, Session):
    _login(client, Session, UserRole.admin, "admin@x.com")
    import uuid as _uuid

    r = client.get(f"/api/ta/consultants?ta_id={_uuid.uuid4()}")
    assert r.status_code == 404, r.text


def test_assignment_cannot_be_created_under_a_non_ta(client, Session):
    """The write path, not just the read: provisioning a consultant under a
    consultant's id must not produce a TAConsultant row."""
    s = Session()
    ta, invite = service.create_user(s, email="ta@x.com", role=UserRole.ta)
    service.accept_invite(s, invite, "Password123!x")
    consultant, _ = service.create_user(
        s, email="c@x.com", role=UserRole.consultant, created_by=ta
    )
    consultant_id = str(consultant.id)
    admin, admin_invite = service.create_user(
        s, email="admin@x.com", role=UserRole.admin
    )
    service.accept_invite(s, admin_invite, "Password123!x")
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "admin", "password": "Password123!x"}
    )

    r = client.post(
        f"/api/ta/consultants?ta_id={consultant_id}",
        json={"email": "victim@x.com"},
    )
    assert r.status_code == 404, r.text
    # And no account was provisioned as a side effect of the rejected call.
    s = Session()
    try:
        from sqlalchemy import select

        from backend.db.models import User

        assert s.scalar(select(User).where(User.email == "victim@x.com")) is None
    finally:
        s.close()


def test_service_refuses_a_ta_id_that_is_not_a_ta(db):
    ta = _user(db, UserRole.ta, "ta@x.com")
    consultant, _ = service.create_user(
        db, email="c@x.com", role=UserRole.consultant, created_by=ta
    )
    with pytest.raises(service.AuthError, match="TA role"):
        service.create_user(
            db,
            email="misrouted@x.com",
            role=UserRole.consultant,
            ta_id=consultant.id,
        )


def test_ta_consultants_listing_reflects_assignment(client, Session):
    s = Session()
    ta, invite = service.create_user(s, email="ta@x.com", role=UserRole.ta)
    service.accept_invite(s, invite, "Password123!x")
    service.create_user(s, email="c@x.com", role=UserRole.consultant, created_by=ta)
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "ta", "password": "Password123!x"}
    )
    body = client.get("/api/ta/consultants").json()
    assert body["total"] == 1
    assert body["consultants"][0]["username"] == "c"
    # New consultants default to actively looking.
    assert body["consultants"][0]["actively_looking"] is True


# ── Actively-looking toggle ─────────────────────────────────────────────────

def test_ta_can_toggle_looking_for_own_consultant(client, Session):
    s = Session()
    ta, invite = service.create_user(s, email="ta@x.com", role=UserRole.ta)
    service.accept_invite(s, invite, "Password123!x")
    service.create_user(s, email="c@x.com", role=UserRole.consultant, created_by=ta)
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "ta", "password": "Password123!x"}
    )

    r = client.post("/api/ta/consultants/c/looking", json={"actively_looking": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"username": "c", "actively_looking": False}

    body = client.get("/api/ta/consultants").json()
    assert body["consultants"][0]["actively_looking"] is False


def test_ta_cannot_toggle_looking_for_unassigned_consultant(client, Session):
    s = Session()
    ta, invite = service.create_user(s, email="ta@x.com", role=UserRole.ta)
    service.accept_invite(s, invite, "Password123!x")
    other_ta, _ = service.create_user(s, email="ta2@x.com", role=UserRole.ta)
    service.create_user(s, email="c@x.com", role=UserRole.consultant, created_by=other_ta)
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "ta", "password": "Password123!x"}
    )
    r = client.post("/api/ta/consultants/c/looking", json={"actively_looking": False})
    assert r.status_code == 404
