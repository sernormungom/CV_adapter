"""Data-subject rights: export (Art 15/20) and erasure (Art 17) for a consultant.

Both operate on the same "what is a consultant's data" definition fixed in
docs/legal/decisions.md §1.1/§3: the `users` row (minus `password_hash`),
the whole `data/profiles/<username>/` tree (incl. uploads),
CV-publication metadata, TA↔consultant associations, the consultant's relational
Job links and their own review markers (V2 Slice 1), their Applications
(V2 Slice 2), and each Application's tailored CV (V2 Slice 3). The `verdicts`
table this used to include was dropped in V3 packet S2 — the verdict-fed
learning path it backed has no live writer or reader left
(`docs/architecture/areas/shared-and-legacy.md` G4, `docs/plans/v3/decisions.md`
V3-P-008 decision 5).

**Export and erasure must cover the same set.** If erasure deletes something,
access has to disclose it; the two functions are read together deliberately.
Neither discloses or deletes another consultant's link or review on a shared
Job — a shared Job's *facts* are common, its *relationships* are not.

Erasure order matters: publications → sessions/invites →
TA associations → notifications → CV imports → tailored CVs → Applications →
Job links and review markers → profile directory → `users` row. Tailored CVs
go before the Applications they hang off, so the deletion never depends on a
cascade having fired first. CV imports (V2 Slice 4) are deleted alongside them,
before the profile directory: the row itself carries no CV content (only a
filename, counts, and a reference to a transient upload that is deleted
promptly on the ordinary path, or occasionally left as a small residual file
otherwise — `cv_importer/service.py`'s module docstring, `S4-P1-R7`), but it
does key by `consultant_username` with no FK to cascade on, like
`CVPublication`/`Notification` below. Either way, erasure's
`shutil.rmtree(profile_dir)` below removes any lingering transient upload
directly, without needing the row's reference at all. The export walk below
excludes that same transient directory outright: a raw upload should never
appear in what this function calls "this person's data." A shared relational Job is business content and is
**not** deleted; this consultant's link to it and their own review markers are,
and authorship back-references on what survives are nulled. The `users`
row is deleted last so every FK-referencing table is already empty by the
time it goes — not relying on `ON DELETE CASCADE` timing/parity across
Postgres and the SQLite test DB. The `users`-row delete and the `AuditLog`
write share one final `db.commit()`, so a successful erasure can never exist
without its accountability record, or vice versa. `export_consultant` writes
its own `AuditLog` row too (an export hands over the same PII set an erasure
would delete, and needs the same "who accessed this" trail).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import uuid
import zipfile

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from backend.config import DATA_DIR
from backend.cv_importer.keys import PROFILE_LOCK_FILENAME, SCRATCH_DIRNAME
from backend.cv_state import service as cv_state_service
from backend.db.models import (
    Application,
    AuditLog,
    CVImport,
    CVPublication,
    Invite,
    Job,
    JobDecisionPair,
    JobConsultantMatch,
    JobReview,
    Notification,
    TAConsultant,
    TailoredCV,
    User,
    UserRole,
    UserRoleAssignment,
    UserSession,
)

logger = logging.getLogger(__name__)


def _username_hash(username: str) -> str:
    """Truncated SHA-256, not reversible to the original username."""
    return hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]


def _delete_publication_photo(photo_path: str | None) -> None:
    """Best-effort removal of a publication-owned photo file during erasure."""
    if not photo_path:
        return
    try:
        os.remove(photo_path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001
        logger.warning(
            "erase_consultant: could not delete publication photo: %s", exc
        )


class PrivacyError(RuntimeError):
    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


def _get_consultant(db: Session, username: str) -> User:
    """Resolve `username` to a consultant — and only a consultant.

    Erasure is destructive and hard-deletes the `users` row itself, so this
    resolver is deliberately strict: the account must hold the consultant role
    and **must not** hold admin or TA. That keeps an erasure request from ever
    targeting a privileged account (including the caller's own) even though
    roles are now a many-to-many membership and an Admin+Consultant is a real,
    supported combination (docs/DECISIONS.md P-004). Erasing such a dual-role
    account is an offboarding decision that has to remove the privileged role
    first — matching the "consultant data" definition this module operates on
    (docs/legal/decisions.md §1.1/§3).
    """
    user = db.scalar(
        select(User)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(
            User.username == username,
            UserRoleAssignment.role == UserRole.consultant.value,
        )
    )
    if user is None:
        raise PrivacyError(f"No consultant found for username '{username}'")
    privileged = sorted(
        user.roles & {UserRole.admin.value, UserRole.ta.value}
    )
    if privileged:
        raise PrivacyError(
            f"'{username}' also holds the {', '.join(privileged)} role; "
            "remove it before erasing this account",
            status=409,
        )
    return user


def _user_export_dict(user: User) -> dict:
    """The `users` row, minus `password_hash` (never exported)."""
    return {
        "id": str(user.id),
        "email": user.email,
        "username": user.username,
        "full_name": user.full_name,
        "roles": sorted(user.roles),
        "city": user.city,
        "is_active": user.is_active,
        "actively_looking": user.actively_looking,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def _ta_associations(db: Session, consultant_id: uuid.UUID) -> list[dict]:
    tas = db.scalars(
        select(User)
        .join(TAConsultant, TAConsultant.ta_id == User.id)
        .where(TAConsultant.consultant_id == consultant_id)
        .order_by(User.username)
    )
    return [{"username": ta.username, "email": ta.email} for ta in tas]


def _job_associations(db: Session, consultant_id: uuid.UUID) -> list[dict]:
    """This Consultant's Job links and their own review markers (Slice 1).

    Access and erasure must agree on what counts as the subject's data. Erasure
    deletes exactly these two things — the link, and the review (a decision
    and its timestamp, nothing else) — so an Art 15/20 artefact that omitted
    them would hand over less than the same module is prepared to delete, which
    is precisely the inconsistency Art 15 is meant to prevent.

    Included per row: enough of the shared Job to make the association
    intelligible (its title, company, and the reference id), and the subject's
    own decision. **Excluded:** which *other* Consultants are linked
    to the same Job and what they wrote — that is their data, not this
    subject's, and a subject-access request is not a route to it. The full job
    description is also left out: it is the client's posting text, already
    public, and reproducing it would bulk out the artefact without telling the
    subject anything about themselves.
    """
    rows = db.execute(
        select(Job, JobReview)
        .join(JobDecisionPair, JobDecisionPair.job_id == Job.id)
        .outerjoin(
            JobReview,
            (JobReview.job_id == Job.id)
            & (JobReview.consultant_id == consultant_id),
        )
        .where(JobDecisionPair.consultant_id == consultant_id)
        .order_by(Job.created_at)
    ).all()

    out: list[dict] = []
    for job, review in rows:
        out.append(
            {
                "job_id": str(job.id),
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "status": job.status,
                "linked": True,
                "my_review": (
                    {
                        "decision": review.decision,
                        "created_at": (
                            review.created_at.isoformat()
                            if review.created_at
                            else None
                        ),
                    }
                    if review is not None
                    else None
                ),
            }
        )
    return out


def _applications(db: Session, consultant_id: uuid.UUID) -> list[dict]:
    """This Consultant's Applications (Slice 2), including terminal ones.

    A separate artefact rather than a nested field on `job_associations.json`,
    on purpose: that file is built by joining through `job_decision_pairs`, so
    nesting Applications inside it would make their disclosure depend on a
    different row still existing. An Application is the subject's own record of
    having been put forward for a position — the thing they are most likely to
    actually ask about — and it is disclosed on its own terms.

    Included: the status and its history-of-one (when it last moved), plus the
    same minimal Job reference `job_associations.json` uses. **Excluded:** any
    other Consultant's Application on the same shared Job.
    """
    rows = db.execute(
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Application.consultant_id == consultant_id)
        .order_by(Application.created_at)
    ).all()
    return [
        {
            "application_id": str(application.id),
            "job_id": str(job.id),
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "status": application.status,
            "created_at": (
                application.created_at.isoformat()
                if application.created_at
                else None
            ),
            "status_changed_at": (
                application.status_changed_at.isoformat()
                if application.status_changed_at
                else None
            ),
        }
        for application, job in rows
    ]


def _tailored_cvs(db: Session, consultant_id: uuid.UUID) -> list[dict]:
    """This Consultant's Application-tailored CVs (Slice 3), content included.

    Erasure deletes these rows outright, so access has to disclose them — and
    what it deletes is not metadata: `cv_state` is a CV of this person, and
    `input_snapshot` holds the profile YAML and base CV state the generation was
    approved to use. Both are exported in full rather than summarised, because
    an Art 15 artefact that hands back less than the same module will delete is
    the exact inconsistency the right of access exists to prevent.

    The snapshot's `profile_yaml` duplicates `profile/profile.md`, already in
    the archive. That duplication is deliberate: it is what was actually used
    for this document, at that moment, and telling the subject "look in the
    other file" would hide a divergence rather than reveal one.

    **Excluded:** any other Consultant's tailored CV on the same shared Job —
    reachable only through *their* Application, which this join never crosses.
    """
    rows = db.execute(
        select(TailoredCV, Application, Job)
        .join(Application, Application.id == TailoredCV.application_id)
        .join(Job, Job.id == Application.job_id)
        .where(Application.consultant_id == consultant_id)
        .order_by(TailoredCV.created_at)
    ).all()
    return [
        {
            **tailored.to_dict(),
            "job_id": str(job.id),
            "title": job.title,
            "company": job.company,
            "cv_state": tailored.cv_state,
            "input_snapshot": tailored.input_snapshot,
        }
        for tailored, application, job in rows
    ]


def _cv_imports(db: Session, username: str) -> list[dict]:
    """This Consultant's durable CV-import history (V2 Slice 4).

    Metadata and non-content counts only, per `CVImport.to_dict()` — the row
    itself never holds profile content (`backend/db/models/cv_import.py`), so
    unlike `_tailored_cvs` there is nothing extra to add here for consistency
    with erasure: erasure deletes the row, and the row's own `to_dict()` is
    already everything erasure removes.
    """
    rows = db.scalars(
        select(CVImport)
        .where(CVImport.consultant_username == username)
        .order_by(CVImport.created_at)
    )
    return [row.to_dict() for row in rows]


#: The ZIP format cannot represent a timestamp before this; `zipfile` raises
#: rather than clamping. Files restored from an archive or copied across some
#: filesystems land with an epoch-zero mtime, which is exactly what the pilot's
#: restored `mo.chen` profile has.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _add_profile_file(zf: zipfile.ZipFile, path, profile_dir) -> None:
    """Add one profile file, tolerating a pre-1980 mtime.

    `ZipFile.write` derives the entry timestamp from the file's mtime and
    raises `ValueError: ZIP does not support timestamps before 1980` on an
    epoch-zero one — which failed the whole Art 15 export with a 500 for a
    consultant whose profile happened to carry such a file. A metadata quirk of
    how a file was restored must not be able to deny a data subject their data,
    so the timestamp is clamped and the content still ships.
    """
    arcname = f"profile/{path.relative_to(profile_dir)}"
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
    info.compress_type = zf.compression
    try:
        info = zipfile.ZipInfo.from_file(path, arcname)
        info.compress_type = zf.compression
    except ValueError:
        pass          # keep the clamped-timestamp ZipInfo built above
    zf.writestr(info, path.read_bytes())


def export_consultant(db: Session, username: str, actor: User) -> bytes:
    """Build a ZIP (Art 15 access + Art 20 portability, one artefact).

    Writes an `AuditLog` row the same way `erase_consultant` does — an export
    hands over a consultant's entire PII set, so it needs the same "who
    accessed this, when" accountability trail as an erasure. Note the
    ordering below: the audit row is written and committed only after the ZIP
    has been fully built in memory, so an export that fails partway through
    (e.g. an unreadable file under `profile_dir`) never logs a phantom
    export that didn't actually happen.
    """
    user = _get_consultant(db, username)

    publications = [
        pub.to_dict()
        for pub in db.scalars(
            select(CVPublication).where(
                CVPublication.consultant_username == username
            )
        )
    ]
    ta_associations = _ta_associations(db, user.id)
    job_associations = _job_associations(db, user.id)
    applications = _applications(db, user.id)
    tailored_cvs = _tailored_cvs(db, user.id)
    cv_imports = _cv_imports(db, username)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("user.json", json.dumps(_user_export_dict(user), indent=2))
        zf.writestr(
            "publications.json", json.dumps(publications, indent=2, default=str)
        )
        zf.writestr(
            "ta_associations.json", json.dumps(ta_associations, indent=2)
        )
        zf.writestr(
            "job_associations.json", json.dumps(job_associations, indent=2)
        )
        zf.writestr("matching_scores.json", json.dumps([r.result for r in db.scalars(select(JobConsultantMatch).where(JobConsultantMatch.consultant_id == user.id))], indent=2))
        zf.writestr("applications.json", json.dumps(applications, indent=2))
        zf.writestr(
            "tailored_cvs.json", json.dumps(tailored_cvs, indent=2, default=str)
        )
        zf.writestr(
            "cv_imports.json", json.dumps(cv_imports, indent=2, default=str)
        )

        profile_dir = DATA_DIR / username
        scratch_dir = profile_dir / SCRATCH_DIRNAME
        if profile_dir.is_dir():
            for path in profile_dir.rglob("*"):
                if not path.is_file():
                    continue
                if path.name in (cv_state_service.LOCK_FILENAME, PROFILE_LOCK_FILENAME):
                    # The base-CV and profile-import compare-and-write locks.
                    # Empty coordination files, not this person's data — an
                    # Art 15 export should be what we hold about them, not our
                    # implementation's scaffolding.
                    continue
                if scratch_dir in path.parents:
                    # `.cv_import_tmp/` is transient processing state, not
                    # retained data — the handler deletes anything here on
                    # every path, ordinarily within seconds. An export must
                    # not bundle a leftover raw CV that happened to still be
                    # there when this ran (a rare delete failure, `S4-P1-R7`);
                    # the retained facts are `profile.md`, exported via that
                    # file directly.
                    continue
                try:
                    _add_profile_file(zf, path, profile_dir)
                except OSError:
                    # A concurrent erase/retention-purge removed this file
                    # between enumeration and read — skip it rather than
                    # 500ing the whole export over one vanished file.
                    continue

    zip_bytes = buf.getvalue()

    db.add(
        AuditLog(
            actor_id=actor.id,
            action="export_consultant",
            target_username_hash=_username_hash(username),
        )
    )
    db.commit()

    return zip_bytes


def erase_consultant(
    db: Session, username: str, actor: User
) -> dict:
    """Ordered hard delete. Returns a count of rows/artefacts removed per store."""
    user = _get_consultant(db, username)
    db.refresh(user, with_for_update=True)

    # Publications first delete their photo files (data on disk), then the rows.
    publications = list(
        db.scalars(
            select(CVPublication).where(
                CVPublication.consultant_username == username
            )
        )
    )
    for pub in publications:
        _delete_publication_photo(pub.photo_path)

    counts = {
        "publications": db.execute(
            delete(CVPublication).where(
                CVPublication.consultant_username == username
            )
        ).rowcount,
        "sessions": db.execute(
            delete(UserSession).where(UserSession.user_id == user.id)
        ).rowcount,
        "invites": db.execute(
            delete(Invite).where(Invite.user_id == user.id)
        ).rowcount,
        # Role memberships. The FK cascades on Postgres, but delete them
        # explicitly so the erasure receipt counts them and so the behaviour
        # does not depend on FK enforcement being on (SQLite in the fast gate).
        "role_memberships": db.execute(
            delete(UserRoleAssignment).where(
                UserRoleAssignment.user_id == user.id
            )
        ).rowcount,
        "ta_associations": db.execute(
            delete(TAConsultant).where(
                or_(
                    TAConsultant.ta_id == user.id,
                    TAConsultant.consultant_id == user.id,
                )
            )
        ).rowcount,
        # `Notification.consultant_username` is a plain string, no FK (like
        # CVPublication) — survives DB-level cascade, so delete it
        # explicitly. Otherwise a TA's notification inbox (e.g. a cv_viewed
        # alert) keeps naming the erased consultant forever.
        "notifications": db.execute(
            delete(Notification).where(
                Notification.consultant_username == username
            )
        ).rowcount,
        # CV imports (Slice 4). No content in the row itself — see
        # `_cv_imports` above — but it keys by `consultant_username` with no
        # FK, like `CVPublication`/`Notification`, so it needs the same
        # explicit delete rather than relying on a cascade that does not exist.
        "cv_imports": db.execute(
            delete(CVImport).where(CVImport.consultant_username == username)
        ).rowcount,
        # Tailored CVs (Slice 3). CV documents of this person plus the frozen
        # profile/base-CV inputs they were generated from — the densest PII in
        # the relational store. Deleted explicitly and *before* the Applications
        # they hang off: the FK cascade is real on Postgres and absent on the
        # fast gate's SQLite, so relying on it would leave these rows behind in
        # one of the two gates and nowhere in the receipt.
        "tailored_cvs": db.execute(
            delete(TailoredCV).where(
                TailoredCV.application_id.in_(
                    select(Application.id).where(
                        Application.consultant_id == user.id
                    )
                )
            )
        ).rowcount,
        # Applications (Slice 2). The record that this person was put forward
        # for a named position — squarely their personal data, and deleted
        # before the Job links below so the erasure reads in the same order the
        # module docstring states.
        "applications": db.execute(
            delete(Application).where(Application.consultant_id == user.id)
        ).rowcount,
        # Relational Job data (Slice 1). A shared Job is business content and
        # survives, but this consultant's link to it and their own review
        # markers do not. Deleted explicitly for the same reason as the rows above: the FK
        # cascade is real on Postgres and absent on the fast gate's SQLite.
        "matching_scores": db.execute(delete(JobConsultantMatch).where(JobConsultantMatch.consultant_id == user.id)).rowcount,
        "job_reviews": db.execute(
            delete(JobReview).where(JobReview.consultant_id == user.id)
        ).rowcount,
        "job_links": db.execute(
            delete(JobDecisionPair).where(JobDecisionPair.consultant_id == user.id)
        ).rowcount,
    }

    # Authorship back-references on records that survive. `ON DELETE SET NULL`
    # would do this on Postgres; doing it here keeps the erased user's id from
    # lingering in the fast gate's SQLite too.
    db.execute(
        update(Job).where(Job.created_by_id == user.id).values(created_by_id=None)
    )
    db.execute(
        update(JobReview)
        .where(JobReview.created_by_id == user.id)
        .values(created_by_id=None)
    )
    # An erased TA may have applied, or moved a stage, on behalf of a consultant
    # who remains. Their Application survives; the erased actor's id does not.
    db.execute(
        update(Application)
        .where(Application.created_by_id == user.id)
        .values(created_by_id=None)
    )
    db.execute(
        update(Application)
        .where(Application.status_changed_by_id == user.id)
        .values(status_changed_by_id=None)
    )
    # The same, for a tailored CV another Consultant still owns: an erased TA
    # may have asked for its generation or saved an edit to it.
    db.execute(
        update(TailoredCV)
        .where(TailoredCV.created_by_id == user.id)
        .values(created_by_id=None)
    )
    db.execute(
        update(TailoredCV)
        .where(TailoredCV.updated_by_id == user.id)
        .values(updated_by_id=None)
    )
    # The same, for a CV import another surviving consultant's TA requested.
    db.execute(
        update(CVImport)
        .where(CVImport.created_by_id == user.id)
        .values(created_by_id=None)
    )

    # Filesystem delete happens here — before the `users` row — matching the
    # documented order above. Deliberately NOT `ignore_errors=True`: a locked
    # file (antivirus scan, a concurrent export reading the same tree) used
    # to be swallowed silently, leaving PII on disk while the function still
    # reported and audited a completed erasure. We still proceed with the DB
    # erasure below even on a filesystem failure (Art 17 compliance for the
    # identity/records the data subject actually asked about shouldn't hinge
    # on one stuck file), but the failure is now recorded truthfully in the
    # returned counts, logged, and never silently claimed as success.
    profile_dir = DATA_DIR / username
    profile_existed = profile_dir.is_dir()
    profile_error: str | None = None
    if profile_existed:
        try:
            shutil.rmtree(profile_dir)
        except OSError as exc:
            profile_error = str(exc)
            logger.error(
                "erase_consultant: failed to remove profile directory for %s: %s",
                _username_hash(username),
                exc,
            )
    counts["profile_directory"] = 1 if profile_existed and profile_error is None else 0
    if profile_error is not None:
        counts["profile_directory_error"] = profile_error

    # The role rows were already deleted in bulk above (so the receipt can
    # count them). Expire the loaded collection first, or the `delete-orphan`
    # cascade on `User.role_assignments` re-issues a DELETE for rows that are
    # already gone and SQLAlchemy warns about the 0-row result.
    db.expire(user, ["role_assignments"])

    # `users` row + audit log commit together, atomically, so a successful
    # erasure can never exist without its accountability record (and vice
    # versa) — no window where one commit lands and the other doesn't.
    db.delete(user)
    db.add(
        AuditLog(
            actor_id=actor.id,
            action="erase_consultant",
            target_username_hash=_username_hash(username),
        )
    )
    db.commit()

    return counts
