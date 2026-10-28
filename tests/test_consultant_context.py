"""`GET /api/me/consultants` — the Consultant contexts a caller may act in.

Added in the `S2-P1-R1` correction round. Every consultant-scoped action takes a
`consultant_username` and is authorized against
`policy.operable_consultant_ids`; the client has to offer that same set. It used
to guess from the caller's roles, reading the TA-assignment endpoint for Admins
too — which requires an explicit `ta_id` and answers 400 without one, so the
pilot Admin+Consultant's picker silently collapsed to themselves while the API
would have allowed every Consultant.

These tests pin the property that removes the guess: **the endpoint returns
exactly what the policy allows**, for every role. A drift between the two is the
defect this replaced.
"""

from __future__ import annotations

import pytest

from backend.authz import policy
from backend.db.models import User


def _context(client) -> dict[str, str]:
    body = client.get("/api/me/consultants").json()
    return {c["username"]: c["id"] for c in body["consultants"]}


def test_a_consultant_gets_themselves(client, actors):
    actors.login(client, actors.consultant)
    assert set(_context(client)) == {actors.consultant.username}


def test_a_ta_gets_the_consultants_assigned_to_them(client, actors, second_pair):
    actors.login(client, actors.ta)
    assert set(_context(client)) == {actors.consultant.username}
    # …and not the other relationship's.
    assert second_pair["consultant"].username not in _context(client)


def test_an_admin_gets_every_consultant(client, actors, second_pair):
    """The regression. The Admin+Consultant (P-004) must be able to act for the
    other pilot Consultants, not only for themselves."""
    actors.login(client, actors.admin)
    contexts = set(_context(client))
    assert contexts == {
        actors.admin.username,          # they hold the consultant role too
        actors.consultant.username,
        second_pair["consultant"].username,
    }


def test_a_ta_with_no_assignments_gets_an_empty_list_not_an_error(client, actors):
    """Authenticated with nothing to work on is an honest answer, not a 4xx."""
    actors.login(client, actors.other_ta)
    r = client.get("/api/me/consultants")
    assert r.status_code == 200
    assert r.json() == {"consultants": [], "total": 0}


def test_it_requires_authentication(client, actors):
    actors.login(client, actors.ta)
    actors.logout(client)
    assert client.get("/api/me/consultants").status_code == 401


def test_every_returned_context_is_one_the_policy_actually_allows(
    client, actors, second_pair, db
):
    """The invariant behind the endpoint, asserted for the whole cast: what the
    picker offers and what the server will authorize are the same set."""
    for actor in (
        actors.consultant,
        actors.ta,
        actors.admin,
        second_pair["ta"],
        second_pair["consultant"],
        actors.other_ta,
    ):
        actors.logout(client)
        actors.login(client, actor)
        offered = _context(client)
        subject = db.get(User, actor.user_id)
        for username in offered:
            advisee = policy.get_advisee_by_username(db, username)
            assert advisee is not None, username
            assert policy.can_access_consultant(db, subject, advisee), (
                f"{actor.username} was offered {username} but may not act for them"
            )
        # And nothing the policy allows is missing.
        reachable = policy.operable_consultant_ids(db, subject)
        if reachable is not None:
            assert {v for v in offered.values()} == {str(i) for i in reachable}


def test_the_returned_id_is_the_one_the_job_projections_are_keyed_by(
    client, actors
):
    """The client reads the Job list's `reviews`/`applications` maps with this
    id. A username-only response would have left it unable to tell whose
    decision a row carries."""
    actors.login(client, actors.ta)
    assert _context(client)[actors.consultant.username] == str(
        actors.consultant.user_id
    )


@pytest.mark.parametrize("path", ["/api/ta/consultants"])
def test_the_admin_still_gets_400_from_the_ta_assignment_endpoint(
    client, actors, path
):
    """Pinning *why* the new endpoint exists: the old source genuinely cannot
    answer this question for an Admin, so a future change must not quietly point
    the client back at it."""
    actors.login(client, actors.admin)
    assert client.get(path).status_code == 400
