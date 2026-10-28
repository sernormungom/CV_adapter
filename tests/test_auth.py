"""Auth tests (Phase 2a) — service logic and route RBAC on in-memory SQLite.

These exercise the real service/route code without Postgres. The migration
itself is verified separately with `alembic upgrade head` + `alembic check`
against a live Postgres.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from backend import config
from backend.auth import service
from backend.db.models import TAConsultant, UserRole

# Shared fixtures (Session, db, client) live in tests/conftest.py.


def _activate(db, **kwargs):
    """Create a user and immediately set its password via the invite."""
    user, invite = service.create_user(db, **kwargs)
    service.accept_invite(db, invite, "Password123!x")
    return user


# ── Security primitives ───────────────────────────────────────────────────

def test_password_hash_roundtrip():
    from backend.auth.security import hash_password, verify_password

    h = hash_password("hunter2!")
    assert h != "hunter2!"
    assert verify_password("hunter2!", h)
    assert not verify_password("wrong", h)


def test_a_hash_at_the_real_production_cost_still_round_trips():
    """The suite runs at 4 rounds (`conftest.py`, ~1ms instead of ~0.24s), so
    without this one test nothing would ever exercise the cost production
    actually uses. Hashes here at the real floor and verifies through the
    ordinary code path — `verify_password` reads the cost out of the hash, so
    a 12-round hash verifies as 12 rounds even while the setting says 4, which
    is exactly why lowering it for tests cannot weaken a deployed password."""
    import bcrypt

    from backend import config
    from backend.auth.security import verify_password

    real = bcrypt.hashpw(
        b"hunter2!", bcrypt.gensalt(rounds=config.BCRYPT_MIN_ROUNDS)
    ).decode("utf-8")
    assert real.startswith(f"$2b${config.BCRYPT_MIN_ROUNDS:02d}$")
    assert verify_password("hunter2!", real)
    assert not verify_password("wrong", real)


@pytest.mark.parametrize(
    "env,expected_message",
    [
        ({"APP_ENV": "production"}, "forbidden in production"),
        ({"APP_ENV": "staging"}, "requires APP_ENV"),
        (
            {"APP_ENV": "test", "CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS": "false"},
            "requires APP_ENV",
        ),
    ],
)
def test_the_bcrypt_cost_floor_cannot_be_lowered_outside_a_test_process(
    env, expected_message, monkeypatch
):
    """The cost is tunable only so the suite can run at 4 rounds. This pins the
    guard that keeps that from reaching anything else: re-imports
    `backend.config` under each environment and asserts it refuses. Same shape,
    and the same escape hatch, as the `AUTH_ENFORCED=false` guard beside it."""
    import importlib

    from backend import config as config_module

    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS", "true")
    monkeypatch.setenv("AUTH_ENFORCED", "true")  # isolate this guard from that one
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match=expected_message):
        importlib.reload(config_module)

    # Leave the module as the rest of the session expects to find it.
    monkeypatch.undo()
    importlib.reload(config_module)


def test_the_default_bcrypt_cost_is_the_production_floor(monkeypatch):
    """An unset `BCRYPT_ROUNDS` must not silently inherit a cheap value from a
    developer's shell — the default is the floor itself."""
    import importlib

    from backend import config as config_module

    monkeypatch.delenv("BCRYPT_ROUNDS", raising=False)
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.BCRYPT_ROUNDS == reloaded.BCRYPT_MIN_ROUNDS == 12
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


# ── Provisioning + invite ─────────────────────────────────────────────────

def test_create_user_has_no_password_until_invite_accepted(db):
    user, invite = service.create_user(db, email="A@X.com", role=UserRole.ta, city="Stockholm")
    assert user.password_hash is None
    assert user.username == "a"  # normalized local part
    assert service.authenticate(db, "a", "Password123!x") is None  # not set yet
    service.accept_invite(db, invite, "Password123!x")
    assert service.authenticate(db, "a@x.com", "Password123!x") is not None


def test_invite_cannot_be_reused(db):
    _, invite = service.create_user(db, email="b@x.com", role=UserRole.consultant)
    service.accept_invite(db, invite, "Password123!x")
    with pytest.raises(service.AuthError):
        service.accept_invite(db, invite, "Password123!x")


def test_duplicate_email_rejected(db):
    service.create_user(db, email="d@x.com", role=UserRole.consultant)
    with pytest.raises(service.UserExistsError):
        service.create_user(db, email="d@x.com", role=UserRole.consultant)


def test_short_password_rejected(db):
    _, invite = service.create_user(db, email="e@x.com", role=UserRole.consultant)
    with pytest.raises(service.AuthError):
        service.accept_invite(db, invite, "short")


def test_ta_creating_consultant_links_them(db):
    ta = _activate(db, email="ta@x.com", role=UserRole.ta, city="Malmö")
    consultant, _ = service.create_user(
        db, email="c@x.com", role=UserRole.consultant, created_by=ta
    )
    link = db.scalar(
        select(TAConsultant).where(TAConsultant.consultant_id == consultant.id)
    )
    assert link is not None and link.ta_id == ta.id


# ── Sessions ──────────────────────────────────────────────────────────────

def test_session_lifecycle(db):
    user = _activate(db, email="s@x.com", role=UserRole.consultant)
    raw = service.create_session(db, user)
    resolved = service.resolve_session(db, raw)
    assert resolved is not None and resolved.id == user.id
    service.revoke_session(db, raw)
    assert service.resolve_session(db, raw) is None


def test_wrong_password_fails(db):
    _activate(db, email="w@x.com", role=UserRole.consultant)
    assert service.authenticate(db, "w", "nope") is None


# ── Routes / RBAC ─────────────────────────────────────────────────────────

def test_me_requires_authentication(client):
    assert client.get("/api/auth/me").status_code == 401


def test_login_sets_session_and_me_works(client, Session):
    s = Session()
    _activate(s, email="admin@x.com", role=UserRole.admin)
    s.close()

    r = client.post(
        "/api/auth/login", json={"identifier": "admin", "password": "Password123!x"}
    )
    assert r.status_code == 200
    assert r.json()["landing"] == "/html/admin-dashboard.html"

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["roles"] == ["admin"]
    assert me.json()["primary_role"] == "admin"


def test_login_rejects_bad_credentials(client, Session):
    s = Session()
    _activate(s, email="u@x.com", role=UserRole.consultant)
    s.close()
    r = client.post(
        "/api/auth/login", json={"identifier": "u", "password": "wrong"}
    )
    assert r.status_code == 401


def test_rbac_ta_can_create_consultant_but_not_admin(client, Session):
    s = Session()
    _activate(s, email="ta2@x.com", role=UserRole.ta, city="Göteborg")
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "ta2", "password": "Password123!x"}
    )

    ok = client.post(
        "/api/auth/users", json={"email": "newc@x.com", "role": "consultant"}
    )
    assert ok.status_code == 201
    assert ok.json()["invite_url"].startswith("http")

    forbidden = client.post(
        "/api/auth/users", json={"email": "newa@x.com", "role": "admin"}
    )
    assert forbidden.status_code == 403


def test_logout_clears_session(client, Session):
    s = Session()
    _activate(s, email="lo@x.com", role=UserRole.consultant)
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "lo", "password": "Password123!x"}
    )
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


# ── Password policy (STAB-2) ──────────────────────────────────────────────
# The confirmed pilot policy: >= 12 characters with an uppercase letter, a
# number and a symbol, and never the account's own username or email local
# part. Enforced at both places a password is ever set, which is why every
# case below is asserted twice.

_GOOD = "Password123!x"
_WRONG = "Nope-Password-1!"


@pytest.mark.parametrize(
    "handle,password,message",
    [
        ("shortpw", "Ab1!ghijkl", "at least 12"),   # 10 characters
        ("nouppercase", "password123!", "uppercase"),
        ("nonumber", "Password!!!!", "number"),
        ("nosymbol", "Password1234", "symbol"),
    ],
)
def test_a_non_compliant_first_password_is_refused(db, handle, password, message):
    _, invite = service.create_user(
        db, email=f"{handle}@x.com", role=UserRole.consultant
    )
    with pytest.raises(service.AuthError, match=message):
        service.accept_invite(db, invite, password)


@pytest.mark.parametrize(
    "handle,password,message",
    [
        ("reset-shortpw", "Ab1!ghijkl", "at least 12"),
        ("reset-nouppercase", "password123!", "uppercase"),
        ("reset-nonumber", "Password!!!!", "number"),
        ("reset-nosymbol", "Password1234", "symbol"),
    ],
)
def test_a_non_compliant_reset_password_is_refused(db, handle, password, message):
    _activate(db, email=f"{handle}@x.com", role=UserRole.consultant)
    with pytest.raises(service.AuthError, match=message):
        service.set_password(db, handle, password)


def test_a_compliant_twelve_character_password_is_accepted(db):
    twelve = "Abcdef-123gH"
    assert len(twelve) == config.PASSWORD_MIN_LENGTH
    _, invite = service.create_user(db, email="justlong@x.com", role=UserRole.ta)
    service.accept_invite(db, invite, twelve)
    assert service.authenticate(db, "justlong", twelve) is not None


def test_a_password_equal_to_the_username_or_email_local_part_is_refused(db):
    """Both guesses below satisfy length and composition, so this isolates the
    trivial-guess rule rather than re-testing the rules above. The comparison
    is case-insensitive: the stored username is normalised to lower case, and a
    capitalised copy of it is not a meaningfully different secret."""
    _, invite = service.create_user(
        db,
        email="cv-adapter9x@example.com",
        username="handle-Number1",
        role=UserRole.consultant,
    )
    for guess in ("Handle-Number1", "CV-Adapter9X"):  # username, email local part
        with pytest.raises(service.AuthError, match="must not be"):
            service.accept_invite(db, invite, guess)

    service.accept_invite(db, invite, _GOOD)  # the invite itself still works
    for guess in ("Handle-Number1", "CV-Adapter9X"):
        with pytest.raises(service.AuthError, match="must not be"):
            service.set_password(db, "handle-number1", guess)


# ── Login throttling (STAB-2) ─────────────────────────────────────────────

def _fail(db, identifier, times=1):
    for _ in range(times):
        assert service.authenticate(db, identifier, _WRONG) is None


def test_five_consecutive_failures_lock_the_account_including_the_right_password(db):
    user = _activate(db, email="lock1@x.com", role=UserRole.consultant)
    _fail(db, "lock1", config.LOGIN_MAX_FAILED_ATTEMPTS - 1)
    assert user.locked_until is None
    assert user.failed_login_attempts == config.LOGIN_MAX_FAILED_ATTEMPTS - 1

    _fail(db, "lock1")
    assert user.locked_until is not None
    # The correct password is refused too — that is the whole point.
    assert service.authenticate(db, "lock1", _GOOD) is None


def test_a_locked_account_answers_a_login_exactly_like_a_wrong_password(
    client, Session
):
    """No status code or body may distinguish "wrong password" from "this
    account exists and is locked" — that would hand an attacker exactly the
    account-enumeration signal throttling is meant to deny them."""
    s = Session()
    _activate(s, email="lock2@x.com", role=UserRole.consultant)
    s.close()

    def login(password):
        return client.post(
            "/api/auth/login", json={"identifier": "lock2", "password": password}
        )

    wrong = login(_WRONG)
    for _ in range(config.LOGIN_MAX_FAILED_ATTEMPTS - 1):
        login(_WRONG)

    locked = login(_GOOD)
    assert wrong.status_code == locked.status_code == 401
    assert wrong.json() == locked.json()


def test_an_attempt_inside_an_active_lockout_does_not_extend_it(db):
    """Otherwise an attacker could hold an account out indefinitely by simply
    continuing to probe it, turning a 15-minute lockout into a permanent one."""
    user = _activate(db, email="lock3@x.com", role=UserRole.consultant)
    _fail(db, "lock3", config.LOGIN_MAX_FAILED_ATTEMPTS)
    locked_until = user.locked_until
    assert locked_until is not None

    _fail(db, "lock3")
    assert service.authenticate(db, "lock3", _GOOD) is None
    db.refresh(user)
    assert user.locked_until == locked_until


def test_after_the_lockout_window_the_right_password_works_and_resets_the_count(db):
    """Asserted by moving `locked_until` into the past rather than sleeping,
    the codebase's established way to test time-bounded state without a real
    delay."""
    user = _activate(db, email="lock4@x.com", role=UserRole.consultant)
    _fail(db, "lock4", config.LOGIN_MAX_FAILED_ATTEMPTS)
    assert user.locked_until is not None

    user.locked_until = service._now() - timedelta(seconds=1)
    db.commit()

    assert service.authenticate(db, "lock4", _GOOD) is not None
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_a_successful_login_clears_earlier_failures(db):
    user = _activate(db, email="lock5@x.com", role=UserRole.consultant)
    _fail(db, "lock5", 3)
    assert user.failed_login_attempts == 3

    assert service.authenticate(db, "lock5", _GOOD) is not None
    assert user.failed_login_attempts == 0


def test_failed_attempts_never_accrue_against_an_account_with_no_password(db):
    """A provisioned-but-not-yet-activated account cannot be locked before its
    owner has ever had a password, which is why `accept_invite` has nothing to
    unlock or revoke."""
    user, _ = service.create_user(db, email="pending@x.com", role=UserRole.ta)
    _fail(db, "pending", config.LOGIN_MAX_FAILED_ATTEMPTS + 2)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


# ── Password reset revokes sessions (STAB-2) ──────────────────────────────

def test_resetting_a_password_revokes_that_account_s_live_sessions(db):
    user = _activate(db, email="reset1@x.com", role=UserRole.ta)
    raw = service.create_session(db, user)
    assert service.resolve_session(db, raw) is not None

    service.set_password(db, "reset1", "New-Password-9!")

    assert service.resolve_session(db, raw) is None
    assert service.authenticate(db, "reset1", "New-Password-9!") is not None


def test_a_live_session_cookie_stops_working_after_an_admin_reset(client, Session):
    s = Session()
    _activate(s, email="reset2@x.com", role=UserRole.consultant)
    s.close()
    client.post(
        "/api/auth/login", json={"identifier": "reset2", "password": _GOOD}
    )
    assert client.get("/api/auth/me").status_code == 200

    s = Session()
    service.set_password(s, "reset2", "New-Password-9!")
    s.close()

    assert client.get("/api/auth/me").status_code == 401


def test_a_password_reset_also_clears_an_active_lockout(db):
    """The documented admin unlock path (ADMIN.md): a reset is what makes the
    hard-lockout trade-off recoverable without waiting out the window."""
    user = _activate(db, email="reset3@x.com", role=UserRole.consultant)
    _fail(db, "reset3", config.LOGIN_MAX_FAILED_ATTEMPTS)
    assert user.locked_until is not None

    service.set_password(db, "reset3", "New-Password-9!")

    assert user.locked_until is None
    assert user.failed_login_attempts == 0
    assert service.authenticate(db, "reset3", "New-Password-9!") is not None


def test_admin_can_force_revoke_a_users_sessions(client, Session):
    """docs/legal/breach-runbook.md Scenario A (lost laptop) instructs an
    admin to force-revoke the affected user's live session — this is the
    endpoint that makes that instruction actually possible. (Relocated here
    from the deleted tests/test_api_tokens.py, STAB-6 — this route is
    unrelated to API tokens and remains live.)"""
    s = Session()
    target = _activate(s, email="revsess1@x.com", role=UserRole.ta)
    _activate(s, email="revsessadmin1@x.com", role=UserRole.admin)
    target_id = target.id
    service.create_session(s, target)  # simulates a still-live browser session
    s.close()

    client.post(
        "/api/auth/login",
        json={"identifier": "revsessadmin1", "password": _GOOD},
    )
    r = client.post(f"/api/auth/admin/users/{target_id}/revoke-sessions")
    assert r.status_code == 200
    assert r.json()["sessions_revoked"] == 1


def test_revoke_sessions_route_requires_admin(client, Session):
    s = Session()
    target = _activate(s, email="revsess2@x.com", role=UserRole.ta)
    _activate(s, email="revsess3@x.com", role=UserRole.ta)
    target_id = target.id
    s.close()

    client.post(
        "/api/auth/login",
        json={"identifier": "revsess3", "password": _GOOD},
    )
    r = client.post(f"/api/auth/admin/users/{target_id}/revoke-sessions")
    assert r.status_code == 403


def test_accepting_an_invite_still_signs_the_new_account_in(client, Session):
    """Regression beside the change above: `accept_invite` deliberately does
    *not* revoke sessions, so the auto-login session it creates must survive
    its own call."""
    s = Session()
    _, invite = service.create_user(
        s, email="invitee@x.com", role=UserRole.consultant
    )
    s.commit()
    s.close()

    r = client.post(
        "/api/auth/invite/accept", json={"token": invite, "password": _GOOD}
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/me").status_code == 200
