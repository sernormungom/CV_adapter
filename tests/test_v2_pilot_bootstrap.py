"""V2 identity baseline: controlled seed and production-auth invariants."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import select

from backend.bootstrap.pilot import PilotSeedError, seed_pilot
from backend.db.models import Site, TAConsultant, User, UserRole

# Clears the seed's own 15-character bootstrap bar *and* the general account
# password policy (STAB-2), which applies here because the seed sets its three
# passwords through `accept_invite`.
SEED_PASSWORD = "Safe-Test-Password-9"


def test_pilot_seed_creates_exactly_the_approved_cast(db):
    seed_pilot(db, password=SEED_PASSWORD)

    users = {user.username: user for user in db.scalars(select(User))}
    assert set(users) == {"patrik.artman", "norberto.munoz", "mo.chen"}
    assert users["patrik.artman"].roles == {UserRole.ta.value}
    assert users["norberto.munoz"].roles == {
        UserRole.admin.value,
        UserRole.consultant.value,
    }
    assert users["mo.chen"].roles == {UserRole.consultant.value}
    # V3-P-007/P-008: the single-Site guard is gone; all four Sites are
    # provisioned, but the three pilot accounts still seed at GBG alone.
    assert {site.code for site in db.scalars(select(Site))} == {"GBG", "MAL", "STH", "UPS"}
    assert {user.site_id for user in users.values()} == {
        next(s.id for s in db.scalars(select(Site)) if s.code == "GBG")
    }
    assert {
        (link.ta_id, link.consultant_id)
        for link in db.scalars(select(TAConsultant))
    } == {
        (users["patrik.artman"].id, users["norberto.munoz"].id),
        (users["patrik.artman"].id, users["mo.chen"].id),
    }


def test_pilot_seed_refuses_to_mix_with_other_accounts(db):
    from backend.auth import service

    service.create_user(db, email="outside@pilot.invalid", role=UserRole.ta)
    with pytest.raises(PilotSeedError, match="non-pilot"):
        seed_pilot(db, password=SEED_PASSWORD)


def test_a_long_but_non_compliant_seed_password_is_refused_before_anything_exists(db):
    """The 15-character bootstrap bar is not a laxer alternative policy: the
    seed sets its passwords through `accept_invite`, so STAB-2's composition
    rules apply. Refused up front, so a rejected password cannot half-seed the
    database and leave every rerun refusing to validate it."""
    with pytest.raises(PilotSeedError, match="uppercase"):
        seed_pilot(db, password="a-long-enough-but-plain-password")

    assert db.scalars(select(User)).first() is None


def test_production_process_refuses_disabled_auth():
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "production",
            "AUTH_ENFORCED": "false",
            "CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS": "true",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import backend.config"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "forbidden in production" in result.stderr
