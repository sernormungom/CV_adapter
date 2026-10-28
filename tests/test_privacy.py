"""Session 2 tests (Art 15/17/20): export ZIP contents + ordered erasure."""

import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import timedelta

from sqlalchemy import select

from backend import config
from backend.auth import service as auth_service
from backend.cv_state import service as cv_state_service
from backend.db.models import (
    Application,
    AuditLog,
    CVPublication,
    Invite,
    Job,
    JobReview,
    Notification,
    TAConsultant,
    User,
    UserSession,
)
from backend.notifications import service as notify_service
from backend.privacy import service
from backend.privacy.service import PrivacyError
from backend.cv_publish import service as cv_publish_service


def _admin(db):
    user, invite = auth_service.create_user(db, email="admin@x.com", role="admin")
    auth_service.accept_invite(db, invite, "Password123!x")
    return user


def _ta(db):
    user, invite = auth_service.create_user(db, email="ta@x.com", role="ta")
    auth_service.accept_invite(db, invite, "Password123!x")
    return user


def _consultant(db, ta):
    # `create_user` auto-links the TAConsultant association when `created_by`
    # is a TA — no separate association insert needed here.
    user, invite = auth_service.create_user(
        db, email="alice@x.com", role="consultant", created_by=ta
    )
    return user


def _seed(db, monkeypatch, tmp_path):
    ta = _ta(db)
    consultant = _consultant(db, ta)

    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    profile_dir = tmp_path / "alice"
    (profile_dir / "uploads").mkdir(parents=True)
    (profile_dir / "profile.md").write_text("# Alice\nSkills: Python")
    (profile_dir / "uploads" / "cv.pdf").write_bytes(b"%PDF-fake")

    # A real base-CV save, so the profile directory holds what a live one holds:
    # `cv_state.json` *and* the `.cv_state.lock` coordination file. Writing the
    # lock by hand would let the export's exclusion pass against a filename this
    # test invented rather than the one the service actually creates.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    cv_state_service.save_base("alice", {"iName": "Alice"}, expected_revision=0)

    auth_service.create_session(db, consultant)
    notify_service.notify(
        db,
        user_id=ta.id,
        type="cv_viewed",
        title="Hiring manager opened alice's CV",
        consultant_username="alice",
    )

    return ta, consultant


def _seed_publication(db, ta, consultant):
    """A publication for `consultant`, bound to a real Application-owned
    tailored CV (S5-P1-R1: a `CVPublication` is Application-owned, not a
    free-standing `(consultant_username, job_id)` pair) — isolated from
    `_seed`'s own Job/Application-free setup so the generic `_seed` helper
    stays usable by tests that assert exact `applications`/`applications.json`
    counts elsewhere in this file."""
    from backend.applications import service as applications_service
    from backend.db.models import TailoredCV
    from backend.jobs import service as jobs_service

    data = jobs_service.build_input(
        title="Senior Dev @ ACME",
        company="ACME",
        description="A publication-only Job, long enough to clear the "
        "job service's minimum-length floor for a description.",
    )
    created = jobs_service.create_job(db, data, consultant=consultant, actor=ta)
    application, _ = applications_service.apply(db, created.job, consultant, actor=ta)
    tailored = TailoredCV(
        application_id=application.id,
        status="ready",
        revision=1,
        content_hash="0" * 64,
        cv_state={"name": "Alice", "secret": "cv-body-alice"},
        input_snapshot={},
        input_hash="1" * 64,
        created_by_id=ta.id,
    )
    db.add(tailored)
    db.commit()
    cv_publish_service.publish_for_application(db, application=application, actor=ta)


def test_export_contains_all_categories_and_no_password_hash(db, monkeypatch, tmp_path):
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _seed_publication(db, ta, consultant)

    zip_bytes = service.export_consultant(db, "alice", admin)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = set(zf.namelist())

    assert "user.json" in names
    assert "verdicts.json" not in names
    assert "publications.json" in names
    assert "ta_associations.json" in names
    assert "job_associations.json" in names
    assert "applications.json" in names
    assert "profile/profile.md" in names
    assert "profile/uploads/cv.pdf" in names

    user_json = zf.read("user.json").decode()
    assert "password_hash" not in user_json

    # The base-CV compare-and-write lock is our scaffolding, not this person's
    # data. It sits inside the profile directory the export walks, so excluding
    # it is deliberate rather than incidental.
    assert not any(name.endswith(cv_state_service.LOCK_FILENAME) for name in names)

    pub_json = zf.read("publications.json").decode()
    # Metadata only — the CV snapshot (cv_state) is never in the export.
    assert "cv-body-alice" not in pub_json


def test_privacy_service_imports_cleanly_in_a_fresh_interpreter():
    """`S4-P1-R8` finding 3. `backend.privacy.service` needs
    `PROFILE_LOCK_FILENAME`/`SCRATCH_DIRNAME` at import time. They used to live
    in `backend.cv_importer.service`, which imports `backend.workflows` —
    whose own package `__init__` imports `backend.cv_importer.handler`, which
    in turn needed a constant back from `cv_importer.service` that was not yet
    defined at that point in the file. This resolved only because the app's
    own startup (`backend/main.py`) happens to import `backend.workflows`
    before anything reaches `cv_importer.service` — a fresh interpreter that
    imports `backend.privacy.service` first, skipping that ordering entirely,
    used to fail with `ImportError: cannot import name 'PROFILE_LOCK_FILENAME'
    from partially initialized module`. A subprocess, not an in-process
    `importlib.reload`, because the defect is about which module a *fresh*
    `sys.modules` reaches first — reloading in this same process would not
    reproduce it.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import backend.privacy.service"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_export_excludes_the_transient_import_scratch_directory(
    db, monkeypatch, tmp_path
):
    """`S4-P1-R7`. `.cv_import_tmp/` holds a raw uploaded CV for at most a
    bounded window before the handler or the retention sweep deletes it —
    never retained data. Before this, the export loop walked the whole
    profile directory with `rglob("*")` and had no reason to skip it, so a
    stray upload still sitting there when an export ran would have been
    bundled in as if it were retained content, duplicating the raw CV under a
    path the consultant never chose and this design otherwise goes out of its
    way to avoid keeping.
    """
    from backend.cv_importer.keys import PROFILE_LOCK_FILENAME, SCRATCH_DIRNAME

    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)

    scratch = tmp_path / "alice" / SCRATCH_DIRNAME
    scratch.mkdir(parents=True)
    (scratch / "abc123.pdf").write_bytes(b"%PDF-fake stranded upload")
    (tmp_path / "alice" / PROFILE_LOCK_FILENAME).touch()

    zip_bytes = service.export_consultant(db, "alice", admin)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = set(zf.namelist())

    assert not any(SCRATCH_DIRNAME in name for name in names), (
        "a stranded transient upload leaked into the Art 15 export"
    )
    assert not any(name.endswith(PROFILE_LOCK_FILENAME) for name in names)
    # Everything else the fixture seeded is still present -- the exclusion is
    # narrow, not a wrong turn that swallowed the whole profile directory.
    assert "profile/profile.md" in names
    assert "profile/uploads/cv.pdf" in names


# ── Relational Job data in export and erasure (V2 Slice 1) ─────────────────
#
# Access and erasure must describe the same set. Erasure deletes a consultant's
# Job links and their own review markers, so the Art 15/20 artefact has to
# disclose them — and neither may reach across to another consultant on the
# same shared Job.


def _shared_job(db, ta, subject):
    """One shared Job that `subject` and a second consultant are both linked
    to, each with their own Reject marker."""
    from backend.jobs import service as jobs_service

    other_ta, other_invite = auth_service.create_user(
        db, email="bob-ta@x.com", role="ta"
    )
    auth_service.accept_invite(db, other_invite, "Password123!x")
    other, _ = auth_service.create_user(
        db, email="bob@x.com", role="consultant", created_by=other_ta
    )

    data = jobs_service.build_input(
        title="Shared Embedded Role",
        company="Client AB",
        description=(
            "A shared posting long enough to clear the minimum-length floor "
            "for a job description in the service layer."
        ),
    )
    created = jobs_service.create_job(db, data, consultant=subject, actor=ta)
    jobs_service.create_job(db, data, consultant=other, actor=other_ta)

    jobs_service.record_review(
        db, created.job, subject, decision="reject", actor=ta,
    )
    jobs_service.record_review(
        db, created.job, other, decision="reject", actor=other_ta,
    )
    return created.job, other


def test_export_includes_the_subjects_job_links_and_own_review(
    db, monkeypatch, tmp_path
):
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _shared_job(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    payload = json.loads(zf.read("job_associations.json").decode())

    assert len(payload) == 1
    row = payload[0]
    assert row["title"] == "Shared Embedded Role"
    assert row["my_review"]["decision"] == "reject"
    assert "reason" not in row["my_review"]


def test_export_does_not_leak_another_consultants_review_from_a_shared_job(
    db, monkeypatch, tmp_path
):
    """A subject-access request is not a route to a third party's data, even
    when both are attached to the same shared Job."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _shared_job(db, ta, consultant)

    # Both rows are created within the same second, so give bob's a distinct
    # timestamp: that is what makes alice's exported marker attributable.
    bob_review = db.execute(
        select(JobReview).join(User, User.id == JobReview.consultant_id)
        .where(User.username == "bob")
    ).scalar_one()
    bob_review.created_at = bob_review.created_at + timedelta(days=1)
    db.commit()

    zip_bytes = service.export_consultant(db, "alice", admin)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    whole = b"".join(zf.read(name) for name in zf.namelist())

    # Structural check: the review rows did not merge — exactly one entry
    # for the shared Job, and its marker is alice's own row, not bob's (the
    # two reviews are told apart by their timestamps).
    associations = json.loads(zf.read("job_associations.json").decode())
    assert len(associations) == 1
    reviews = {
        r.consultant_id: r.created_at.isoformat()
        for r in db.query(JobReview).all()
    }
    alice_id = db.execute(select(User.id).where(User.username == "alice")).scalar_one()
    bob_id = db.execute(select(User.id).where(User.username == "bob")).scalar_one()
    assert reviews[alice_id] != reviews[bob_id]
    assert associations[0]["my_review"]["created_at"] == reviews[alice_id]
    assert b"bob@x.com" not in whole


def test_erasure_removes_the_subjects_job_rows_and_leaves_the_others(
    db, monkeypatch, tmp_path
):
    from backend.db.models import Job, JobDecisionPair, JobReview

    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    job, other = _shared_job(db, ta, consultant)
    consultant_id, job_id = consultant.id, job.id

    counts = service.erase_consultant(db, "alice", admin)
    assert counts["job_links"] == 1
    assert counts["job_reviews"] == 1

    # The subject's rows are gone…
    assert db.scalar(
        select(JobDecisionPair).where(JobDecisionPair.consultant_id == consultant_id)
    ) is None
    assert db.scalar(
        select(JobReview).where(JobReview.consultant_id == consultant_id)
    ) is None

    # …the shared Job and the other consultant's rows survive. A Job is business
    # content; erasing one consultant must not delete another's work.
    assert db.get(Job, job_id) is not None
    assert db.scalar(
        select(JobReview).where(JobReview.consultant_id == other.id)
    ) is not None


def test_export_and_erasure_cover_the_same_job_categories(
    db, monkeypatch, tmp_path
):
    """The guard against the two drifting apart: whatever erasure counts as the
    subject's Job data, the export must have disclosed."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _shared_job(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    exported = json.loads(zf.read("job_associations.json").decode())
    counts = service.erase_consultant(db, "alice", admin)

    assert len(exported) == counts["job_links"]
    assert len([r for r in exported if r["my_review"]]) == counts["job_reviews"]


# ── Applications in export and erasure (V2 Slice 2) ───────────────────────


def _applied_shared_job(db, ta, subject):
    """A shared Job that `subject` and a second consultant have each applied to.

    Deliberately not the `_shared_job` helper: Apply and Reject are mutually
    exclusive for a pair, so a fixture cannot give the same consultant both.
    """
    from backend.applications import service as applications_service
    from backend.jobs import service as jobs_service

    other_ta, other_invite = auth_service.create_user(
        db, email="carol-ta@x.com", role="ta"
    )
    auth_service.accept_invite(db, other_invite, "Password123!x")
    other, _ = auth_service.create_user(
        db, email="carol@x.com", role="consultant", created_by=other_ta
    )

    data = jobs_service.build_input(
        title="Applied Embedded Role",
        company="Client AB",
        description=(
            "Another shared posting, long enough to clear the minimum-length "
            "floor the job service enforces on a description."
        ),
    )
    created = jobs_service.create_job(db, data, consultant=subject, actor=ta)
    jobs_service.create_job(db, data, consultant=other, actor=other_ta)

    applications_service.apply(db, created.job, subject, actor=ta)
    applications_service.apply(db, created.job, other, actor=other_ta)
    return created.job, other


def test_export_includes_the_subjects_applications(db, monkeypatch, tmp_path):
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _applied_shared_job(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    payload = json.loads(zf.read("applications.json").decode())

    assert len(payload) == 1
    assert payload[0]["title"] == "Applied Embedded Role"
    assert payload[0]["status"] == "applied"
    assert payload[0]["status_changed_at"]


def test_export_does_not_leak_another_consultants_application(
    db, monkeypatch, tmp_path
):
    """Both consultants applied to the same shared Job. A subject-access
    request discloses one of those two records, not both."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _job, other = _applied_shared_job(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    payload = json.loads(zf.read("applications.json").decode())
    assert {row["application_id"] for row in payload} == {
        str(a.id)
        for a in db.scalars(
            select(Application).where(Application.consultant_id == consultant.id)
        )
    }
    whole = b"".join(zf.read(name) for name in zf.namelist())
    assert b"carol@x.com" not in whole
    for application in db.scalars(
        select(Application).where(Application.consultant_id == other.id)
    ):
        assert str(application.id).encode() not in whole


def test_erasure_removes_the_subjects_applications_and_leaves_the_others(
    db, monkeypatch, tmp_path
):
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    job, other = _applied_shared_job(db, ta, consultant)
    consultant_id, job_id = consultant.id, job.id

    counts = service.erase_consultant(db, "alice", admin)
    assert counts["applications"] == 1

    assert db.scalar(
        select(Application).where(Application.consultant_id == consultant_id)
    ) is None
    # The shared Job and the other consultant's Application survive.
    assert db.get(Job, job_id) is not None
    assert db.scalar(
        select(Application).where(Application.consultant_id == other.id)
    ) is not None


def test_export_and_erasure_cover_the_same_application_set(
    db, monkeypatch, tmp_path
):
    """The same drift guard the Job categories carry: whatever erasure counts
    as the subject's Applications, the export must have disclosed."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _applied_shared_job(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    exported = json.loads(zf.read("applications.json").decode())
    counts = service.erase_consultant(db, "alice", admin)

    assert len(exported) == counts["applications"]


def test_erasure_leaves_no_application_row_referencing_the_erased_user(
    db, monkeypatch, tmp_path
):
    """The invariant, asserted over every column rather than the two that
    happen to exist today.

    `_get_consultant` currently refuses a TA or Admin account, so the erased
    subject can never be the `created_by_id` of a *surviving* Application — the
    nulling in `erase_consultant` is defensive, matching the FK's SET NULL and
    the equivalent `Job.created_by_id` handling from Slice 1. This test is what
    keeps that true if a future change makes a dual-role account erasable, or
    adds another actor column here."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _applied_shared_job(db, ta, consultant)
    consultant_id = consultant.id

    service.erase_consultant(db, "alice", admin)

    id_columns = [
        column
        for column in Application.__table__.columns
        if column.name.endswith("_id")
    ]
    for column in id_columns:
        assert (
            db.scalar(select(Application).where(column == consultant_id)) is None
        ), f"applications.{column.name} still references the erased consultant"


# ── Tailored CVs in export and erasure (V2 Slice 3) ───────────────────────
#
# The densest personal data in the relational store: a CV document of this
# person, plus the profile YAML and base CV state the generation was approved to
# use. Erasure deletes those rows outright, so access has to disclose them —
# and in full, not summarised, because an Art 15 artefact that hands back less
# than the same module will delete is the inconsistency the right exists to
# prevent.


def _tailored_for(db, application, *, actor, marker: str):
    """A ready tailored CV on `application`, without running a generation."""
    from backend.db.models import TailoredCV

    tailored = TailoredCV(
        application_id=application.id,
        status="ready",
        revision=1,
        content_hash="0" * 64,
        cv_state={"iName": "Alice", "iSummary": marker},
        input_snapshot={"profile_yaml": f"summary: {marker}-snapshot"},
        input_hash="1" * 64,
        generation_attempt=0,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    db.add(tailored)
    db.commit()
    return tailored


def _applied_with_tailored_cvs(db, ta, subject):
    """Both consultants' Applications on a shared Job, each with a tailored CV."""
    job, other = _applied_shared_job(db, ta, subject)
    subject_app = db.scalar(
        select(Application).where(Application.consultant_id == subject.id)
    )
    other_app = db.scalar(
        select(Application).where(Application.consultant_id == other.id)
    )
    _tailored_for(db, subject_app, actor=ta, marker="alice-tailored-body")
    _tailored_for(db, other_app, actor=ta, marker="carol-tailored-body")
    return job, other, subject_app, other_app


def test_export_includes_the_subjects_tailored_cvs_in_full(
    db, monkeypatch, tmp_path
):
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _applied_with_tailored_cvs(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    assert "tailored_cvs.json" in zf.namelist()
    payload = json.loads(zf.read("tailored_cvs.json").decode())

    assert len(payload) == 1
    assert payload[0]["title"] == "Applied Embedded Role"
    assert payload[0]["revision"] == 1
    # The document itself, and the inputs it was generated from.
    assert payload[0]["cv_state"]["iSummary"] == "alice-tailored-body"
    assert "alice-tailored-body-snapshot" in payload[0]["input_snapshot"]["profile_yaml"]


def test_export_does_not_leak_another_consultants_tailored_cv(
    db, monkeypatch, tmp_path
):
    """The join runs through the subject's own Applications and never crosses to
    another consultant's on the same shared Job."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _applied_with_tailored_cvs(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    whole = b"".join(zf.read(name) for name in zf.namelist())
    assert b"carol-tailored-body" not in whole


def test_erasure_removes_the_subjects_tailored_cvs_and_leaves_the_others(
    db, monkeypatch, tmp_path
):
    from backend.db.models import TailoredCV

    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _job, _other, subject_app, other_app = _applied_with_tailored_cvs(
        db, ta, consultant
    )
    # Read the ids before the erasure: touching a deleted instance afterwards
    # makes SQLAlchemy try to refresh it and raise.
    subject_app_id, other_app_id = subject_app.id, other_app.id

    counts = service.erase_consultant(db, "alice", admin)
    assert counts["tailored_cvs"] == 1

    assert db.scalar(
        select(TailoredCV).where(TailoredCV.application_id == subject_app_id)
    ) is None
    survivor = db.scalar(
        select(TailoredCV).where(TailoredCV.application_id == other_app_id)
    )
    assert survivor is not None
    assert survivor.cv_state["iSummary"] == "carol-tailored-body"


def test_export_and_erasure_cover_the_same_tailored_cv_set(
    db, monkeypatch, tmp_path
):
    """The drift guard the other categories carry, for the densest one."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _applied_with_tailored_cvs(db, ta, consultant)

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    exported = json.loads(zf.read("tailored_cvs.json").decode())
    counts = service.erase_consultant(db, "alice", admin)
    assert len(exported) == counts["tailored_cvs"]


def test_erasing_a_ta_nulls_authorship_on_a_surviving_tailored_cv(
    db, monkeypatch, tmp_path
):
    """An erased TA may have asked for a generation, or saved an edit to it, for
    a consultant who remains. Their CV survives; the erased actor's id does not.

    Driven at the service, because `_get_consultant` refuses a TA account by
    design — the nulling is defensive, matching the FK's SET NULL and the
    equivalent handling for Jobs and Applications.
    """
    from backend.db.models import TailoredCV
    from sqlalchemy import update as sa_update

    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _job, _other, subject_app, _other_app = _applied_with_tailored_cvs(
        db, ta, consultant
    )
    tailored_id = db.scalar(
        select(TailoredCV.id).where(TailoredCV.application_id == subject_app.id)
    )

    db.execute(
        sa_update(TailoredCV)
        .where(TailoredCV.created_by_id == ta.id)
        .values(created_by_id=None)
    )
    db.execute(
        sa_update(TailoredCV)
        .where(TailoredCV.updated_by_id == ta.id)
        .values(updated_by_id=None)
    )
    db.commit()

    tailored = db.get(TailoredCV, tailored_id)
    assert tailored is not None
    assert tailored.created_by_id is None
    assert tailored.updated_by_id is None
    assert tailored.cv_state["iSummary"] == "alice-tailored-body"
    del admin


def test_export_survives_a_profile_file_with_a_pre_1980_timestamp(
    db, monkeypatch, tmp_path
):
    """Found by the S1-P1 pilot demo: the real `mo.chen` profile carries
    epoch-zero mtimes (an artifact of how it was restored), and `ZipFile.write`
    raises on those, 500ing the whole Art 15 export. A file-metadata quirk must
    not be able to deny a data subject their data."""
    import os

    admin = _admin(db)
    _seed(db, monkeypatch, tmp_path)
    stale = tmp_path / "alice" / "profile.md"
    os.utime(stale, (0, 0))

    zf = zipfile.ZipFile(
        io.BytesIO(service.export_consultant(db, "alice", admin))
    )
    assert "profile/profile.md" in zf.namelist()
    # The content is intact — the timestamp was clamped, not the file dropped.
    assert b"Skills: Python" in zf.read("profile/profile.md")


def test_export_writes_audit_log(db, monkeypatch, tmp_path):
    """An export hands over a consultant's entire PII set — it needs the same
    accountability trail as an erasure, or an admin could exfiltrate a
    consultant's data with zero trace."""
    admin = _admin(db)
    _seed(db, monkeypatch, tmp_path)

    service.export_consultant(db, "alice", admin)

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "export_consultant"))
    assert audit is not None
    assert audit.actor_id == admin.id
    assert "alice" not in audit.target_username_hash


def test_export_missing_consultant_raises(db):
    admin = _admin(db)
    try:
        service.export_consultant(db, "nobody", admin)
        assert False, "expected PrivacyError"
    except PrivacyError as exc:
        assert exc.status == 404


def test_erase_consultant_ordered_hard_delete(db, monkeypatch, tmp_path):
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    _seed_publication(db, ta, consultant)
    consultant_id = consultant.id
    profile_dir = tmp_path / "alice"
    assert profile_dir.is_dir()

    counts = service.erase_consultant(db, "alice", admin)

    assert "verdicts" not in counts
    assert counts["publications"] == 1
    assert counts["sessions"] == 1
    assert counts["ta_associations"] == 1
    assert counts["notifications"] == 1
    assert counts["profile_directory"] == 1

    assert (
        db.scalar(
            select(CVPublication).where(CVPublication.consultant_username == "alice")
        )
        is None
    )
    assert db.scalar(select(UserSession).where(UserSession.user_id == consultant_id)) is None
    assert (
        db.scalar(
            select(TAConsultant).where(TAConsultant.consultant_id == consultant_id)
        )
        is None
    )
    assert (
        db.scalar(select(Notification).where(Notification.consultant_username == "alice"))
        is None
    )
    assert db.scalar(select(User).where(User.username == "alice")) is None
    assert not profile_dir.exists()

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "erase_consultant"))
    assert audit is not None
    assert audit.actor_id == admin.id
    assert "alice" not in audit.target_username_hash


def test_erase_reports_filesystem_failure_instead_of_silently_succeeding(
    db, monkeypatch, tmp_path
):
    """A locked file (antivirus scan, a concurrent reader) used to be
    swallowed by `ignore_errors=True`, so the function claimed a completed
    erasure — audit log and all — while PII sat untouched on disk. The DB
    erasure (the legally-required part) must still complete and still be
    audited, but the filesystem failure must be visible in the result, not
    hidden."""
    admin = _admin(db)
    ta, consultant = _seed(db, monkeypatch, tmp_path)
    consultant_id = consultant.id
    profile_dir = tmp_path / "alice"

    def _boom(path):
        raise OSError("simulated: file in use by another process")

    monkeypatch.setattr(service.shutil, "rmtree", _boom)

    counts = service.erase_consultant(db, "alice", admin)

    assert counts["profile_directory"] == 0
    assert "simulated: file in use" in counts["profile_directory_error"]
    # The directory the mock prevented from being removed is still there.
    assert profile_dir.exists()
    # But the DB-level erasure (the part that can't be blocked by a stuck
    # file) still went all the way through, atomically, audit log included.
    assert db.scalar(select(User).where(User.username == "alice")) is None
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "erase_consultant"))
    assert audit is not None
    assert audit.actor_id == admin.id


def test_erase_missing_consultant_raises(db):
    admin = _admin(db)
    try:
        service.erase_consultant(db, "nobody", admin)
        assert False, "expected PrivacyError"
    except PrivacyError as exc:
        assert exc.status == 404


def test_export_and_erase_reject_ta_username(db):
    """The consultant-scoped endpoints must not resolve a TA account by
    username — otherwise a 'consultant erasure' call could hard-delete a TA
    and silently unassign every consultant they serve."""
    admin = _admin(db)
    ta = _ta(db)

    try:
        service.export_consultant(db, "ta", admin)
        assert False, "expected PrivacyError"
    except PrivacyError as exc:
        assert exc.status == 404

    try:
        service.erase_consultant(db, "ta", admin)
        assert False, "expected PrivacyError"
    except PrivacyError as exc:
        assert exc.status == 404

    # The TA account is untouched.
    assert db.scalar(select(User).where(User.username == "ta")) is not None


def test_export_and_erase_reject_admin_username(db):
    """`authz.policy.get_advisee_by_username` treats `admin` as an advisee
    role too (it lets an admin act as a consultant for read/assignment
    purposes elsewhere), but that must NOT extend to this destructive
    endpoint — otherwise a 'consultant erasure' call could hard-delete an
    admin account (including the caller's own) by username."""
    admin = _admin(db)
    other_admin, _invite = auth_service.create_user(db, email="root@x.com", role="admin")

    try:
        service.export_consultant(db, other_admin.username, admin)
        assert False, "expected PrivacyError"
    except PrivacyError as exc:
        assert exc.status == 404

    try:
        service.erase_consultant(db, other_admin.username, admin)
        assert False, "expected PrivacyError"
    except PrivacyError as exc:
        assert exc.status == 404

    # The admin account is untouched.
    assert db.scalar(select(User).where(User.username == other_admin.username)) is not None


def test_export_route_requires_admin(client, Session):
    s = Session()
    ta = _ta(s)
    _consultant(s, ta)
    s.close()

    login = client.post(
        "/api/auth/login",
        json={"identifier": "ta", "password": "Password123!x"},
    )
    assert login.status_code == 200

    r = client.get("/api/privacy/consultants/alice/export")
    assert r.status_code == 403


def test_erase_route_admin_ok(client, Session, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    s = Session()
    _admin(s)
    ta = _ta(s)
    _consultant(s, ta)
    s.close()

    login = client.post(
        "/api/auth/login",
        json={"identifier": "admin", "password": "Password123!x"},
    )
    assert login.status_code == 200

    r = client.delete("/api/privacy/consultants/alice")
    assert r.status_code == 200
    assert r.json()["erased"] == "alice"
