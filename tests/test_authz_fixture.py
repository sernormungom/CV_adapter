"""The reusable disposable TA/Consultant authorization fixture (V2 M1).

`tests/conftest.py::actors` is the one place that builds the authorization cast
used by relationship, browser, and route tests. These tests protect the
properties every consumer relies on, so a later milestone that reshapes the
identity model (M2 adds Sites and multi-role memberships) finds out here rather
than in six unrelated test modules.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.db.models import Site, User, UserRole
from backend.sites.service import SiteConfigurationError, require_active_site
from backend.authz import policy as authz
from tests.conftest import ACTOR_PASSWORD


def test_cast_has_the_expected_roles(actors):
    assert actors.ta.user.roles == {UserRole.ta.value}
    assert actors.other_ta.user.roles == {UserRole.ta.value}
    assert actors.consultant.user.roles == {UserRole.consultant.value}
    # A normal membership, not a policy bypass: this actor is both roles.
    assert actors.admin.user.roles == {UserRole.admin.value, UserRole.consultant.value}


def test_usernames_are_normalized_from_the_reserved_email_domain(actors):
    # normalize_username strips the @domain suffix.
    assert actors.ta.username == "fixture-ta"
    assert actors.consultant.username == "fixture-consultant"
    assert actors.other_ta.username == "fixture-other-ta"
    assert actors.admin.username == "fixture-admin"
    for actor in (actors.ta, actors.consultant, actors.other_ta, actors.admin):
        assert actor.email.endswith("@example.invalid")


def test_detached_instances_are_fully_loaded(actors):
    """The fixture closes its session, so every attribute a consumer might read
    must already be loaded — otherwise this raises DetachedInstanceError."""
    for actor in (actors.ta, actors.consultant, actors.other_ta, actors.admin):
        assert actor.user.id == actor.user_id
        assert actor.user.username == actor.username
        assert actor.user.is_active is True
        assert actor.user.password_hash  # invite was accepted


def test_relationship_is_the_ta_consultant_pair(actors, db):
    """`consultant` is assigned to `ta` and to nobody else — the positive and
    negative case authorization tests need."""
    assert authz.can_access_consultant(db, actors.ta.user, actors.consultant.user)
    assert not authz.can_access_consultant(
        db, actors.other_ta.user, actors.consultant.user
    )
    # Admin is global; a consultant can always reach itself.
    assert authz.can_access_consultant(db, actors.admin.user, actors.consultant.user)
    assert authz.can_access_consultant(
        db, actors.consultant.user, actors.consultant.user
    )

    assert authz.consultant_ids_for_ta(db, actors.ta.user_id) == {
        actors.consultant.user_id
    }
    assert authz.consultant_ids_for_ta(db, actors.other_ta.user_id) == set()
    assert (
        authz.ta_id_for_consultant_username(db, actors.consultant.username)
        == actors.ta.user_id
    )


def test_unrelated_ta_is_forbidden_not_merely_empty(actors, db):
    """The negative case must be a 403, not a silent empty result."""
    with pytest.raises(HTTPException) as forbidden:
        authz.ensure_consultant_access(
            db, actors.other_ta.user, actors.consultant.username
        )
    assert forbidden.value.status_code == 403


def test_policy_does_not_read_site_for_owned_access(actors, db):
    """A hypothetical nonmatching Site cannot silently become an auth input.

    The second Site is inactive, so this does not create the invalid two-active
    pilot configuration.  The TA relationship remains the sole ownership fact.
    """
    other_site = Site(code="NOT-AUTH", name="Hypothetical", is_active=False)
    db.add(other_site)
    db.flush()
    consultant = db.get(User, actors.consultant.user_id)
    assert consultant is not None
    consultant.site_id = other_site.id
    db.flush()
    assert authz.can_access_consultant(db, actors.ta.user, consultant)


def test_multiple_active_sites_require_explicit_provisioning_site(db):
    other = Site(code="SECOND", name="Second Site", is_active=True)
    db.add(other)
    db.commit()
    assert require_active_site(db, other.id).id == other.id
    with pytest.raises(SiteConfigurationError):
        require_active_site(db)


@pytest.mark.parametrize(
    "which", ["ta", "consultant", "other_ta", "admin"]
)
def test_every_actor_can_log_in(actors, client, which):
    actor = getattr(actors, which)
    actors.login(client, actor)
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["username"] == actor.username


def test_logout_clears_the_session(actors, client):
    actors.login(client, actors.ta)
    assert client.get("/api/auth/me").status_code == 200
    actors.logout(client)
    assert client.get("/api/auth/me").status_code == 401


def test_the_fixture_password_satisfies_the_real_policy():
    """Asserted against the live validator, not a copied number: if the policy
    is tightened again, this fails alone and points at the fixture, instead of
    every test in the suite that logs an actor in (STAB-2)."""
    from backend.auth import service

    service.validate_password_policy(
        ACTOR_PASSWORD, username="fixture", email="fixture@example.com"
    )
