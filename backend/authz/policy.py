"""**The** authorization policy for the application. One module, one place.

Every "may this actor do this?" decision lives here. Routers and services call
these helpers; they do not re-implement role comparisons inline, and
`backend/main.py` composes routers only (`docs/plans/v2-pilot.md` §5). If you
are about to write `if user.has_role(...)` in a route, add a named predicate
here instead — the point of centralising is that the policy can be read, and
tested, in one file.

Two invariants this module exists to hold:

**1. Role membership, never a role bypass.** Roles are a many-to-many
membership (`user_roles`). An account holding Admin *and* Consultant is fully
both; there is no "admin counts as a consultant" special case (V1 had one, and
`docs/DECISIONS.md` P-004 removes it). Ask `has_role`, not `primary_role`.

**2. Opportunities are Site-scoped; consultant records remain assignment-scoped.**
A Site or score row never grants access to another consultant's records.
Admin authority remains unrestricted (V3-P-008).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import (
    Application,
    Job,
    JobDecisionPair,
    TAConsultant,
    User,
    UserRole,
    UserRoleAssignment,
)

# ── Role predicates ────────────────────────────────────────────────────────
#
# Thin, named, and the only way the rest of the app should ask about roles.


def is_admin(user: User) -> bool:
    return user.has_role(UserRole.admin)


def is_ta(user: User) -> bool:
    return user.has_role(UserRole.ta)


def is_consultant(user: User) -> bool:
    return user.has_role(UserRole.consultant)


def has_any_role(user: User, *roles: UserRole | str) -> bool:
    wanted = {r.value if isinstance(r, UserRole) else r for r in roles}
    return bool(wanted & user.roles)


def users_with_role(db: Session, role: UserRole | str) -> list[User]:
    """Every account holding `role`, by username.

    Replaces V1's `select(User).where(User.role == ...)`: role membership is a
    join now, so callers must not filter on a column that no longer exists.
    """
    value = role.value if isinstance(role, UserRole) else role
    return list(
        db.scalars(
            select(User)
            .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
            .where(UserRoleAssignment.role == value)
            .order_by(User.username)
        ).all()
    )


# ── TA ↔ Consultant relationships ─────────────────────────────────────────


def consultant_ids_for_ta(db: Session, ta_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        db.scalars(
            select(TAConsultant.consultant_id).where(
                TAConsultant.ta_id == ta_id
            )
        )
    )


def consultants_for_ta(db: Session, ta_id: uuid.UUID) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(TAConsultant, TAConsultant.consultant_id == User.id)
            .where(TAConsultant.ta_id == ta_id)
            .order_by(User.username)
        )
    )


def holds_ta_role(db: Session, ta_id: uuid.UUID) -> bool:
    """Whether `ta_id` names an existing account that holds the TA role.

    Every write that creates a `TAConsultant` row must check this. The
    relationship is an authorization input — `can_access_consultant` grants a
    TA access through it, and `ta_id_for_consultant_username` routes
    notifications through it — so a row whose `ta_id` points at a consultant
    (or at nothing) would quietly widen access and misroute mail. The FK alone
    is not enough: it proves the id is *a user*, not that it is a TA.
    """
    user = db.get(User, ta_id)
    return user is not None and is_ta(user)


def is_assigned(db: Session, ta_id: uuid.UUID, consultant_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(TAConsultant.id).where(
                TAConsultant.ta_id == ta_id,
                TAConsultant.consultant_id == consultant_id,
            )
        )
        is not None
    )


def ta_id_for_consultant_username(
    db: Session, username: str
) -> uuid.UUID | None:
    """The TA assigned to a consultant (the first, if several), or None."""
    advisee = get_advisee_by_username(db, username)
    if advisee is None:
        return None
    return db.scalar(
        select(TAConsultant.ta_id).where(
            TAConsultant.consultant_id == advisee.id
        )
    )


# ── Consultant-scoped access ──────────────────────────────────────────────
#
# Access rules for a consultant's data:
#   admin       → any consultant
#   ta          → only consultants assigned to them
#   consultant  → only themselves
# Site is deliberately absent from all of them (invariant 2 above).


def get_advisee_by_username(db: Session, username: str) -> User | None:
    """The consultant account behind `username`, or None.

    A "consultant" here means an account that *holds the consultant role* —
    including one that also holds admin. V1 had to list admin as an advisee
    role to make its single-role dual-identity work; with real memberships the
    question is simply whether the consultant role is held.
    """
    return db.scalar(
        select(User)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(
            User.username == username,
            UserRoleAssignment.role == UserRole.consultant.value,
        )
    )


def can_access_consultant(db: Session, actor: User, consultant: User) -> bool:
    if is_admin(actor):
        return True
    if actor.id == consultant.id and is_consultant(actor):
        return True
    if is_ta(actor):
        return is_assigned(db, actor.id, consultant.id)
    return False


def ensure_consultant_access(db: Session, actor: User, username: str) -> User:
    """Return the consultant if `actor` may access them, else raise 403/404."""
    advisee = get_advisee_by_username(db, username)
    if advisee is None:
        raise HTTPException(status_code=404, detail="Consultant not found")
    if not can_access_consultant(db, actor, advisee):
        raise HTTPException(
            status_code=403, detail="Not authorized for this consultant"
        )
    return advisee


# ── Job access (Slice 1) ──────────────────────────────────────────────────
#
# Jobs distribute by Site. Decision-pair rows retain human-decision history.

def consultant_ids_for_job(db: Session, job_id: uuid.UUID) -> set[uuid.UUID]:
    """Every Consultant linked to `job_id`."""
    return set(
        db.scalars(
            select(JobDecisionPair.consultant_id).where(
                JobDecisionPair.job_id == job_id
            )
        )
    )


def operable_consultant_ids(db: Session, actor: User) -> set[uuid.UUID] | None:
    """Which Consultants `actor` may operate a Job for.

    Returns `None` for an unrestricted actor (Admin), which callers must treat
    as "no filter" rather than as an empty set — conflating the two would hide
    every Job from an Admin.
    """
    if is_admin(actor):
        return None
    reachable: set[uuid.UUID] = set()
    if is_consultant(actor):
        reachable.add(actor.id)
    if is_ta(actor):
        reachable |= consultant_ids_for_ta(db, actor.id)
    return reachable


def operable_consultants(db: Session, actor: User) -> list[User]:
    """The Consultant accounts `actor` may act for, by username.

    The User-shaped form of `operable_consultant_ids`, **derived from it** so a
    client picker built on this cannot offer a context the server would then
    refuse. That mattered concretely: the Jobs page used the TA-assignment
    endpoint for Admins too, which requires an explicit `ta_id` and answers 400
    without one — so the pilot Admin+Consultant saw a picker containing only
    themselves and could not act for either ordinary Consultant, while the API
    was perfectly willing.

    Admin's unrestricted sentinel resolves to every account holding the
    consultant role, which is the set an Admin genuinely reaches.
    """
    reachable = operable_consultant_ids(db, actor)
    if reachable is None:
        return users_with_role(db, UserRole.consultant)
    if not reachable:
        return []
    return list(
        db.scalars(
            select(User).where(User.id.in_(reachable)).order_by(User.username)
        )
    )


def can_access_job(db: Session, actor: User, job: Job) -> bool:
    if is_admin(actor):
        return True
    if not same_site(actor, job):
        return False
    if is_ta(actor):
        return True
    if is_consultant(actor):
        if job.is_open and actor.is_active and actor.actively_looking:
            return True
        # History remains reachable, but never restores open opportunities.
        return not job.is_open and actor.id in consultant_ids_for_job(db, job.id)
    return False


def same_site(left, right) -> bool:
    return left.site_id == right.site_id


def ensure_same_site_assignment(ta: User, consultant: User) -> None:
    if not same_site(ta, consultant):
        raise HTTPException(status_code=403, detail="TA assignments must stay within one Site")


def ensure_job_creator(actor: User) -> None:
    if not has_any_role(actor, UserRole.admin, UserRole.ta, UserRole.consultant):
        raise HTTPException(status_code=403, detail="Not authorized to create Jobs")


def ensure_job_access(db: Session, actor: User, job_id: str) -> Job:
    """Return the Job if `actor` may operate it, else raise 404.

    **404, not 403, and deliberately so.** A Job's existence is itself content:
    an unrelated TA who probed ids and got 403-for-real / 404-for-absent would
    learn which postings the pilot holds. The acceptance case for this slice is
    that such an actor "learns no Job content", so the unauthorized and the
    unknown answer identically — and neither response body carries a title,
    company, or description.
    """
    try:
        key = uuid.UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Job not found") from None
    job = db.get(Job, key)
    if job is None or not can_access_job(db, actor, job):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def ensure_consultant_operates_job(
    db: Session, job: Job, consultant: User
) -> None:
    """The consultant must belong to the Job's Site; no link is needed."""
    if not same_site(job, consultant):
        raise HTTPException(status_code=403, detail="Consultant and Job belong to different Sites")


# ── Application access (Slice 2) ──────────────────────────────────────────
#
# An Application belongs to exactly one Consultant, so unlike a Job it has no
# shared/relationship split to reason about: the actor may reach it precisely
# when they may operate for that Consultant. That is the same
# `operable_consultant_ids` set the Job rules use, so there is one relationship
# model here too — and Site is absent from this path as well.


def can_access_application(
    db: Session, actor: User, application: Application
) -> bool:
    reachable = operable_consultant_ids(db, actor)
    if reachable is None:
        return True
    return application.consultant_id in reachable


def ensure_application_access(
    db: Session, actor: User, application_id: str
) -> Application:
    """Return the Application if `actor` may operate it, else raise 404.

    **404, not 403**, for the same reason `ensure_job_access` answers that way:
    an Application's existence is itself content. A 403-for-real /
    404-for-absent split would let an unrelated TA probe ids and learn which
    Consultants are being put forward for which postings. The unauthorized and
    the unknown answer identically, and neither body carries any Application
    fact.
    """
    try:
        key = uuid.UUID(str(application_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=404, detail="Application not found"
        ) from None
    application = db.get(Application, key)
    if application is None or not can_access_application(db, actor, application):
        raise HTTPException(status_code=404, detail="Application not found")
    return application


# ── Named capability checks ───────────────────────────────────────────────
#
# Each one answers a single product question and raises the right status, so a
# route body reads as one call rather than an inline role expression.


def ensure_admin(actor: User) -> None:
    if not is_admin(actor):
        raise HTTPException(status_code=403, detail="Admin only")


def ensure_ta_or_admin(actor: User) -> None:
    if not has_any_role(actor, UserRole.ta, UserRole.admin):
        raise HTTPException(status_code=403, detail="TA or admin only")


def ensure_can_list_all_profiles(actor: User) -> None:
    """Listing every consultant profile directory is a TA/admin capability."""
    ensure_ta_or_admin(actor)


# Which roles each role may provision (hierarchical invite). A multi-role
# creator gets the union of what their roles allow.
_CREATABLE_BY_ROLE: dict[str, frozenset[str]] = {
    UserRole.admin.value: frozenset(
        {UserRole.admin.value, UserRole.ta.value, UserRole.consultant.value}
    ),
    UserRole.ta.value: frozenset({UserRole.consultant.value}),
    UserRole.consultant.value: frozenset(),
}


def roles_creatable_by(actor: User) -> frozenset[str]:
    """The union of roles `actor` may provision, across all roles they hold."""
    allowed: set[str] = set()
    for role in actor.roles:
        allowed |= _CREATABLE_BY_ROLE.get(role, frozenset())
    return frozenset(allowed)


def ensure_can_create_roles(actor: User, roles: list[str]) -> None:
    """Guard provisioning: every requested role must be creatable by `actor`."""
    if not roles:
        raise HTTPException(status_code=400, detail="At least one role is required")
    allowed = roles_creatable_by(actor)
    forbidden = sorted(set(roles) - allowed)
    if forbidden:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Not allowed to create role(s): {', '.join(forbidden)}"
            ),
        )
