"""Controlled seed for the three fabricated V2 Pilot identities.

Migrations build a clean schema only.  This explicit bootstrap is the sole
place that creates pilot accounts, so test databases and deployed environments
never receive shared seed state as a side effect of schema work.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import service as auth_service
from backend.db.models import Site, TAConsultant, User, UserRole
from backend.sites.service import SiteConfigurationError, require_active_site

PILOT_SITE_CODE = "GBG"
PILOT_USERNAMES = frozenset({"patrik.artman", "norberto.munoz", "mo.chen"})


class PilotSeedError(RuntimeError):
    pass


def seed_pilot(db: Session, *, password: str) -> None:
    """Create (or validate) exactly the approved fabricated Pilot cast.

    The operation is intentionally conservative: it never deletes or changes
    pre-existing accounts.  A rerun is a validation no-op only when the exact
    three-account relationship/role shape is already present.
    """
    if len(password) < 15:
        raise PilotSeedError("PILOT_SEED_PASSWORD must be at least 15 characters")
    # The general account password policy applies to the seeded cast too — this
    # function sets all three passwords through `accept_invite`, which enforces
    # it. Checked here, up front, so a non-compliant password fails before
    # anything is created: a refusal part-way through would leave a half-seeded
    # database that every later rerun rejects as "non-pilot users" (STAB-2).
    # The 15-character bar above is bootstrap-only and stays stricter.
    try:
        auth_service.validate_password_policy(password)
    except auth_service.AuthError as exc:
        raise PilotSeedError(f"PILOT_SEED_PASSWORD rejected: {exc}") from exc

    sites = {s.code: s for s in db.scalars(select(Site))}
    for code in ("GBG", "MAL", "STH", "UPS"):
        if code not in sites:
            db.add(Site(code=code, name=code, is_active=True))
    db.commit()
    site = require_active_site(db, code="GBG")

    existing = list(db.scalars(select(User).order_by(User.username)))
    if existing:
        if {user.username for user in existing} != PILOT_USERNAMES:
            raise PilotSeedError(
                "Refusing to mix the pilot seed with existing non-pilot users"
            )
        _validate_seed_shape(db, existing)
        return

    patrik, invite = auth_service.create_user(
        db,
        site_id=site.id,
        email="patrik.artman@pilot.invalid",
        username="patrik.artman",
        full_name="Patrik Artman",
        role=UserRole.ta,
    )
    auth_service.accept_invite(db, invite, password)
    norberto, invite = auth_service.create_user(
        db,
        site_id=site.id,
        email="norberto.munoz@pilot.invalid",
        username="norberto.munoz",
        full_name="Norberto Muñoz",
        roles=[UserRole.admin, UserRole.consultant],
        ta_id=patrik.id,
    )
    auth_service.accept_invite(db, invite, password)
    mo, invite = auth_service.create_user(
        db,
        site_id=site.id,
        email="mo.chen@pilot.invalid",
        username="mo.chen",
        full_name="Mo Chen",
        role=UserRole.consultant,
        ta_id=patrik.id,
    )
    auth_service.accept_invite(db, invite, password)
    _validate_seed_shape(db, [patrik, norberto, mo])


def _validate_seed_shape(db: Session, users: list[User]) -> None:
    by_username = {user.username: user for user in users}
    if set(by_username) != PILOT_USERNAMES:
        raise PilotSeedError("Pilot accounts do not match the approved cast")
    expected_roles = {
        "patrik.artman": {UserRole.ta.value},
        "norberto.munoz": {UserRole.admin.value, UserRole.consultant.value},
        "mo.chen": {UserRole.consultant.value},
    }
    if {name: user.roles for name, user in by_username.items()} != expected_roles:
        raise PilotSeedError("Pilot account role memberships do not match P-004")
    site = require_active_site(db, code="GBG")
    if any(user.site_id != site.id for user in users):
        raise PilotSeedError("Every pilot account must belong to the active GBG Site")
    expected_pairs = {
        (by_username["patrik.artman"].id, by_username["norberto.munoz"].id),
        (by_username["patrik.artman"].id, by_username["mo.chen"].id),
    }
    actual_pairs = set(
        db.execute(select(TAConsultant.ta_id, TAConsultant.consultant_id)).all()
    )
    if actual_pairs != expected_pairs:
        raise PilotSeedError("Pilot TA-consultant relationships do not match P-004")
