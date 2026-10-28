"""V2 Slice 1 — relational Jobs, on-demand advisory scoring, and Reject.

The acceptance cases from `docs/handoffs/slice-1-jobs.md`, minus the ones that
need real Postgres semantics (the concurrent duplicate insert and the migration
shape), which live in `tests/integration/test_jobs_pg.py`.

Uses the standard `actors` cast (`docs/testing.md`): `ta` serves `consultant`,
`other_ta` serves nobody in this story, and `admin` holds Admin **and**
Consultant so the multi-role case (P-004) is exercised without being
constructed by hand.
"""

from __future__ import annotations

import uuid

import pytest
import yaml

from backend import config
from backend.auth import service as auth_service
from backend.authz import policy
from backend.db.models import Job, JobDecisionPair, JobReview, User, UserRole
from backend.jobs import service
from backend.matching import service as matching_service
from tests.conftest import ACTOR_PASSWORD

DESCRIPTION = (
    "We are looking for a senior embedded engineer in Göteborg to own an "
    "automotive control unit end to end: C, AUTOSAR, CAN, and ISO 26262 "
    "functional-safety work alongside a verification team."
)

#: A genuinely different posting (~0.15 word-set Jaccard vs `DESCRIPTION`),
#: used wherever a fixture needs to be "not a duplicate at all". Appending a
#: short sentence to `DESCRIPTION` itself, as these fixtures used to do,
#: scores ~0.88 Tier 1 Jaccard under the near-duplicate rule and gets wrongly
#: refused as `near_duplicate` instead of `created` (S11 finding).
DIFFERENT_DESCRIPTION = (
    "We are hiring a payments backend engineer to own our ledger service: "
    "idempotent webhook processing, double-entry accounting, and "
    "reconciliation against three banking partners."
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _payload(consultant_username: str, **over) -> dict:
    body = {
        "consultant_username": consultant_username,
        "title": "Senior Embedded Engineer",
        "company": "Client AB",
        "location": "Göteborg",
        "description": DESCRIPTION,
    }
    body.update(over)
    return body


def _create(client, actors, actor=None, **over) -> dict:
    actors.login(client, actor or actors.ta)
    r = client.post("/api/jobs", json=_payload(actors.consultant.username, **over))
    assert r.status_code in (200, 201), r.text
    return r.json()


# `second_pair` — a second populated TA↔Consultant relationship — moved to
# `tests/conftest.py` when Slice 2 needed the same cast for Applications.


# ── Acceptance 1: shared access, negative cases, no Site on the path ────────


def test_related_ta_and_consultant_both_see_a_job_either_created(
    client, actors
):
    created = _create(client, actors)
    job_id = created["job"]["id"]
    assert created["status"] == "created"

    # The TA that created it.
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    listed = client.get("/api/jobs").json()
    assert [j["id"] for j in listed["jobs"]] == [job_id]

    # The assigned consultant reaches the same shared Job.
    actors.logout(client)
    actors.login(client, actors.consultant)
    assert client.get(f"/api/jobs/{job_id}").json()["title"] == (
        "Senior Embedded Engineer"
    )
    assert client.get("/api/jobs").json()["total"] == 1


def test_consultant_created_job_is_visible_to_their_ta(client, actors):
    created = _create(client, actors, actor=actors.consultant)
    job_id = created["job"]["id"]

    actors.logout(client)
    actors.login(client, actors.ta)
    assert client.get(f"/api/jobs/{job_id}").status_code == 200


def test_unrelated_ta_gets_404_and_learns_no_job_content(client, actors):
    created = _create(client, actors)
    job_id = created["job"]["id"]

    actors.logout(client)
    actors.login(client, actors.other_ta)

    r = client.get(f"/api/jobs/{job_id}")
    # 404, not 403: a Job's existence is itself content.
    assert r.status_code == 404
    body = r.text
    assert "Senior Embedded Engineer" not in body
    assert "Client AB" not in body
    assert DESCRIPTION[:30] not in body

    # And it is absent from their list entirely.
    assert client.get("/api/jobs").json() == {"jobs": [], "total": 0, "unscored_count": 0}


def test_unrelated_ta_cannot_edit_close_score_or_reject(client, actors):
    created = _create(client, actors)
    job_id = created["job"]["id"]
    consultant = actors.consultant.username

    actors.logout(client)
    actors.login(client, actors.other_ta)

    assert client.patch(
        f"/api/jobs/{job_id}", json=_payload(consultant, title="Hijacked")
    ).status_code == 404
    assert client.post(f"/api/jobs/{job_id}/close").status_code == 404
    assert client.get(
        f"/api/jobs/{job_id}/score", params={"consultant_username": consultant}
    ).status_code == 404
    assert client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": consultant},
    ).status_code == 404


def test_admin_reaches_every_job(client, actors):
    created = _create(client, actors)
    job_id = created["job"]["id"]

    actors.logout(client)
    actors.login(client, actors.admin)
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get("/api/jobs").json()["total"] == 1


def test_job_routes_require_authentication(client, actors):
    created = _create(client, actors)
    job_id = created["job"]["id"]
    actors.logout(client)

    assert client.get("/api/jobs").status_code == 401
    assert client.get(f"/api/jobs/{job_id}").status_code == 401
    assert client.post("/api/jobs", json=_payload("whoever")).status_code == 401
    assert client.post(f"/api/jobs/{job_id}/close").status_code == 401


def test_job_access_refuses_a_different_site_even_with_a_decision_pair(client, actors, db):
    job_id = _create(client, actors)["job"]["id"]
    job = db.get(Job, uuid.UUID(job_id))
    job.site_id = actors.other_ta.user.site_id
    db.commit()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404



# ── Acceptance 2: exact duplicate blocking ─────────────────────────────────


def test_identical_submission_creates_one_job(client, actors, db):
    first = _create(client, actors)
    second = client.post(
        "/api/jobs", json=_payload(actors.consultant.username)
    )
    assert second.status_code == 200          # not 201 — nothing was created
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 1


def test_duplicate_detection_ignores_case_and_whitespace(client, actors, db):
    _create(client, actors)
    noisy = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="  SENIOR   Embedded Engineer ",
            description=DESCRIPTION.upper().replace(" ", "   "),
        ),
    )
    assert noisy.json()["status"] == "duplicate"
    assert db.query(Job).count() == 1


def test_a_genuinely_different_posting_is_not_a_duplicate(client, actors, db):
    _create(client, actors)
    other = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Verification Engineer",
            description=DIFFERENT_DESCRIPTION,
        ),
    )
    assert other.status_code == 201
    assert other.json()["status"] == "created"
    assert db.query(Job).count() == 2


def test_duplicate_does_not_materialize_a_decision_pair(
    client, actors, db
):
    """A Job is shared. A second consultant whose TA submits the identical
    posting is linked to the one Job rather than getting a second copy."""
    first = _create(client, actors)
    job_id = uuid.UUID(first["job"]["id"])

    actors.logout(client)
    actors.login(client, actors.admin)
    dup = client.post("/api/jobs", json=_payload(actors.admin.username))
    assert dup.json()["status"] == "duplicate"

    assert db.query(Job).count() == 1
    linked = policy.consultant_ids_for_job(db, job_id)
    assert linked == set()  # Creation never starts a decision


# ── Acceptance 3: Tier 1/2 lightweight near-duplicate warning (S9/S11/S13) ─


def test_near_duplicate_formatting_change_is_refused_with_no_new_row(
    client, actors, db
):
    first = _create(client, actors)
    noisy = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Senior Embedded Engineer II",  # different title, same content
            description=DESCRIPTION.upper().replace(" ", "  "),
        ),
    )
    assert noisy.status_code == 200          # not 201 — nothing was created
    body = noisy.json()
    assert body["status"] == "near_duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 1


def test_near_duplicate_reordered_content_is_refused(client, actors, db):
    """Same words, reordered — the exact-hash key is order-sensitive (so this
    is not an exact duplicate), but a description's word *set* is not."""
    opening = "Senior embedded engineer needed in Göteborg."
    body_text = (
        "Own an automotive control unit end to end using C, AUTOSAR, CAN, "
        "and ISO 26262 functional safety practices alongside a "
        "verification team."
    )
    first = _create(client, actors, description=f"{opening} {body_text}")
    reordered = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Embedded Engineer (Reposted)",
            description=f"{body_text} {opening}",
        ),
    )
    assert reordered.status_code == 200
    body = reordered.json()
    assert body["status"] == "near_duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 1


def test_shared_boilerplate_with_different_core_work_is_not_a_near_duplicate(
    client, actors, db
):
    _create(client, actors)
    other = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Payments Backend Engineer",
            description=(
                "We are looking for a senior engineer to join our growing "
                "team. Own the payments ledger service end to end: "
                "idempotent webhook processing, double-entry accounting, and "
                "reconciliation against three banking partners."
            ),
        ),
    )
    assert other.status_code == 201
    assert other.json()["status"] == "created"
    assert db.query(Job).count() == 2


def test_near_duplicate_of_a_closed_job_may_be_created_anew(client, actors, db):
    first = _create(client, actors)
    job = db.get(Job, uuid.UUID(first["job"]["id"]))
    job.status = "closed"
    db.commit()

    again = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username, title="Senior Embedded Engineer (Repost)"
        ),
    )
    assert again.status_code == 201
    assert again.json()["status"] == "created"
    assert db.query(Job).count() == 2


def test_near_duplicate_is_scoped_to_the_same_site(client, actors, db):
    _create(client, actors)
    actors.logout(client)
    actors.login(client, actors.other_ta)  # a different Site (G7's cast)

    elsewhere = client.post(
        "/api/jobs",
        json=_payload(actors.other_ta.username, title="Senior Embedded Engineer"),
    )
    assert elsewhere.status_code == 201
    assert elsewhere.json()["status"] == "created"


def test_near_duplicate_does_not_materialize_a_decision_pair(client, actors, db):
    first = _create(client, actors)
    job_id = uuid.UUID(first["job"]["id"])

    near = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username, title="Senior Embedded Engineer (Reposted)"
        ),
    )
    assert near.json()["status"] == "near_duplicate"
    assert policy.consultant_ids_for_job(db, job_id) == set()


def test_near_duplicate_match_is_not_disclosed_to_an_opted_out_consultant(
    client, actors, db
):
    """An opted-out Consultant cannot `GET` an open Job (S1) — so a
    near-duplicate match against that same Job must not become a back door
    that hands them its id, title, company, or description anyway."""
    first = _create(client, actors)
    job_id = first["job"]["id"]

    actors.login(client, actors.ta)
    assert client.post(
        f"/api/ta/consultants/{actors.consultant.username}/looking",
        json={"actively_looking": False},
    ).status_code == 200

    actors.logout(client)
    actors.login(client, actors.consultant)
    assert client.get(f"/api/jobs/{job_id}").status_code == 404

    near = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Senior Embedded Engineer (Reposted)",
        ),
    )
    assert near.status_code == 200
    body = near.json()
    assert body["status"] == "near_duplicate"
    assert body["job"] is None
    text = near.text
    assert job_id not in text
    assert "Senior Embedded Engineer" not in text
    assert "Client AB" not in text
    assert DESCRIPTION[:30] not in text
    assert db.query(Job).count() == 1
    assert policy.consultant_ids_for_job(db, uuid.UUID(job_id)) == set()


def test_tier1_match_ignores_title_and_company_even_at_the_threshold_boundary(
    client, actors, db
):
    """Tier 1 has no title/company gate: two bodies at 37/43 == 0.860465
    Jaccard — just above `NEAR_DUPLICATE_THRESHOLD` (0.85) — match even when
    both the title and the company are completely different.

    37 words are shared; each side then adds 3 more unique words of its own,
    so |A| = |B| = 40, the intersection is 37, and the union is 37 + 3 + 3 =
    43: 37 / 43 = 0.860465...
    """
    shared = " ".join(f"role{i}" for i in range(37))
    first = _create(client, actors, description=f"{shared} alphaone alphatwo alphathree")
    changed = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Completely Unrelated Posting",
            company="Northwind Logistics",
            description=f"{shared} betaone betatwo betathree",
        ),
    )
    assert changed.status_code == 200
    body = changed.json()
    assert body["status"] == "near_duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 1


def test_tier2_match_requires_title_and_company_and_holds_at_the_threshold_boundary(
    client, actors, db
):
    """Tier 2: a moderately reworded body — 34/46 == 0.739130 Jaccard, above
    `NEAR_DUPLICATE_TIER2_THRESHOLD` (0.55) but below Tier 1 (0.85) — only
    matches when the title and company agree.

    34 words are shared; each side then adds 6 more unique words of its own,
    so |A| = |B| = 40, the intersection is 34, and the union is 34 + 6 + 6 =
    46: 34 / 46 = 0.739130...
    """
    shared = " ".join(f"term{i}" for i in range(34))
    extra_a = " ".join(f"gammaa{i}" for i in range(6))
    extra_b = " ".join(f"gammab{i}" for i in range(6))
    first = _create(client, actors, description=f"{shared} {extra_a}")
    reworded = client.post(
        "/api/jobs",
        json=_payload(actors.consultant.username, description=f"{shared} {extra_b}"),
    )
    assert reworded.status_code == 200
    body = reworded.json()
    assert body["status"] == "near_duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 1


def test_tier2_range_similarity_without_a_matching_title_is_not_a_near_duplicate(
    client, actors, db
):
    """The same 0.739130 Tier 2-range Jaccard as above, but this time the
    title and company are both unrelated to the first posting — the gate
    correctly refuses the match and a second Job is created."""
    shared = " ".join(f"term{i}" for i in range(34))
    extra_a = " ".join(f"gammaa{i}" for i in range(6))
    extra_c = " ".join(f"gammac{i}" for i in range(6))
    _create(client, actors, description=f"{shared} {extra_a}")
    other = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Warehouse Operations Lead",
            company="Northwind Logistics",
            description=f"{shared} {extra_c}",
        ),
    )
    assert other.status_code == 201
    assert other.json()["status"] == "created"
    assert db.query(Job).count() == 2


def test_an_exact_resubmission_reports_duplicate_not_near_duplicate(
    client, actors, db
):
    """Resubmitting a Job's exact content scores Jaccard 1.0 against itself —
    well above Tier 1's threshold. `find_near_duplicate`'s `exclude_hash` must
    leave this Job out of its own candidate set so the DB-enforced exact-match
    path (G1) decides, and the response says "duplicate", not "near_duplicate"."""
    first = _create(client, actors)
    again = client.post("/api/jobs", json=_payload(actors.consultant.username))
    assert again.status_code == 200
    body = again.json()
    assert body["status"] == "duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 1


def test_exact_resubmission_takes_precedence_over_a_coexisting_near_duplicate(
    client, actors, db
):
    """An exact match must win even when a *different* open Job at the same
    Site independently qualifies as a Tier 1 near-duplicate of the same
    submission (reachable via an edit, as built here, or pre-existing data).

    Without exact-match precedence in `create_job`, the fuzzy scan could pick
    the other qualifying Job as its "best" match and relabel the exact
    resubmission `near_duplicate`, pointing it at the wrong Job."""
    first = _create(client, actors)

    other = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Verification Engineer",
            description=DIFFERENT_DESCRIPTION,
        ),
    ).json()
    assert other["status"] == "created"

    # Edit the second Job so its description becomes a Tier 1 match of the
    # first Job's content (same folded word set) while its title stays
    # distinct enough that this is not itself an exact-hash collision.
    retitled = client.patch(
        f"/api/jobs/{other['job']['id']}",
        json=_payload(
            actors.consultant.username,
            title="Senior Embedded Engineer II",
            description=DESCRIPTION.upper().replace(" ", "  "),
        ),
    )
    assert retitled.status_code == 200
    assert db.query(Job).count() == 2

    again = client.post("/api/jobs", json=_payload(actors.consultant.username))
    assert again.status_code == 200
    body = again.json()
    assert body["status"] == "duplicate"
    assert body["job"]["id"] == first["job"]["id"]
    assert db.query(Job).count() == 2


# ── Shared Job, unshared relationships (S1-P1-R1 finding 1) ────────────────
#
# Linking yourself to an existing shared Job is unprivileged: submit the same
# public posting and the duplicate path links you automatically. So the Job's
# *facts* being shared must not make the other Consultants on it, or what they
# wrote when rejecting it, readable too.


def _link_second_consultant(client, actors, second_pair) -> str:
    """Pair 1 creates a Job and rejects it; pair 2 reaches the same Job via the
    ordinary duplicate path. Returns the shared job id."""
    created = _create(client, actors)
    job_id = created["job"]["id"]
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    actors.logout(client)

    actors.login(client, second_pair["ta"])
    dup = client.post(
        "/api/jobs", json=_payload(second_pair["consultant"].username)
    )
    assert dup.json()["status"] == "duplicate"
    assert dup.json()["job"]["id"] == job_id
    return job_id


def test_duplicate_response_hides_the_other_consultant_and_their_review(
    client, actors, second_pair
):
    job_id = _link_second_consultant(client, actors, second_pair)
    body = client.get(f"/api/jobs/{job_id}").json()

    # The shared Job's own facts are visible — that is the point of sharing.
    assert body["title"] == "Senior Embedded Engineer"

    # The other relationship is not.
    usernames = {c["username"] for c in body["consultants"]}
    assert usernames == set()
    assert actors.consultant.username not in usernames
    assert body["reviews"] == []


def test_a_consultant_sees_only_their_own_link_and_review(
    client, actors, second_pair
):
    job_id = _link_second_consultant(client, actors, second_pair)
    actors.logout(client)
    actors.login(client, second_pair["consultant"])

    raw = client.get(f"/api/jobs/{job_id}").text
    assert actors.consultant.username not in raw

    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["consultants"] == []


def test_each_side_still_sees_its_own_review(client, actors, second_pair):
    """Scoping must not hide the caller's own data from them."""
    job_id = _link_second_consultant(client, actors, second_pair)
    actors.logout(client)
    actors.login(client, actors.ta)

    body = client.get(f"/api/jobs/{job_id}").json()
    assert [c["username"] for c in body["consultants"]] == [
        actors.consultant.username
    ]
    assert [r["consultant_id"] for r in body["reviews"]] == [
        str(actors.consultant.user_id)
    ]
    assert all("reason" not in r for r in body["reviews"])


def test_admin_sees_every_link_and_review(client, actors, second_pair):
    job_id = _link_second_consultant(client, actors, second_pair)
    actors.logout(client)
    actors.login(client, actors.admin)

    body = client.get(f"/api/jobs/{job_id}").json()
    assert {c["username"] for c in body["consultants"]} == {
        actors.consultant.username,
    }
    assert len(body["reviews"]) == 1


def _list_reviews(client, job_id) -> dict:
    """The `reviews` map the list route renders the Rejected badge from."""
    rows = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
    return rows[job_id]["reviews"]


def test_admin_sees_review_markers_in_the_list_not_only_the_detail(
    client, actors, second_pair
):
    """Regression: `operable_consultant_ids` returns `None` for Admin meaning
    "no filter", and the list route used to coerce it with `or []` — which the
    review query read as "no Consultants at all". The Admin saw every Job and
    none of them as reviewed, while the detail view for the same Job returned
    the review. Two projections of one fact must not disagree."""
    job_id = _link_second_consultant(client, actors, second_pair)
    # Both consultants have now rejected it, so there are two markers.
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": second_pair["consultant"].username},
    )
    actors.logout(client)
    actors.login(client, actors.admin)

    listed = _list_reviews(client, job_id)
    assert listed == {
        str(actors.consultant.user_id): "reject",
        str(second_pair["consultant"].user_id): "reject",
    }
    # …and the two views agree, which is the property that actually broke.
    detail = client.get(f"/api/jobs/{job_id}").json()["reviews"]
    assert {r["consultant_id"] for r in detail} == set(listed)


def test_the_admin_consultant_sees_their_own_review_in_the_list(client, actors):
    """The pilot Admin is also a Consultant (P-004). The sentinel bug meant they
    could not even see their own Reject state in the primary UI."""
    actors.login(client, actors.admin)
    created = client.post("/api/jobs", json=_payload(actors.admin.username))
    job_id = created.json()["job"]["id"]
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.admin.username},
    )

    assert _list_reviews(client, job_id) == {
        str(actors.admin.user_id): "reject"
    }


def test_the_list_review_map_stays_relationship_scoped_for_ta_and_consultant(
    client, actors, second_pair
):
    """Fixing the Admin case must not widen the ordinary ones: the list map is
    still exactly what that actor may see, matching the detail projection."""
    job_id = _link_second_consultant(client, actors, second_pair)
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": second_pair["consultant"].username},
    )

    # Pair two's TA is still logged in from the helper.
    assert _list_reviews(client, job_id) == {
        str(second_pair["consultant"].user_id): "reject"
    }

    for actor, own_id in (
        (second_pair["consultant"], second_pair["consultant"].user_id),
        (actors.ta, actors.consultant.user_id),
        (actors.consultant, actors.consultant.user_id),
    ):
        actors.logout(client)
        actors.login(client, actor)
        assert _list_reviews(client, job_id) == {str(own_id): "reject"}, actor.username


def test_list_and_detail_report_the_same_reviews_for_every_actor(
    client, actors, second_pair
):
    """The general property behind the Admin regression, asserted for the whole
    cast: the list's `reviews` map and the detail's `reviews` array are two
    projections of one fact and must never disagree, whatever the scoping."""
    job_id = _link_second_consultant(client, actors, second_pair)
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": second_pair["consultant"].username},
    )

    for actor in (
        actors.ta,
        actors.consultant,
        second_pair["ta"],
        second_pair["consultant"],
        actors.admin,
    ):
        actors.logout(client)
        actors.login(client, actor)
        listed = set(_list_reviews(client, job_id))
        detail = {
            r["consultant_id"]
            for r in client.get(f"/api/jobs/{job_id}").json()["reviews"]
        }
        assert listed == detail, f"{actor.username}: {listed} != {detail}"


def test_an_actor_reaching_no_consultant_sees_no_reviews(client, actors):
    """The empty set and the `None` sentinel must stay distinguishable: an
    unrelated TA reaches nothing, which is not the same as being unfiltered."""
    _create(client, actors)
    actors.logout(client)
    actors.login(client, actors.other_ta)
    assert client.get("/api/jobs").json() == {"jobs": [], "total": 0, "unscored_count": 0}
    assert service.reviewed_job_ids(None, []) == {}


def test_the_creating_response_is_scoped_too(client, actors, second_pair):
    """Not only GET: the create/duplicate envelope carries the same projection,
    and it is the one an attacker reaches first."""
    _create(client, actors)
    client.post(
        f"/api/jobs/{_create(client, actors)['job']['id']}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    actors.logout(client)

    actors.login(client, second_pair["ta"])
    dup = client.post(
        "/api/jobs", json=_payload(second_pair["consultant"].username)
    ).json()
    assert dup["status"] == "duplicate"
    assert dup["job"]["consultants"] == []
    assert dup["job"]["reviews"] == []


def test_a_second_ta_cannot_reject_for_the_first_pairs_consultant(
    client, actors, second_pair
):
    """Reaching the shared Job does not confer the right to act for someone
    else's Consultant."""
    job_id = _link_second_consultant(client, actors, second_pair)
    r = client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 403


# ── RCB-D2: link an additional consultant to an existing shared Job ────────


def _second_consultant_for_ta(Session, ta_actor) -> str:
    """A second Consultant assigned to `ta_actor`'s TA account.

    `actors`/`second_pair` each give one TA exactly one Consultant, so a test
    that needs "one TA operating two Consultants" has to provision the second
    one itself — the same `created_by` mechanism `conftest.py`'s own fixtures
    use to establish the `TAConsultant` link.
    """
    session = Session()
    try:
        user, invite = auth_service.create_user(
            session,
            email="second-advisee@example.invalid",
            role=UserRole.consultant,
            created_by=ta_actor.user,
        )
        auth_service.accept_invite(session, invite, ACTOR_PASSWORD)
        session.commit()
        return user.username
    finally:
        session.close()


def test_later_consultant_immediately_reaches_open_site_jobs(client, actors, Session):
    job_id = _create(client, actors)["job"]["id"]
    username = _second_consultant_for_ta(Session, actors.ta)
    assert client.get(f"/api/jobs/{job_id}/score", params={"consultant_username": username}).status_code == 200
    r = client.post(f"/api/jobs/{job_id}/reject", json={"consultant_username": username})
    assert r.status_code == 200
    assert r.json()["created"] is True



def test_unrelated_ta_gets_404_linking_anyone(client, actors, second_pair):
    """Acceptance 2: an actor who cannot reach the Job at all still gets 404,
    unchanged from `ensure_job_access`'s existing contract."""
    job_id = _create(client, actors)["job"]["id"]
    actors.logout(client)
    actors.login(client, actors.other_ta)

    r = client.post(
        f"/api/jobs/{job_id}/consultants",
        json={"consultant_username": second_pair["consultant"].username},
    )
    assert r.status_code == 404


def test_site_membership_does_not_grant_sibling_consultant_actions(client, actors, second_pair):
    job_id = _link_second_consultant(client, actors, second_pair)
    for action in ("score", "reject"):
        endpoint = f"/api/jobs/{job_id}/{action}"
        r = client.get(endpoint, params={"consultant_username": actors.consultant.username}) if action == "score" else client.post(endpoint, json={"consultant_username": actors.consultant.username})
        assert r.status_code == 403



def test_manual_link_endpoint_is_retired_for_every_actor(client, actors):
    job_id = _create(client, actors)["job"]["id"]
    for actor in (actors.ta, actors.consultant, actors.admin, actors.other_ta):
        actors.login(client, actor)
        assert client.post(f"/api/jobs/{job_id}/consultants", json={"consultant_username": actors.consultant.username}).status_code == 404



def test_decision_pair_is_created_only_by_first_decision(client, actors, db):
    job_id = _create(client, actors)["job"]["id"]
    assert db.query(JobDecisionPair).count() == 0
    for _ in range(3):
        r = client.post(f"/api/jobs/{job_id}/reject", json={"consultant_username": actors.consultant.username})
        assert r.status_code == 200
    assert db.query(JobDecisionPair).count() == 1



def test_content_hash_separates_fields(db):
    """Moving text across a field boundary must not collide: 'Acme' as the
    company is a different job from 'Acme' appended to the title."""
    a = service.content_hash("Engineer", "Acme", DESCRIPTION)
    b = service.content_hash("Engineer Acme", "", DESCRIPTION)
    assert a != b


def test_edit_into_an_existing_job_is_refused(client, actors):
    _create(client, actors)
    second = client.post(
        "/api/jobs",
        json=_payload(
            actors.consultant.username,
            title="Verification Engineer",
            description=DIFFERENT_DESCRIPTION,
        ),
    ).json()

    clash = client.patch(
        f"/api/jobs/{second['job']['id']}", json=_payload(actors.consultant.username)
    )
    assert clash.status_code == 409


# ── Validation ─────────────────────────────────────────────────────────────


def test_missing_title_or_too_short_description_is_refused(client, actors):
    actors.login(client, actors.ta)
    u = actors.consultant.username
    assert client.post(
        "/api/jobs", json=_payload(u, title="   ")
    ).status_code == 400
    assert client.post(
        "/api/jobs", json=_payload(u, description="too short")
    ).status_code == 400


def test_bad_close_date_is_refused(client, actors):
    actors.login(client, actors.ta)
    r = client.post(
        "/api/jobs", json=_payload(actors.consultant.username, close_date="30/09/2026")
    )
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]


def test_unknown_job_id_shapes_are_404_not_500(client, actors):
    actors.login(client, actors.ta)
    assert client.get("/api/jobs/not-a-uuid").status_code == 404
    assert client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404


# ── Edit and close ─────────────────────────────────────────────────────────


def test_edit_updates_the_shared_job_for_everyone(client, actors):
    job_id = _create(client, actors)["job"]["id"]
    r = client.patch(
        f"/api/jobs/{job_id}",
        json=_payload(
            actors.consultant.username,
            title="Lead Embedded Engineer",
            close_date="2026-09-30",
        ),
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Lead Embedded Engineer"
    assert r.json()["close_date"] == "2026-09-30"

    actors.logout(client)
    actors.login(client, actors.consultant)
    assert client.get(f"/api/jobs/{job_id}").json()["title"] == (
        "Lead Embedded Engineer"
    )


def test_close_is_explicit_idempotent_and_filterable(client, actors):
    job_id = _create(client, actors)["job"]["id"]

    first = client.post(f"/api/jobs/{job_id}/close").json()
    assert first["status"] == "closed"
    assert first["closed_at"]

    # Idempotent: a repeat neither errors nor moves the timestamp.
    second = client.post(f"/api/jobs/{job_id}/close").json()
    assert second["closed_at"] == first["closed_at"]

    assert client.get("/api/jobs", params={"status": "open"}).json()["total"] == 0
    assert client.get("/api/jobs", params={"status": "closed"}).json()["total"] == 1
    # A closed Job is still fetchable, with its state explicit.
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "closed"


def test_unknown_status_filter_is_refused(client, actors):
    actors.login(client, actors.ta)
    assert client.get("/api/jobs", params={"status": "maybe"}).status_code == 400


# ── A closed Job is not an active score/review candidate (finding 3) ───────


def test_a_closed_job_cannot_be_scored_or_rejected(client, actors, scorable):
    scorable(actors.consultant.username)
    job_id = _create(client, actors)["job"]["id"]
    client.post(f"/api/jobs/{job_id}/close")

    score = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    )
    assert score.status_code == 409, score.text
    assert "closed" in score.json()["detail"].lower()

    reject = client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    assert reject.status_code == 409
    # 409 for both — one documented state error, not two different answers.
    assert reject.json()["detail"] == score.json()["detail"]


def test_closing_does_not_retract_a_review_already_made(client, actors, db):
    """Refusing *new* reviews is a lifecycle rule, not a retraction: a decision
    validly made before closure stands, and stays visible."""
    job_id = _create(client, actors)["job"]["id"]
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    client.post(f"/api/jobs/{job_id}/close")

    assert db.query(JobReview).count() == 1
    body = client.get(f"/api/jobs/{job_id}").json()
    assert [r["decision"] for r in body["reviews"]] == ["reject"]


def test_a_closed_job_stays_listed_fetchable_and_editable(client, actors):
    """The refusal is narrow. Closing must not amount to a soft delete."""
    job_id = _create(client, actors)["job"]["id"]
    client.post(f"/api/jobs/{job_id}/close")

    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get("/api/jobs").json()["total"] == 1
    # Editing a closed record is still allowed — a posting may need correcting
    # after the fact.
    edited = client.patch(
        f"/api/jobs/{job_id}",
        json=_payload(actors.consultant.username, title="Corrected Title"),
    )
    assert edited.status_code == 200
    assert edited.json()["title"] == "Corrected Title"
    assert edited.json()["status"] == "closed"


def test_the_service_refuses_a_closed_job_even_bypassing_the_route(
    db, actors, client
):
    """The precondition lives at the write boundary, so a future second caller
    of the service cannot forget it."""
    job_id = _create(client, actors)["job"]["id"]
    client.post(f"/api/jobs/{job_id}/close")

    job = db.get(Job, uuid.UUID(job_id))
    consultant = db.get(User, actors.consultant.user_id)
    with pytest.raises(service.JobServiceError) as exc:
        service.record_review(
            db, job, consultant, decision="reject", actor=consultant
        )
    assert exc.value.status == 409


def test_closing_a_job_does_not_delete_or_reopen_anything(client, actors, db):
    job_id = _create(client, actors)["job"]["id"]
    client.post(f"/api/jobs/{job_id}/close")
    assert db.query(Job).count() == 1
    assert db.query(JobDecisionPair).count() == 0
    # There is no reopen route; the only status change is close.
    assert client.post(f"/api/jobs/{job_id}/open").status_code == 404


# ── Acceptance 3: on-demand advisory scoring ───────────────────────────────


@pytest.fixture
def scorable(tmp_path, monkeypatch):
    """Give a consultant a minimal but real profile.md + matching_config.yaml
    under an isolated DATA_DIR, so the deterministic scorer runs for real
    rather than against a mock."""

    def _install(username: str) -> None:
        root = tmp_path / username
        root.mkdir(parents=True, exist_ok=True)
        profile = {
            "career_history": {
                "role_groups": [
                    {
                        "organization": {"industries": ["automotive"]},
                        "blocks": [
                            {
                                "languages": ["C"],
                                "tools": ["CAN"],
                                "processes_standards": ["ISO 26262"],
                                "domains": ["automotive"],
                                "evidence_items": [
                                    {"text": "Owned an automotive ECU in C."}
                                ],
                            }
                        ],
                    }
                ]
            }
        }
        (root / "profile.md").write_text(
            "# Profile\n\n```yaml\n" + yaml.dump(profile) + "```\n",
            encoding="utf-8",
        )
        # Every key in shared.matching_config.REQUIRED_KEYS must be present or
        # load_config validates the file away and returns None.
        cfg = {
            "schema_version": 1,
            "consultant_id": username,
            "scoring_weights": {
                "expertise_fit": 0.30,
                "role_fit": 0.30,
                "growth_fit": 0.20,
                "interest_fit": 0.15,
                "practical_fit": 0.05,
            },
            # `thresholds.maybe` is the scorer's own internal band label, not
            # the removed review verdict — Slice 1 changes no scoring maths.
            "thresholds": {"keep": 76, "maybe": 58},
            "term_catalogs": {
                "languages": {"high_signal": ["C"]},
                "tools": {"standard": ["CAN"]},
                "methods": {"high_signal": ["ISO 26262"]},
            },
            "role_archetypes": [
                {"title_patterns": ["embedded engineer"], "seniority_bonus": 0.1}
            ],
            "growth_signals": {"primary": ["own"], "secondary": [], "penalize": []},
            "interest_signals": {"preferred": ["end to end"], "avoid": []},
            "blockers": {"hard": [], "soft": []},
            "location_scores": {"default_score": 60, "rules": []},
        }
        (root / "matching_config.yaml").write_text(
            yaml.dump(cfg), encoding="utf-8"
        )

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.profile_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.matching.config_reader.DATA_DIR", tmp_path)
    return _install


def test_score_is_cached_separately_from_the_job(client, actors, db, scorable):
    scorable(actors.consultant.username)
    job_id = _create(client, actors)["job"]["id"]

    r = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scored"] is True
    assert 0 <= body["match_score"] <= 100
    assert set(body["score_breakdown"]) >= {"expertise_fit", "role_fit"}

    # Nothing about the score reached the Job row or its API projection.
    job = db.get(Job, uuid.UUID(job_id))
    assert not any("score" in c.name for c in job.__table__.columns)
    assert "match_score" not in job.to_dict()
    from backend.db.models import JobConsultantMatch
    assert db.query(JobConsultantMatch).count() == 1


def test_closed_jobs_are_excluded_from_unscored_count_and_minimum_score(
    client, actors, scorable
):
    """Regression: a closed Job never gets a job_consultant_matches row (it
    was never eligible for scoring) and must not be confused with one that
    failed to score. Before this fix, closing a Job made list_jobs count it
    as "unscored", and minimum_score=0 — the most permissive threshold —
    silently dropped it from the response entirely."""
    scorable(actors.consultant.username)
    job_id = _create(client, actors)["job"]["id"]
    client.post(f"/api/jobs/{job_id}/close")

    actors.login(client, actors.consultant)
    body = client.get("/api/jobs").json()
    assert body["total"] == 1
    assert body["unscored_count"] == 0

    filtered = client.get("/api/jobs", params={"minimum_score": 0}).json()
    assert filtered["total"] == 1
    assert filtered["jobs"][0]["id"] == job_id


def test_minimum_score_filters_open_jobs_by_their_actual_score(
    client, actors, scorable
):
    scorable(actors.consultant.username)
    job_id = _create(client, actors)["job"]["id"]

    actors.login(client, actors.consultant)
    score = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    ).json()["match_score"]

    below = client.get("/api/jobs", params={"minimum_score": score}).json()
    assert job_id in [j["id"] for j in below["jobs"]]
    assert below["unscored_count"] == 0

    above = client.get("/api/jobs", params={"minimum_score": score + 1}).json()
    assert job_id not in [j["id"] for j in above["jobs"]]


def test_order_by_score_ranks_the_better_match_first(client, actors, scorable):
    scorable(actors.consultant.username)
    strong = _create(client, actors)["job"]["id"]
    weak = _create(
        client,
        actors,
        title="Marketing Copywriter",
        description=(
            "We need a copywriter to write blog posts and social media "
            "captions for a retail fashion brand's seasonal campaigns."
        ),
    )["job"]["id"]

    actors.login(client, actors.consultant)
    body = client.get("/api/jobs", params={"order": "score"}).json()
    ids = [j["id"] for j in body["jobs"]]
    assert ids.index(strong) < ids.index(weak)


def test_missing_profile_is_an_honest_failure_not_a_zero_or_a_500(
    client, actors, scorable
):
    # `scorable` isolates DATA_DIR but installs nothing for this consultant.
    job_id = _create(client, actors)["job"]["id"]
    r = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scored"] is False
    assert body["reason"] == matching_service.REASON_NO_PROFILE
    # Null, not 0.0 — a zero would read as a genuinely poor match.
    assert body["match_score"] is None


def test_missing_config_is_reported_separately(client, actors, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.profile_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.matching.config_reader.DATA_DIR", tmp_path)
    root = tmp_path / actors.consultant.username
    root.mkdir(parents=True)
    (root / "profile.md").write_text("```yaml\ncareer_history: {}\n```", "utf-8")

    job_id = _create(client, actors)["job"]["id"]
    body = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    ).json()
    assert body["scored"] is False
    assert body["reason"] == matching_service.REASON_NO_CONFIG


def test_opted_out_consultant_is_not_scored_and_profile_is_not_read(
    client, actors, scorable, monkeypatch
):
    """STAB-1: the Art 21 `actively_looking` opt-out. A consultant marked not
    actively looking is refused a score the same honest way a missing
    profile/config is, and the profile is never read for the attempt."""
    scorable(actors.consultant.username)
    job_id = _create(client, actors)["job"]["id"]

    def _fail_if_called(username):
        raise AssertionError(
            "load_profile must not be called for an opted-out consultant"
        )

    monkeypatch.setattr(matching_service, "load_profile", _fail_if_called)

    actors.login(client, actors.ta)
    toggle = client.post(
        f"/api/ta/consultants/{actors.consultant.username}/looking",
        json={"actively_looking": False},
    )
    assert toggle.status_code == 200, toggle.text

    r = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scored"] is False
    assert body["reason"] == matching_service.REASON_OPTED_OUT
    assert body["reason"] not in {
        matching_service.REASON_NO_PROFILE,
        matching_service.REASON_INVALID_PROFILE,
        matching_service.REASON_NO_CONFIG,
    }
    assert body["match_score"] is None


def test_opting_back_in_resumes_scoring_with_no_persisted_suppression(
    client, actors, scorable
):
    scorable(actors.consultant.username)
    job_id = _create(client, actors)["job"]["id"]

    actors.login(client, actors.ta)
    client.post(
        f"/api/ta/consultants/{actors.consultant.username}/looking",
        json={"actively_looking": False},
    )
    off = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    ).json()
    assert off["scored"] is False

    client.post(
        f"/api/ta/consultants/{actors.consultant.username}/looking",
        json={"actively_looking": True},
    )
    on = client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    ).json()
    assert on["scored"] is True
    assert on["match_score"] is not None


def test_opt_out_hides_opportunities_but_preserves_application_history(client, actors):
    job_id = _create(client, actors)["job"]["id"]
    r = client.post("/api/applications", json={"job_id": job_id, "consultant_username": actors.consultant.username})
    assert r.status_code == 201
    application = r.json()["application"]["id"]
    assert client.post(f"/api/ta/consultants/{actors.consultant.username}/looking", json={"actively_looking": False}).status_code == 200
    actors.login(client, actors.consultant)
    assert client.get("/api/jobs").json()["total"] == 0
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/applications/{application}").status_code == 200
    assert client.post(f"/api/applications/{application}/status", json={"status": "withdrawn"}).status_code == 200



def test_unscorable_job_is_still_listed_and_fetchable(client, actors, scorable):
    """Invariant 7: a missing score never hides or rejects an otherwise valid
    Job."""
    job_id = _create(client, actors)["job"]["id"]
    client.get(
        f"/api/jobs/{job_id}/score",
        params={"consultant_username": actors.consultant.username},
    )
    assert client.get("/api/jobs").json()["total"] == 1
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "open"


def test_same_site_consultant_needs_no_decision_pair_to_score(client, actors, scorable, db):
    scorable(actors.admin.username)
    job_id = _create(client, actors)["job"]["id"]
    actors.login(client, actors.admin)
    r = client.get(f"/api/jobs/{job_id}/score", params={"consultant_username": actors.admin.username})
    assert r.status_code == 200
    assert r.json()["scored"]
    assert db.query(JobDecisionPair).count() == 0



def test_scoring_text_carries_the_manual_entry_fields(db, actors):
    job = Job(
        title="Senior Embedded Engineer",
        company="Client AB",
        location="Göteborg",
        description=DESCRIPTION,
        content_hash="x" * 64,
        status="open",
    )
    text = matching_service.job_scoring_text(job)
    # Title first, so the scorer's leading-window title heuristic still works.
    assert text.startswith("Senior Embedded Engineer")
    assert "Client AB" in text and "Göteborg" in text and "AUTOSAR" in text


# ── Acceptance 4: Reject, idempotent, and no Maybe ─────────────────────────


def test_reject_is_idempotent(client, actors, db):
    job_id = _create(client, actors)["job"]["id"]
    payload = {"consultant_username": actors.consultant.username}

    first = client.post(f"/api/jobs/{job_id}/reject", json=payload)
    assert first.status_code == 200
    assert first.json()["created"] is True

    second = client.post(f"/api/jobs/{job_id}/reject", json=payload)
    assert second.json()["created"] is False
    assert second.json()["review"]["id"] == first.json()["review"]["id"]

    assert db.query(JobReview).count() == 1
    # The Job itself is untouched by a review.
    assert db.get(Job, uuid.UUID(job_id)).status == "open"


def test_related_ta_and_consultant_share_one_review_marker(client, actors, db):
    job_id = _create(client, actors)["job"]["id"]
    payload = {"consultant_username": actors.consultant.username}
    client.post(f"/api/jobs/{job_id}/reject", json=payload)

    actors.logout(client)
    actors.login(client, actors.consultant)
    again = client.post(f"/api/jobs/{job_id}/reject", json=payload)
    assert again.json()["created"] is False
    assert db.query(JobReview).count() == 1


def test_reject_collects_and_echoes_no_reason(client, actors):
    job_id = _create(client, actors)["job"]["id"]
    r = client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 200
    assert "reason" not in r.json()["review"]
    assert not hasattr(JobReview, "reason")

    # A legacy client still sending one is refused, not silently dropped.
    legacy = client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username, "reason": "x"},
    )
    assert legacy.status_code == 422


def test_unauthorized_actor_can_neither_create_nor_read_a_review(client, actors):
    job_id = _create(client, actors)["job"]["id"]
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )

    actors.logout(client)
    actors.login(client, actors.other_ta)
    assert client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    ).status_code == 404
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_the_review_shows_on_the_list_for_the_actors_own_consultants(
    client, actors
):
    job_id = _create(client, actors)["job"]["id"]
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    row = client.get("/api/jobs").json()["jobs"][0]
    assert row["reviews"] == {str(actors.consultant.user_id): "reject"}


def test_there_is_no_maybe_anywhere_in_the_api(client, actors):
    """P-008: Maybe is gone — no endpoint, no accepted value, no translation."""
    job_id = _create(client, actors)["job"]["id"]

    # No route accepts it.
    assert client.post(
        f"/api/jobs/{job_id}/maybe",
        json={"consultant_username": actors.consultant.username},
    ).status_code == 404

    # And the service refuses the value outright rather than mapping it.
    with pytest.raises(service.JobServiceError):
        service.record_review(
            None, None, None, decision="maybe", actor=None
        )


def test_the_schema_itself_cannot_hold_maybe(db, actors, client):
    job_id = _create(client, actors)["job"]["id"]
    from backend.db.models.job import REVIEW_DECISION_VALUES

    assert REVIEW_DECISION_VALUES == {"reject"}
    del job_id


# ── Every route maps a service error (S2-P1-R3) ───────────────────────────


@pytest.mark.parametrize(
    "service_function,request_call",
    [
        (
            "create_job",
            lambda c, job, user: c.post("/api/jobs", json=_payload(user)),
        ),
        (
            "update_job",
            lambda c, job, user: c.patch(f"/api/jobs/{job}", json=_payload(user)),
        ),
        ("close_job", lambda c, job, user: c.post(f"/api/jobs/{job}/close")),
        (
            "ensure_job_is_open",
            lambda c, job, user: c.get(
                f"/api/jobs/{job}/score", params={"consultant_username": user}
            ),
        ),
        (
            "record_review",
            lambda c, job, user: c.post(
                f"/api/jobs/{job}/reject", json={"consultant_username": user}
            ),
        ),
        (
            "withdraw_review",
            lambda c, job, user: c.delete(
                f"/api/jobs/{job}/reject", params={"consultant_username": user}
            ),
        ),
    ],
)
def test_every_job_route_maps_a_service_error_rather_than_500(
    client, actors, monkeypatch, service_function, request_call
):
    """`JobServiceError` has no global handler, so a route that forgets the
    `try/except` loses the service's status and escapes as a 500.

    That is not hypothetical: when `lock_job` gained a 404 for a Job that
    vanished between authorization and the lock, `POST /{id}/close` was the one
    caller without the wrapper, and the intended 404 became a 500. A test for
    the close route alone would not have caught the next one, so this asserts
    the property for every route that calls the service.

    Each case forces the service function that route uses to raise, and asserts
    the mapped status. An unmapped error does not merely fail the assertion —
    `TestClient` re-raises it, so the case errors out, which is if anything a
    louder failure.
    """
    job_id = _create(client, actors)["job"]["id"]
    username = actors.consultant.username

    def boom(*_args, **_kwargs):
        raise service.JobServiceError("Job not found", status=404)

    monkeypatch.setattr(service, service_function, boom)
    r = request_call(client, job_id, username)
    assert r.status_code == 404, (
        f"{service_function} escaped unmapped: {r.status_code}"
    )
    assert r.json()["detail"] == "Job not found"


# ── Acceptance 5: nothing unrequested happened ─────────────────────────────


def test_no_v1_job_routes_remain_mounted(client):
    paths = {route.path for route in client.app.routes}
    for retired in (
        "/api/opportunities/ingest",
        "/api/opportunities/run-matching",
        "/api/dashboard/{consultant_id}/positions",
        "/api/dashboard/{consultant_id}/verdict",
        "/api/ta/job-store/summary",
        "/api/ta/job-store/positions",
        "/opportunity-dashboard.html",
    ):
        assert retired not in paths, f"{retired} is still mounted"


def test_creating_a_job_enqueues_no_workflow_run(client, actors, db):
    """Matching stays synchronous and on demand for the pilot (P-007): Slice 1
    is not a workflow-runner consumer."""
    from backend.db.models import WorkflowRun

    _create(client, actors)
    assert db.query(WorkflowRun).count() == 0
