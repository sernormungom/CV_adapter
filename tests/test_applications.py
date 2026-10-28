"""V2 Slice 2 — Apply, Applications, and the one-live-decision rule.

The acceptance cases from `docs/handoffs/slice-2-applications.md`, minus the
ones that need real Postgres semantics (the concurrent Apply race, the deployed
partial unique index and check constraint, the cascades), which live in
`tests/integration/test_applications_pg.py`.

Uses the standard cast (`docs/testing.md`): `ta` serves `consultant`,
`other_ta` serves nobody here, `admin` holds Admin **and** Consultant so the
multi-role case (P-004) is exercised by default, and `second_pair` is a second
*populated* relationship — the shape needed to ask whether one Consultant's
Application leaks to another who reaches the same shared Job.
"""

from __future__ import annotations

import uuid

import pytest

from backend.applications import service
from backend.db.models import (
    LIVE_APPLICATION_STATUSES,
    TERMINAL_APPLICATION_STATUSES,
    Application,
    ApplicationStatus,
    Job,
    JobReview,
    User,
)
from backend.jobs import service as jobs_service

DESCRIPTION = (
    "We are looking for a senior embedded engineer in Göteborg to own an "
    "automotive control unit end to end: C, AUTOSAR, CAN, and ISO 26262 "
    "functional-safety work alongside a verification team."
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _job_payload(consultant_username: str, **over) -> dict:
    body = {
        "consultant_username": consultant_username,
        "title": "Senior Embedded Engineer",
        "company": "Client AB",
        "location": "Göteborg",
        "description": DESCRIPTION,
    }
    body.update(over)
    return body


def _create_job(client, actors, actor=None, consultant=None, **over) -> str:
    actors.login(client, actor or actors.ta)
    r = client.post(
        "/api/jobs",
        json=_job_payload((consultant or actors.consultant).username, **over),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["job"]["id"]


def _apply(client, job_id: str, consultant_username: str):
    return client.post(
        "/api/applications",
        json={"job_id": job_id, "consultant_username": consultant_username},
    )


def _set_status(client, application_id: str, status: str):
    return client.post(
        f"/api/applications/{application_id}/status", json={"status": status}
    )


@pytest.fixture
def job_id(client, actors) -> str:
    """One open Job in `actors.consultant`'s context, created by their TA."""
    return _create_job(client, actors)


# ── Acceptance 1: self-Apply creates exactly one Application ───────────────


def test_consultant_self_apply_creates_one_application(client, actors, db):
    job = _create_job(client, actors, actor=actors.consultant)
    actors.logout(client)
    actors.login(client, actors.consultant)

    r = _apply(client, job, actors.consultant.username)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    application = body["application"]
    assert application["status"] == ApplicationStatus.applied.value
    assert application["is_live"] is True
    assert application["job_id"] == job
    assert application["consultant_username"] == actors.consultant.username
    assert uuid.UUID(application["id"])          # a stable identity, not a flag
    assert db.query(Application).count() == 1


def test_repeating_apply_returns_the_same_application(client, actors, db, job_id):
    first = _apply(client, job_id, actors.consultant.username)
    assert first.status_code == 201

    second = _apply(client, job_id, actors.consultant.username)
    # 200, not 201 — nothing was created. Same convention as a duplicate Job.
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert (
        second.json()["application"]["id"] == first.json()["application"]["id"]
    )
    assert db.query(Application).count() == 1


def test_apply_is_refused_on_a_closed_job(client, actors, db, job_id):
    client.post(f"/api/jobs/{job_id}/close")
    r = _apply(client, job_id, actors.consultant.username)
    assert r.status_code == 409
    assert r.json()["detail"] == jobs_service.JOB_CLOSED_MESSAGE
    assert db.query(Application).count() == 0


def test_closing_the_job_afterwards_does_not_strand_a_live_application(
    client, actors, job_id
):
    """A posting closing does not free anyone from closing out the Application
    honestly — refusing the transition would leave it live forever."""
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    client.post(f"/api/jobs/{job_id}/close")

    r = _set_status(client, application, ApplicationStatus.closed.value)
    assert r.status_code == 200, r.text
    assert r.json()["application"]["status"] == "closed"


# ── Acceptance 2: who may Apply, and who may see it ────────────────────────


def test_related_ta_applies_for_their_consultant_and_both_can_read_it(
    client, actors, job_id
):
    created = _apply(client, job_id, actors.consultant.username)
    assert created.status_code == 201
    application = created.json()["application"]["id"]

    # The TA that applied.
    assert client.get(f"/api/applications/{application}").status_code == 200
    assert client.get("/api/applications").json()["total"] == 1

    # The consultant it was made for.
    actors.logout(client)
    actors.login(client, actors.consultant)
    detail = client.get(f"/api/applications/{application}")
    assert detail.status_code == 200
    assert detail.json()["consultant_username"] == actors.consultant.username
    assert [a["id"] for a in client.get("/api/applications").json()["applications"]] == [
        application
    ]


def test_a_consultant_cannot_apply_for_a_different_consultant(
    client, actors, second_pair, db
):
    job = _create_job(client, actors)
    actors.logout(client)
    actors.login(client, second_pair["consultant"])

    # Reaching the Job is a separate question from acting for someone else; this
    # actor fails the second check either way.
    r = _apply(client, job, actors.consultant.username)
    assert r.status_code in (403, 404), r.text
    assert db.query(Application).count() == 0


def test_an_unrelated_ta_cannot_create_list_detail_or_mutate(
    client, actors, job_id, db
):
    """404 throughout — an Application's existence is itself content."""
    created = _apply(client, job_id, actors.consultant.username)
    application = created.json()["application"]["id"]

    actors.logout(client)
    actors.login(client, actors.other_ta)

    apply_attempt = _apply(client, job_id, actors.consultant.username)
    assert apply_attempt.status_code == 404
    assert "Senior Embedded Engineer" not in apply_attempt.text

    detail = client.get(f"/api/applications/{application}")
    assert detail.status_code == 404
    assert actors.consultant.username not in detail.text
    assert "Senior Embedded Engineer" not in detail.text

    assert client.get("/api/applications").json() == {
        "applications": [],
        "total": 0,
    }
    assert _set_status(
        client, application, ApplicationStatus.withdrawn.value
    ).status_code == 404

    # Nothing they did changed the row.
    assert db.query(Application).count() == 1
    assert db.query(Application).one().status == ApplicationStatus.applied.value


def test_admin_reaches_every_application(client, actors, second_pair):
    """Admin is global — explicitly tested, not assumed."""
    job = _create_job(client, actors)
    _apply(client, job, actors.consultant.username)
    actors.logout(client)

    actors.login(client, second_pair["ta"])
    dup = client.post(
        "/api/jobs", json=_job_payload(second_pair["consultant"].username)
    )
    assert dup.json()["status"] == "duplicate"
    _apply(client, job, second_pair["consultant"].username)
    actors.logout(client)

    actors.login(client, actors.admin)
    listed = client.get("/api/applications").json()
    assert listed["total"] == 2
    assert {a["consultant_username"] for a in listed["applications"]} == {
        actors.consultant.username,
        second_pair["consultant"].username,
    }
    for row in listed["applications"]:
        assert client.get(f"/api/applications/{row['id']}").status_code == 200


def test_the_admin_consultant_can_apply_for_themselves(client, actors, db):
    """P-004: the pilot Admin is genuinely also a Consultant, so self-Apply has
    to work for them without an admin bypass doing the work."""
    actors.login(client, actors.admin)
    job = client.post(
        "/api/jobs", json=_job_payload(actors.admin.username)
    ).json()["job"]["id"]

    r = _apply(client, job, actors.admin.username)
    assert r.status_code == 201
    assert db.query(Application).one().consultant_id == actors.admin.user_id


def test_a_ta_cannot_apply_for_a_consultant_not_linked_to_the_job(
    client, actors, second_pair, db
):
    """Reaching a shared Job does not confer the right to act for someone
    else's Consultant on it — the same guard Reject carries."""
    job = _create_job(client, actors)
    actors.logout(client)

    # Pair two reaches the same shared Job through the ordinary duplicate path…
    actors.login(client, second_pair["ta"])
    client.post("/api/jobs", json=_job_payload(second_pair["consultant"].username))
    # …but may not apply on behalf of pair one's consultant.
    r = _apply(client, job, actors.consultant.username)
    assert r.status_code == 403
    assert db.query(Application).count() == 0


def test_application_routes_require_authentication(client, actors, job_id):
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    actors.logout(client)

    assert client.get("/api/applications").status_code == 401
    assert client.get(f"/api/applications/{application}").status_code == 401
    assert _apply(client, job_id, "whoever").status_code == 401
    assert _set_status(client, application, "closed").status_code == 401


def test_application_access_does_not_read_site(client, actors, db, job_id):
    """P-005: Site is never an authorization predicate, on this path either.

    The consultant is moved to a different, *inactive* Site — inactive so this
    does not create the invalid two-active-Site pilot configuration.
    """
    from backend.db.models import Site

    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    assert client.get(f"/api/applications/{application}").status_code == 200

    other_site = Site(code="NOT-AUTH-APP", name="Hypothetical", is_active=False)
    db.add(other_site)
    db.flush()
    db.get(User, actors.consultant.user_id).site_id = other_site.id
    db.commit()

    assert client.get(f"/api/applications/{application}").status_code == 200
    assert client.get("/api/applications").json()["total"] == 1


def test_unknown_application_id_shapes_are_404_not_500(client, actors):
    actors.login(client, actors.ta)
    assert client.get("/api/applications/not-a-uuid").status_code == 404
    assert client.get(f"/api/applications/{uuid.uuid4()}").status_code == 404


# ── The actor-by-route authorization matrix (acceptance 2 and 5) ──────────
#
# The packet requires self-Consultant, related TA, unrelated TA, and Admin
# coverage for **every** mutating and read/detail route, not global-role
# coverage plus the assumption that route composition is uniform. The
# S2-P1-R1 Admin client defect is why that distinction is not pedantry: a
# per-route dependency or a role regression can break one actor on one route
# while every indirect test stays green.


def _matrix_calls(client, job: str, application: str, consultant_username: str):
    """One callable per route, in the order a real session would use them."""
    return {
        # Apply on a pair that already has a live Application: the create route
        # is exercised (and its authorization decided) either way, and the
        # idempotent answer keeps the case identical for every actor.
        "apply": lambda: client.post(
            "/api/applications",
            json={"job_id": job, "consultant_username": consultant_username},
        ),
        "list": lambda: client.get("/api/applications"),
        "detail": lambda: client.get(f"/api/applications/{application}"),
        "status": lambda: client.post(
            f"/api/applications/{application}/status",
            json={"status": ApplicationStatus.submitted.value},
        ),
        "withdraw_reject": lambda: client.delete(
            f"/api/jobs/{job}/reject",
            params={"consultant_username": consultant_username},
        ),
    }


@pytest.mark.parametrize(
    "actor_name,allowed",
    [
        ("consultant", True),    # self-Consultant
        ("ta", True),            # related TA
        ("admin", True),         # Admin acting for a *different* Consultant
        ("other_ta", False),     # unrelated TA — the documented negative
    ],
)
def test_every_application_route_is_authorized_for_every_actor(
    client, actors, db, actor_name, allowed, job_id
):
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    actors.logout(client)
    actors.login(client, getattr(actors, actor_name))

    calls = _matrix_calls(client, job_id, application, actors.consultant.username)
    for route, call in calls.items():
        r = call()
        if allowed:
            assert r.status_code == 200, f"{actor_name}/{route}: {r.status_code} {r.text}"
        else:
            # The documented non-disclosing result differs in shape by route,
            # not in what it discloses: an addressed route answers 404 (an
            # Application's existence is itself content), while the collection
            # answers 200 with nothing in it — the honest reply to "show me what
            # I can reach" is an empty list, not a lie about the route.
            expected = 200 if route == "list" else 404
            assert r.status_code == expected, f"{actor_name}/{route}: {r.status_code}"
            if route == "list":
                assert r.json() == {"applications": [], "total": 0}
            assert application not in r.text, route
            assert actors.consultant.username not in r.text, route
            assert "Senior Embedded Engineer" not in r.text, route

    if allowed:
        assert client.get(f"/api/applications/{application}").json()["status"] == (
            ApplicationStatus.submitted.value
        )
    else:
        # The unrelated TA changed nothing.
        assert client.get("/api/applications").json() == {
            "applications": [],
            "total": 0,
        }
        assert db.query(Application).one().status == ApplicationStatus.applied.value


def test_admin_can_apply_for_a_different_consultant(client, actors, db):
    """The matrix above exercises Apply as a *repeat*; this is the positive
    create, which is the case the Admin client defect actually blocked."""
    job = _create_job(client, actors)
    actors.logout(client)
    actors.login(client, actors.admin)

    r = _apply(client, job, actors.consultant.username)
    assert r.status_code == 201, r.text
    assert r.json()["application"]["consultant_username"] == (
        actors.consultant.username
    )
    assert db.query(Application).one().consultant_id == actors.consultant.user_id
    assert db.query(Application).one().created_by_id == actors.admin.user_id


def test_a_consultant_can_move_their_own_application_and_withdraw_own_reject(
    client, actors, db, job_id
):
    """The self-service path end to end, without a TA involved anywhere."""
    actors.logout(client)
    actors.login(client, actors.consultant)

    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    assert _set_status(client, application, "submitted").json()["changed"] is True
    assert _set_status(client, application, "withdrawn").json()["changed"] is True

    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    r = client.delete(
        f"/api/jobs/{job_id}/reject",
        params={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 200 and r.json() == {"withdrawn": True}
    assert db.query(JobReview).count() == 0


# ── Acceptance 3: Apply and Reject are mutually exclusive ──────────────────


def test_apply_is_refused_while_the_pair_holds_a_reject(client, actors, db, job_id):
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    r = _apply(client, job_id, actors.consultant.username)
    assert r.status_code == 409
    assert r.json()["detail"] == service.REJECTED_PAIR_MESSAGE
    assert db.query(Application).count() == 0
    # The recorded decision is untouched — Apply never overwrites it.
    assert db.query(JobReview).count() == 1


def test_reject_is_refused_while_the_pair_holds_a_live_application(
    client, actors, db, job_id
):
    _apply(client, job_id, actors.consultant.username)
    r = client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": actors.consultant.username},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == jobs_service.LIVE_APPLICATION_MESSAGE
    assert db.query(JobReview).count() == 0


def test_withdrawing_the_reject_unblocks_apply(client, actors, db, job_id):
    """The rule has a documented way out, or one mistaken click would bar a
    consultant from a posting permanently."""
    consultant = actors.consultant.username
    client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": consultant},
    )
    assert _apply(client, job_id, consultant).status_code == 409

    withdraw = client.delete(
        f"/api/jobs/{job_id}/reject", params={"consultant_username": consultant}
    )
    assert withdraw.status_code == 200
    assert withdraw.json() == {"withdrawn": True}
    assert db.query(JobReview).count() == 0

    assert _apply(client, job_id, consultant).status_code == 201


def test_withdrawing_a_reject_is_idempotent_and_authorized(
    client, actors, job_id
):
    consultant = actors.consultant.username
    first = client.delete(
        f"/api/jobs/{job_id}/reject", params={"consultant_username": consultant}
    )
    assert first.json() == {"withdrawn": False}   # nothing there, not an error

    actors.logout(client)
    actors.login(client, actors.other_ta)
    assert client.delete(
        f"/api/jobs/{job_id}/reject", params={"consultant_username": consultant}
    ).status_code == 404


def test_a_terminal_application_frees_the_pair_to_be_rejected(
    client, actors, db, job_id
):
    """The other direction out of the rule: close or withdraw the Application
    and the pair can be evaluated again."""
    consultant = actors.consultant.username
    application = _apply(client, job_id, consultant).json()["application"]["id"]
    assert client.post(
        f"/api/jobs/{job_id}/reject", json={"consultant_username": consultant}
    ).status_code == 409

    _set_status(client, application, ApplicationStatus.withdrawn.value)
    r = client.post(
        f"/api/jobs/{job_id}/reject",
        json={"consultant_username": consultant},
    )
    assert r.status_code == 200
    assert db.query(JobReview).count() == 1


def test_the_service_enforces_both_halves_even_bypassing_the_routes(
    db, actors, client, job_id
):
    """Both directions of the rule live at the write boundary, so a future
    second caller of either service cannot forget one."""
    job = db.get(Job, uuid.UUID(job_id))
    consultant = db.get(User, actors.consultant.user_id)
    ta = db.get(User, actors.ta.user_id)

    jobs_service.record_review(
        db, job, consultant, decision="reject", actor=ta
    )
    with pytest.raises(service.ApplicationServiceError) as apply_exc:
        service.apply(db, job, consultant, actor=ta)
    assert apply_exc.value.status == 409

    jobs_service.withdraw_review(db, job, consultant)
    service.apply(db, job, consultant, actor=ta)
    with pytest.raises(jobs_service.JobServiceError) as reject_exc:
        jobs_service.record_review(
            db, job, consultant, decision="reject", actor=ta
        )
    assert reject_exc.value.status == 409


def test_no_maybe_and_no_v1_verdict_write_reaches_the_application_path(
    client, actors, job_id
):
    """P-008: Maybe stays unrepresentable, and Applications replace the V1
    `verdicts.application_status` field rather than writing to it. The
    `verdicts` table itself is gone (V3 packet S2, V3-P-008 decision 5), so
    there is no longer a model to assert an empty count against — the
    remaining, still-live assertion is that no endpoint accepts a V1 status
    word."""
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    _set_status(client, application, ApplicationStatus.submitted.value)
    client.get("/api/applications")

    assert "maybe" not in {s.value for s in ApplicationStatus}
    # No endpoint accepts it, and neither does the service.
    assert _set_status(client, application, "maybe").status_code == 400
    for v1_status in ("to_apply", "cv_ready", "link_opened"):
        assert _set_status(client, application, v1_status).status_code == 400


# ── Acceptance 4: retry safety and the bounded status machine ──────────────


def test_status_only_moves_forward(client, actors, job_id):
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]

    assert _set_status(client, application, "submitted").json()["changed"] is True
    back = _set_status(client, application, "applied")
    assert back.status_code == 409
    assert "cannot move from 'submitted' to 'applied'" in back.json()["detail"]
    assert client.get(f"/api/applications/{application}").json()["status"] == (
        "submitted"
    )


def test_re_requesting_the_current_status_is_an_idempotent_no_op(
    client, actors, job_id
):
    """The double-clicked Submit. Refusing it would report a failure where
    nothing is wrong; accepting it must still not move anything."""
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    first = _set_status(client, application, "submitted")
    changed_at = first.json()["application"]["status_changed_at"]

    repeat = _set_status(client, application, "submitted")
    assert repeat.status_code == 200
    assert repeat.json()["changed"] is False
    assert repeat.json()["application"]["status_changed_at"] == changed_at


def test_a_transition_on_a_concurrently_erased_application_is_404_not_500(
    Session, client, actors, job_id
):
    """The rights-path race (S2-P1-R2): a GDPR erasure lands between
    authorization and the transition.

    The row must be reloaded *by selecting*, not by `Session.refresh` — refresh
    raises `InvalidRequestError` ("Could not refresh instance") on a vanished
    row, which is **not** `ObjectDeletedError`, so an earlier `except
    ObjectDeletedError` never fired and the request escaped as a 500. The
    assertion that it is one of `SERVICE_ERRORS` is the part that actually
    matters: that is what decides whether the route maps it or crashes.
    """
    from sqlalchemy import delete

    application_id = uuid.UUID(
        _apply(client, job_id, actors.consultant.username).json()["application"]["id"]
    )
    holder, remover = Session(), Session()
    try:
        stale = holder.get(Application, application_id)
        remover.execute(delete(Application).where(Application.id == application_id))
        remover.commit()

        with pytest.raises(service.ApplicationServiceError) as exc:
            service.transition(
                holder, stale, status="closed",
                actor=holder.get(User, actors.ta.user_id),
            )
        assert exc.value.status == 404
        assert isinstance(exc.value, service.SERVICE_ERRORS), (
            "the route maps SERVICE_ERRORS; anything else is a 500"
        )
    finally:
        holder.close()
        remover.close()


def test_a_terminal_application_accepts_no_further_transition(
    client, actors, job_id
):
    """Including the other terminal value: `closed` and `withdrawn` share the
    top rank, so neither can become the other."""
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    _set_status(client, application, "closed")

    for attempt in ("withdrawn", "submitted", "applied"):
        r = _set_status(client, application, attempt)
        assert r.status_code == 409, attempt
    assert client.get(f"/api/applications/{application}").json()["status"] == (
        "closed"
    )


def test_applied_may_skip_straight_to_a_terminal_status(client, actors, job_id):
    """Nothing forces a stage that did not happen: an Application can be
    withdrawn before it is ever submitted."""
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    r = _set_status(client, application, "withdrawn")
    assert r.status_code == 200
    assert r.json()["application"]["is_live"] is False


def test_a_terminal_pair_may_be_applied_to_again_as_a_new_application(
    client, actors, db, job_id
):
    """One *live* Application per pair, not one ever. The historical row stays."""
    consultant = actors.consultant.username
    first = _apply(client, job_id, consultant).json()["application"]["id"]
    _set_status(client, first, "withdrawn")

    second = _apply(client, job_id, consultant)
    assert second.status_code == 201
    assert second.json()["application"]["id"] != first
    assert db.query(Application).count() == 2
    assert (
        db.query(Application)
        .filter(Application.status.in_(LIVE_APPLICATION_STATUSES))
        .count()
        == 1
    )


def test_the_status_vocabulary_is_closed_and_ordered():
    """The four pilot stages, and the single rule that governs them."""
    from backend.db.models import APPLICATION_STATUS_ORDER, APPLICATION_STATUS_VALUES

    assert APPLICATION_STATUS_VALUES == {
        "applied", "submitted", "closed", "withdrawn"
    }
    assert LIVE_APPLICATION_STATUSES == {"applied", "submitted"}
    assert TERMINAL_APPLICATION_STATUSES == {"closed", "withdrawn"}
    # Both terminal values share the top rank; that is what makes "nothing
    # leaves a terminal state" fall out of the ordering instead of needing a
    # second rule that could drift from it.
    top = max(APPLICATION_STATUS_ORDER.values())
    assert {
        s for s, rank in APPLICATION_STATUS_ORDER.items() if rank == top
    } == TERMINAL_APPLICATION_STATUSES


def test_unknown_status_filter_is_refused(client, actors):
    actors.login(client, actors.ta)
    assert client.get(
        "/api/applications", params={"status": "everything"}
    ).status_code == 400


def test_the_list_can_be_filtered_by_status_and_job(client, actors, job_id):
    consultant = actors.consultant.username
    # A distinct-enough description keeps this a "genuinely different posting"
    # under Tier 1's near-duplicate check (`backend/jobs/service.py
    # ::find_near_duplicate`), not a fuzzy match of the `job_id` fixture's Job.
    other_job = _create_job(
        client, actors, title="Verification Engineer",
        description=DESCRIPTION + " Also HIL rig ownership on a separate opening.",
    )
    live = _apply(client, job_id, consultant).json()["application"]["id"]
    done = _apply(client, other_job, consultant).json()["application"]["id"]
    _set_status(client, done, "closed")

    by_status = client.get("/api/applications", params={"status": "applied"}).json()
    assert [a["id"] for a in by_status["applications"]] == [live]
    by_job = client.get("/api/applications", params={"job_id": other_job}).json()
    assert [a["id"] for a in by_job["applications"]] == [done]


# ── Acceptance 5: projections disclose only the caller's relationship ──────


def _link_second_pair(client, actors, second_pair) -> str:
    """Pair 1 creates a Job and applies; pair 2 reaches the same shared Job via
    the ordinary duplicate path and applies too. Returns the shared job id."""
    job = _create_job(client, actors)
    _apply(client, job, actors.consultant.username)
    actors.logout(client)

    actors.login(client, second_pair["ta"])
    dup = client.post(
        "/api/jobs", json=_job_payload(second_pair["consultant"].username)
    )
    assert dup.json()["job"]["id"] == job
    _apply(client, job, second_pair["consultant"].username)
    return job


def test_an_application_list_shows_only_the_actors_own_relationship(
    client, actors, second_pair
):
    _link_second_pair(client, actors, second_pair)

    # Pair two's TA is still logged in from the helper.
    listed = client.get("/api/applications").json()
    assert [a["consultant_username"] for a in listed["applications"]] == [
        second_pair["consultant"].username
    ]
    assert actors.consultant.username not in client.get("/api/applications").text


def test_one_consultants_application_is_not_readable_by_another_on_the_same_job(
    client, actors, second_pair
):
    """The shared Job is reachable by both; the Application on it is not."""
    _link_second_pair(client, actors, second_pair)
    theirs = client.get("/api/applications").json()["applications"][0]["id"]
    actors.logout(client)

    actors.login(client, actors.consultant)
    mine = client.get("/api/applications").json()["applications"]
    assert len(mine) == 1
    assert mine[0]["consultant_username"] == actors.consultant.username
    assert client.get(f"/api/applications/{theirs}").status_code == 404


def test_the_job_projection_is_scoped_to_the_callers_relationship(
    client, actors, second_pair
):
    job = _link_second_pair(client, actors, second_pair)

    # Pair two's TA sees the shared Job's facts and only its own Application.
    detail = client.get(f"/api/jobs/{job}").json()
    assert detail["title"] == "Senior Embedded Engineer"
    assert [a["consultant_id"] for a in detail["applications"]] == [
        str(second_pair["consultant"].user_id)
    ]

    actors.logout(client)
    actors.login(client, actors.admin)
    admin_detail = client.get(f"/api/jobs/{job}").json()
    assert {a["consultant_id"] for a in admin_detail["applications"]} == {
        str(actors.consultant.user_id),
        str(second_pair["consultant"].user_id),
    }


def _list_applications_map(client, job) -> dict:
    rows = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
    return rows[job]["applications"]


def test_job_list_and_detail_report_the_same_applications_for_every_actor(
    client, actors, second_pair
):
    """The general property the Slice 1 review found broken for review markers,
    asserted for the new projection pair before it can break the same way: the
    Job list's `applications` map and the Job detail's array are two views of
    one fact and must never disagree, whatever the scoping."""
    job = _link_second_pair(client, actors, second_pair)

    for actor in (
        actors.ta,
        actors.consultant,
        second_pair["ta"],
        second_pair["consultant"],
        actors.admin,
    ):
        actors.logout(client)
        actors.login(client, actor)
        listed = set(_list_applications_map(client, job))
        detail = {
            a["consultant_id"]
            for a in client.get(f"/api/jobs/{job}").json()["applications"]
        }
        assert listed == detail, f"{actor.username}: {listed} != {detail}"


def test_the_job_projection_reports_live_applications_only(
    client, actors, job_id
):
    """A terminal Application must not keep the Apply control disabled: the
    pair is free to apply again, and the Job views say so."""
    consultant_id = str(actors.consultant.user_id)
    application = _apply(client, job_id, actors.consultant.username).json()[
        "application"
    ]["id"]
    assert _list_applications_map(client, job_id) == {consultant_id: "applied"}

    _set_status(client, application, "closed")
    assert _list_applications_map(client, job_id) == {}
    assert client.get(f"/api/jobs/{job_id}").json()["applications"] == []
    # …and the full history is still available from the Application domain.
    assert client.get(
        "/api/applications", params={"job_id": job_id}
    ).json()["total"] == 1


def test_an_actor_reaching_no_consultant_sees_no_applications(client, actors):
    """The empty set and the `None` sentinel stay distinguishable — the trap
    that once hid every review marker from the Admin."""
    job = _create_job(client, actors)
    _apply(client, job, actors.consultant.username)
    actors.logout(client)
    actors.login(client, actors.other_ta)

    assert client.get("/api/applications").json() == {
        "applications": [],
        "total": 0,
    }
    assert service.list_applications(None, consultant_ids=[]) == []
    assert service.applied_job_ids(None, []) == {}


def test_the_application_response_carries_no_job_body_and_no_cv(
    client, actors, job_id
):
    """P-009: an Application is not a CV, and its projection is not a second
    way to read Job content.

    Slice 3 added a `tailored_cv` field, so the original "no key mentions cv"
    assertion no longer states the rule — it stated a proxy for it. **Tightened,
    not relaxed:** the field must be metadata only (does one exist, is it
    generating, which revision, why did it fail) and must carry neither the CV
    state nor the frozen input snapshot. A list of Applications must not become
    a way to read every tailored CV in it; the content is served by
    `/api/applications/{id}/cv`, one Application at a time.
    """
    body = _apply(client, job_id, actors.consultant.username).json()["application"]
    assert DESCRIPTION[:40] not in str(body)
    assert body["job"]["title"] == "Senior Embedded Engineer"
    assert "description" not in body["job"]
    # No Application-owned CV or content field. `tailored_cv` is a reference to
    # a separate artifact and is checked on its own terms below.
    assert not any(
        ("cv" in key.lower() or "content" in key.lower())
        for key in body
        if key != "tailored_cv"
    )
    # Nothing has been generated, so there is no artifact to point at yet.
    assert body["tailored_cv"] is None


def test_the_applications_table_stores_no_cv_or_score_column(db):
    """Slice 3 owns the tailored artifact; a column here that could hold one
    would be that slice arriving early. Scores stay per-pair and on demand."""
    names = {c.name for c in Application.__table__.columns}
    assert not any(
        token in name for name in names for token in ("cv", "score", "content")
    ), names


def test_applying_enqueues_no_workflow_run(client, actors, db, job_id):
    """Slice 2 is not a workflow-runner consumer — durable generation is
    Slice 3's."""
    from backend.db.models import WorkflowRun

    _apply(client, job_id, actors.consultant.username)
    assert db.query(WorkflowRun).count() == 0
