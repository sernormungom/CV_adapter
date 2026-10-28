"""Shared test fixtures: an in-memory SQLite DB and an app TestClient bound to it.

Data-root isolation lives one level up, in the repository-root `conftest.py`,
because it has to run before `backend.config` is imported.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.db.models  # noqa: F401  (register models on Base.metadata)
from backend.auth import service as auth_service
from backend.cv_publish.throttle import public_capability_throttle
from backend.db.base import Base
from backend.db.models import Site, User, UserRole
from backend.db.session import get_db
from backend.main import app


@pytest.fixture
def Session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    # Each disposable database gets the sole pilot Site explicitly. This is
    # fixture-local state, not the staging/dev seed command, and lets every
    # existing service test provision accounts through the real V2 invariant.
    session = factory()
    try:
        session.add(Site(code="GBG", name="Test Göteborg", is_active=True))
        session.commit()
    finally:
        session.close()
    return factory


@pytest.fixture
def db(Session):
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(Session):
    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _fresh_public_capability_budget():
    """Give every test the CV-publish capability throttle's full budget.

    That throttle (STAB-5) is deliberately one global in-process counter, so
    without this the whole test session would share a single 60-request minute
    and a public-route test would fail or pass depending on what ran before it.
    Autouse rather than opt-in: the coupling is invisible at the call site — a
    test that never mentions the throttle is exactly the one that would be
    broken by a neighbour spending the budget.
    """
    public_capability_throttle.reset()
    yield
    public_capability_throttle.reset()


# ── Disposable TA + Consultant authorization fixture ────────────────────────
# V2 M1: authorization and browser tests kept re-deriving the same cast of
# actors with copy-pasted `_ta`/`_consultant`/`_login` helpers in six test
# modules. One fixture now owns the cast, so a milestone that changes the
# identity model (M2 adds Sites and multi-role memberships) has one place to
# update instead of six.
#
# Every actor is created inside the test's own throwaway in-memory database and
# dies with it — nothing is seeded into a shared or long-lived environment. The
# plan (M1) allows staging to seed the same shape, and allows a *temporary* TA
# in a production canary only under explicit separate authorization; it forbids
# adding a third permanent production identity.

# Satisfies the account password policy (STAB-2): >= 12 characters with an
# uppercase letter, a number and a symbol, and not any actor's username or
# email local part. `tests/test_authz_fixture.py` pins that against the real
# validator, so a future policy change fails there rather than in every test
# that logs in.
ACTOR_PASSWORD = "Fixture-Password-9-not-a-secret"


@dataclass(frozen=True)
class Actor:
    """One provisioned, password-accepted account plus its login credentials.

    `user` is a fully-loaded but *detached* ORM instance (see the `actors`
    fixture): safe to read, and safe to pass to authorization helpers that only
    read `id`/`role`/`username`. Pass `user_id` when a foreign key is all that
    is wanted.
    """

    user: User
    user_id: uuid.UUID
    username: str
    email: str
    password: str = ACTOR_PASSWORD


@dataclass(frozen=True)
class Actors:
    """The standard authorization cast.

    `consultant` is assigned to `ta`. `other_ta` is a TA with **no**
    relationship to `consultant`, which is the negative case every
    relationship check needs. `admin` is globally scoped.
    """

    ta: Actor
    consultant: Actor
    other_ta: Actor
    admin: Actor

    def login(self, client: TestClient, actor: Actor) -> None:
        """Log `actor` in on `client`, asserting it worked.

        Logs in by username, not email, because that is what the real login
        form submits and what `normalize_username` operates on.
        """
        resp = client.post(
            "/api/auth/login",
            json={"identifier": actor.username, "password": actor.password},
        )
        assert resp.status_code == 200, resp.text

    def logout(self, client: TestClient) -> None:
        client.post("/api/auth/logout")
        client.cookies.clear()


def _provision(
    db,
    *,
    role: UserRole | None = None,
    roles: list[UserRole] | None = None,
    email: str,
    created_by: User | None = None,
    site_id=None,
) -> User:
    if site_id is None and created_by is None:
        from sqlalchemy import select
        site_id = db.scalar(select(Site.id).where(Site.code == "GBG"))
    user, invite = auth_service.create_user(
        db, email=email, role=role, roles=roles, created_by=created_by, site_id=site_id
    )
    auth_service.accept_invite(db, invite, ACTOR_PASSWORD)
    return user


@pytest.fixture
def actors(Session) -> Actors:
    """Create the disposable TA/Consultant/other-TA/admin cast.

    Uses its own session so it composes with either the `db` fixture (service
    level tests) or the `client` fixture (route level tests) without holding an
    open transaction on the shared in-memory connection while the code under
    test writes.

    The session is fully closed before returning, so each instance is
    `refresh`ed (loading every column) and expunged first. Without that, the
    commit inside `accept_invite` leaves attributes expired and the first
    `actor.user.id` in a test would raise `DetachedInstanceError`.
    """
    session = Session()
    try:
        ta = _provision(session, role=UserRole.ta, email="fixture-ta@example.invalid")
        # created_by a TA is what establishes the TAConsultant association.
        consultant = _provision(
            session,
            role=UserRole.consultant,
            email="fixture-consultant@example.invalid",
            created_by=ta,
        )
        other_ta = _provision(
            session, role=UserRole.ta, email="fixture-other-ta@example.invalid"
        )
        admin = _provision(
            session,
            roles=[UserRole.admin, UserRole.consultant],
            email="fixture-admin@example.invalid",
        )

        second_site = Site(code="MAL", name="Test Malmö", is_active=True)
        session.add(second_site)
        session.flush()
        other_ta.site_id = second_site.id
        session.commit()

        users = (ta, consultant, other_ta, admin)
        for user in users:
            session.refresh(user)
        session.expunge_all()

        ta_a, consultant_a, other_ta_a, admin_a = (
            Actor(
                user=user,
                user_id=user.id,
                username=user.username,
                email=user.email,
            )
            for user in users
        )
        return Actors(
            ta=ta_a, consultant=consultant_a, other_ta=other_ta_a, admin=admin_a
        )
    finally:
        session.close()


@pytest.fixture
def second_pair(Session) -> dict[str, Actor]:
    """A second, entirely separate (TA, Consultant) relationship.

    The `actors` cast has one TA↔Consultant pair plus an unrelated TA with no
    consultant at all. Cross-boundary questions about *shared* objects — a Job
    two Consultants both reach, an Application one of them owns — need a second
    *populated* relationship, which the shared cast deliberately does not model.
    That gap is exactly why the original Slice 1 suite missed a cross-Consultant
    disclosure (`docs/handoffs/slice-1-jobs.md`, S1-P1-R1 finding 1), so the
    fixture lives here rather than being re-derived per module.

    Returns `{"ta": Actor, "consultant": Actor}`.
    """
    session = Session()
    try:
        ta = _provision(session, role=UserRole.ta, email="pair2-ta@example.invalid")
        consultant = _provision(
            session,
            role=UserRole.consultant,
            email="pair2-consultant@example.invalid",
            created_by=ta,
        )
        for user in (ta, consultant):
            session.refresh(user)
        session.expunge_all()
        return {
            role: Actor(
                user=user, user_id=user.id, username=user.username, email=user.email
            )
            for role, user in (("ta", ta), ("consultant", consultant))
        }
    finally:
        session.close()
