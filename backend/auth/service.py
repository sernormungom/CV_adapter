"""Auth domain operations against the database.

Covers authentication, server-side sessions, hierarchical user provisioning,
and the invite → set-password flow. Each function takes an explicit SQLAlchemy
session and commits its own unit of work.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.auth.security import (
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from backend import config
from backend.config import INVITE_TTL_HOURS, SESSION_TTL_HOURS
from backend.db.models import (
    ROLE_VALUES,
    Invite,
    TAConsultant,
    User,
    UserRole,
    UserRoleAssignment,
    UserSession,
)
from backend.authz import policy
from backend.sites.service import SiteConfigurationError, require_active_site


class AuthError(Exception):
    """Domain error; routes map these to 4xx responses."""

    status_code = 400


class UserExistsError(AuthError):
    status_code = 409


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC-aware for safe comparison.

    Postgres returns tz-aware values; some drivers (and SQLite in tests) return
    naive ones. Treat naive timestamps as UTC.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def normalize_username(raw: str) -> str:
    """Strip an @domain suffix; the local part is the login handle / store key."""
    return raw.strip().split("@")[0].strip().lower()


# ── Authentication ────────────────────────────────────────────────────────

def authenticate(db: Session, identifier: str, password: str) -> User | None:
    """Return the user if identifier (username or email) + password match.

    Every failure — unknown account, inactive, wrong password, or locked out —
    returns None, because the login route maps None to one generic
    `401 Invalid credentials`. A distinct "account locked" answer would tell an
    attacker which usernames exist and which ones they have already driven to
    the threshold, which is the opposite of what throttling is for (STAB-2).
    """
    ident = identifier.strip().lower()
    user = db.scalar(
        select(User).where(
            or_(User.username == normalize_username(ident), User.email == ident)
        )
    )
    if user is None or not user.is_active or not user.password_hash:
        return None
    if _is_locked_out(user):
        # Refuse without checking the password and without touching either
        # counter: an attempt made *inside* an active lockout must not extend
        # it, or an attacker could hold an account locked indefinitely simply
        # by continuing to probe it.
        return None
    if not verify_password(password, user.password_hash):
        _record_failed_login(db, user)
        return None
    if user.failed_login_attempts or user.locked_until is not None:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()
    return user


def _is_locked_out(user: User) -> bool:
    return user.locked_until is not None and _aware(user.locked_until) > _now()


def _record_failed_login(db: Session, user: User) -> None:
    """Count one failed password check, locking the account at the threshold.

    A hard lockout (`config.LOGIN_MAX_FAILED_ATTEMPTS` failures →
    `config.LOGIN_LOCKOUT_MINUTES` of refusal), not exponential backoff. The
    simpler mechanism is adequate for a pilot with a handful of named accounts,
    and its accepted trade-off is that anyone who knows a real username can
    force that account into a 15-minute lockout by failing five logins on
    purpose — a self-clearing nuisance (and an admin `set_password` clears it
    at once), not data loss or a lasting availability risk.

    The counter is reset when the lock is set, so it always means "consecutive
    failures since the last success or lockout". Leaving it at the threshold
    instead would re-lock the account on the first wrong password after the
    window elapsed, which penalises the forgetful user this is not aimed at.

    Throttling is per account, not per source IP: no IP is visible at this
    layer today (no reverse-proxy header contract), and per-account is what the
    stated threat — guessing against a known identity — needs.

    Two near-simultaneous failures can both read the same count and undercount
    by one. Deliberately not row-locked: locking a counter this coarse costs
    more than the one extra guess it would prevent
    (`docs/plans/v2-pilot.md` §1 stop rule).
    """
    attempts = (user.failed_login_attempts or 0) + 1
    if attempts >= config.LOGIN_MAX_FAILED_ATTEMPTS:
        user.failed_login_attempts = 0
        user.locked_until = _now() + timedelta(
            minutes=config.LOGIN_LOCKOUT_MINUTES
        )
    else:
        user.failed_login_attempts = attempts
    db.commit()


# ── Sessions ──────────────────────────────────────────────────────────────

def create_session(db: Session, user: User) -> str:
    """Create a session row and return the raw token for the cookie."""
    raw = generate_token()
    db.add(
        UserSession(
            token_hash=hash_token(raw),
            user_id=user.id,
            expires_at=_now() + timedelta(hours=SESSION_TTL_HOURS),
        )
    )
    db.commit()
    return raw


def resolve_session(db: Session, raw_token: str) -> User | None:
    """Return the live user for a session token, or None if invalid/expired."""
    sess = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if sess is None:
        return None
    if _aware(sess.expires_at) <= _now():
        db.delete(sess)
        db.commit()
        return None
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(db: Session, raw_token: str) -> None:
    sess = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if sess is not None:
        db.delete(sess)
        db.commit()


def revoke_all_sessions_for_user(db: Session, user_id: uuid.UUID) -> int:
    """Force-expire every active session belonging to `user_id`.

    Incident-response containment (`docs/legal/breach-runbook.md` §2 step 1):
    a device is reported lost/stolen while its owner's cloud session may still
    be live in a saved browser on it. `revoke_session` alone can't do this —
    it only revokes a session given its own raw token, which an admin
    responding to a report doesn't have. Returns the number revoked.

    The commit is the caller's whole transaction, not just these deletes: a
    caller that has already staged a change on the same Session publishes both
    together. `set_password` relies on that to change a password and close its
    sessions atomically.
    """
    sessions = db.scalars(
        select(UserSession).where(UserSession.user_id == user_id)
    ).all()
    for sess in sessions:
        db.delete(sess)
    db.commit()
    return len(sessions)


# ── Provisioning (hierarchical invite) ────────────────────────────────────

def normalize_roles(
    role: UserRole | str | None,
    roles: "Sequence[UserRole | str] | None",
) -> list[str]:
    """Resolve the `role=` / `roles=` argument pair into validated role values.

    Accepts either form: `role=` for the common single-role case, `roles=` for a
    genuinely multi-role account (`docs/DECISIONS.md` P-004's Admin+Consultant).
    Supplying both, or neither, is a programming error rather than a merge.
    """
    if role is not None and roles is not None:
        raise AuthError("Pass either role= or roles=, not both")
    given = [role] if role is not None else list(roles or [])
    if not given:
        raise AuthError("At least one role is required")

    resolved: list[str] = []
    for item in given:
        value = item.value if isinstance(item, UserRole) else item
        if value not in ROLE_VALUES:
            raise AuthError(f"Invalid role: {value!r}")
        if value not in resolved:
            resolved.append(value)
    return resolved


def create_user(
    db: Session,
    *,
    email: str,
    role: UserRole | str | None = None,
    roles: "Sequence[UserRole | str] | None" = None,
    full_name: str | None = None,
    city: str | None = None,
    username: str | None = None,
    created_by: User | None = None,
    ta_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
) -> tuple[User, str]:
    """Provision an account (no password yet) and return (user, raw_invite_token).

    Pass `role=` for one role or `roles=` for several. A consultant is linked to
    a TA: either the explicit `ta_id`, or the creating TA when `created_by`
    holds the TA role.
    """
    role_values = normalize_roles(role, roles)

    email_norm = email.strip().lower()
    username_norm = normalize_username(username or email_norm)

    existing = db.scalar(
        select(User).where(
            or_(User.email == email_norm, User.username == username_norm)
        )
    )
    if existing is not None:
        raise UserExistsError("A user with that email or username already exists")

    try:
        if created_by is not None and not policy.is_admin(created_by):
            if site_id is not None and site_id != created_by.site_id:
                raise AuthError("Consultants must be created at your own Site")
            site_id = created_by.site_id
        if site_id is None and ta_id is not None:
            ta = db.get(User, ta_id)
            if ta is not None:
                site_id = ta.site_id
        site = require_active_site(db, site_id)
    except SiteConfigurationError as exc:
        raise AuthError(str(exc)) from exc

    user = User(
        email=email_norm,
        username=username_norm,
        full_name=full_name,
        city=city,
        password_hash=None,
        created_by_id=created_by.id if created_by else None,
        site_id=site.id,
    )
    db.add(user)
    db.flush()  # assign user.id

    for value in role_values:
        db.add(UserRoleAssignment(user_id=user.id, role=value))

    # Link consultant ↔ TA. An explicitly supplied `ta_id` is validated: the
    # relationship is an authorization input, so it must point at an account
    # that actually holds the TA role (see policy.holds_ta_role). The implicit
    # path is already safe — it only fires when `created_by` holds TA.
    link_ta_id = ta_id
    if link_ta_id is not None and not policy.holds_ta_role(db, link_ta_id):
        raise AuthError("ta_id must belong to an account holding the TA role")
    if (
        link_ta_id is None
        and created_by is not None
        and created_by.has_role(UserRole.ta)
    ):
        link_ta_id = created_by.id
    if UserRole.consultant.value in role_values and link_ta_id is not None:
        ta = db.get(User, link_ta_id)
        if ta.site_id != user.site_id:
            raise AuthError("TA assignments must stay within one Site")
        db.add(TAConsultant(ta_id=link_ta_id, consultant_id=user.id))

    raw_invite = _create_invite(db, user)
    db.commit()
    return user, raw_invite


def validate_password_policy(
    password: str, *, username: str | None = None, email: str | None = None
) -> None:
    """Raise AuthError unless `password` satisfies the account password policy.

    Both places a password is ever set — `accept_invite` (an account's first
    password) and `set_password` (admin recovery) — call this, so the policy
    exists once rather than as two copies that drift. The rules are exactly the
    ones the product owner confirmed for the pilot (STAB-2,
    `docs/plans/v2-pilot.md` §7): at least `config.PASSWORD_MIN_LENGTH`
    characters, one uppercase letter, one number, one symbol, and a refusal of
    the two trivial guesses that are always in scope at both call sites — the
    account's own username and its email local part, compared
    case-insensitively. No further composition rules were requested and none
    are added here.

    A "symbol" is any character that is not a letter or a digit, which includes
    a space: the point is to widen the search space, not to police punctuation.

    The pilot seed's 15-character bar (`backend/bootstrap/pilot.py`) is a
    separate, stricter bootstrap requirement that still applies on top of this
    one, since `seed_pilot` sets those passwords through `accept_invite`.
    """
    if len(password) < config.PASSWORD_MIN_LENGTH:
        raise AuthError(
            f"Password must be at least {config.PASSWORD_MIN_LENGTH} characters"
        )
    if not any(c.isupper() for c in password):
        raise AuthError("Password must include an uppercase letter")
    if not any(c.isdigit() for c in password):
        raise AuthError("Password must include a number")
    if all(c.isalnum() for c in password):
        raise AuthError("Password must include a symbol")
    trivial = {
        normalize_username(username or ""),
        normalize_username(email or ""),
    }
    trivial.discard("")
    if password.lower() in trivial:
        raise AuthError(
            "Password must not be the account's username or email address"
        )


def set_password(db: Session, identifier: str, new_password: str) -> User:
    """Reset a user's password directly (admin recovery for a forgotten login).

    `identifier` is an email or username. Raises AuthError if no such user or
    the password fails `validate_password_policy`.

    Also **revokes every live session for the account** and clears any active
    login lockout, so this one command is a complete containment action: an
    admin resetting a possibly-compromised password no longer leaves the
    attacker's already-open session cookie working until it expires (STAB-2;
    `docs/legal/breach-runbook.md` §2 step 1). The revoke deliberately happens
    *before* the commit that publishes the new hash, so the two land in a
    single unit of work — a failure part-way cannot produce the worst state,
    a changed password with the old sessions still live.
    """
    ident = identifier.strip().lower()
    user = db.scalar(
        select(User).where(
            or_(User.username == normalize_username(ident), User.email == ident)
        )
    )
    if user is None:
        raise AuthError(f"No user with email or username {identifier!r}")
    validate_password_policy(
        new_password, username=user.username, email=user.email
    )
    user.password_hash = hash_password(new_password)
    # A reset is also the admin's unlock path for a throttled account.
    user.failed_login_attempts = 0
    user.locked_until = None
    revoke_all_sessions_for_user(db, user.id)  # commits the pending hash too
    db.commit()  # no-op if the revoke already committed; keeps this self-evident
    return user


def _create_invite(db: Session, user: User) -> str:
    raw = generate_token()
    db.add(
        Invite(
            token_hash=hash_token(raw),
            user_id=user.id,
            expires_at=_now() + timedelta(hours=INVITE_TTL_HOURS),
        )
    )
    return raw


def accept_invite(db: Session, raw_token: str, password: str) -> User:
    """Set the account's first password by redeeming a valid invite."""
    invite = db.scalar(
        select(Invite).where(Invite.token_hash == hash_token(raw_token))
    )
    if invite is None or invite.accepted_at is not None:
        raise AuthError("Invalid or already-used invite")
    if _aware(invite.expires_at) <= _now():
        raise AuthError("Invite has expired")

    user = db.get(User, invite.user_id)
    if user is None:
        raise AuthError("Invite refers to a missing user")
    # After the lookup, not before it: the policy's trivial-guess rule needs
    # this account's username and email.
    validate_password_policy(password, username=user.username, email=user.email)

    # No sessions are revoked here, unlike `set_password`: this is the
    # account's *first* password, and `authenticate` refuses an account with no
    # `password_hash`, so neither a session nor a lockout can exist yet.
    user.password_hash = hash_password(password)
    invite.accepted_at = _now()
    db.commit()
    return user
