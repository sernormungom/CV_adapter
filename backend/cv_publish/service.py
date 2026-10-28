"""Published-CV store, lifecycle, and view tracking (ADR-009, D-013, P-010).

`publish_for_application()` upserts the one active publication for an
Application: an existing non-revoked row keeps its identity but gets a fresh
`cv_state` snapshot (sanitized, derived from the Application's tailored CV —
never client-supplied), a rotated token, and a reset expiry; otherwise a new
row (and token) is minted. **Every publish call mints a fresh raw token and
stores only its hash** (`token_hash`, via `backend.auth.security.generate_token`/
`hash_token`) — the same primitive `UserSession`/`Invite` already
use — so Regenerate/Republish always invalidates whatever link was shown
before (P-010). The public view route calls `record_view`, which bumps the
counter and emits a daily-deduped `cv_viewed` notification to the consultant's
assigned TA (falling back to the publisher). Neither the viewer's IP nor
user-agent is stored (data minimisation).

`disable_for_application()` is the other write path into `revoked`: called by
`backend.applications.service.transition` in the same transaction as a
confirmed terminal Application status, so "a terminal Application action
disables the link" is atomic with the status change, not a follow-up request
that could be lost. Expiry alone never deletes anything — only a `revoked` row
is ever hard-deleted, by `purge_revoked`.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.applications.service import ApplicationServiceError, lock_application
from backend.auth.security import generate_token, hash_token
from backend.cv_publish.sanitize import sanitize_cv_state
from backend.db.models import Application, CVPublication, Job, TailoredCV, User
from backend.notifications import service as notify_service
from backend.authz import policy as authz

logger = logging.getLogger(__name__)

# Photo file extensions the profile store may hold (mirror of main.py's set).
_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class CVPublishError(RuntimeError):
    """A caller-fixable problem with a publish request."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


#: Every service error a route in this domain can see. `publish_for_application`
#: reuses the Application service's row lock (same composition
#: `backend/tailored_cv/service.py` makes), so its error type reaches the
#: caller too.
SERVICE_ERRORS = (CVPublishError, ApplicationServiceError)

TERMINAL_APPLICATION_MESSAGE = (
    "This application has ended, so its tailored CV can no longer be "
    "published. A previously published link is disabled at the same time "
    "the application ends."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _delete_photo(photo_path: str | None) -> None:
    """Best-effort removal of a publication-owned photo file."""
    if not photo_path:
        return
    try:
        os.remove(photo_path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001 - a stuck file must not break lifecycle ops
        logger.warning("cv_publish: could not delete photo %s: %s", photo_path, exc)


def _consultant_photo_file(consultant_username: str):
    """The consultant's current profile photo path, or None if none on disk."""
    profile_dir = config.DATA_DIR / consultant_username
    for ext in _PHOTO_EXTS:
        candidate = profile_dir / f"photo{ext}"
        if candidate.exists():
            return candidate
    return None


def _snapshot_photo(
    consultant_username: str, pub_id: uuid.UUID, cv_state: dict
) -> tuple[dict, str | None]:
    """Freeze the CV's photo into a publication-owned copy, keyed by `pub_id`.

    A publication must render the photo the consultant approved, independent of
    later profile-photo changes, and it must be servable to an anonymous viewer
    without the authenticated `/api/profiles/{u}/photo` route. Keyed by the
    publication's own id (never by the capability token, which must not appear
    in a URL or filename): on publish we copy the current profile photo to an
    id-keyed file and rewrite the snapshot's `photoSrc` to the capability photo
    route — the route itself authorizes by presenting the raw token, not by the
    filename or path.

    No-op when the CV carries no photo (`cv_state.photoSrc` falsy). If the CV
    references a photo but none is on disk, the reference is dropped rather than
    left pointing at a broken/auth-walled URL.
    """
    if not cv_state.get("photoSrc"):
        return cv_state, None
    src = _consultant_photo_file(consultant_username)
    if src is None:
        return {**cv_state, "photoSrc": None}, None
    dest_dir = config.CV_PUBLISH_PHOTO_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Clear any prior copy for this publication (a republish may change extension).
    for old in dest_dir.glob(f"{pub_id}.*"):
        old.unlink()
    dest = dest_dir / f"{pub_id}{src.suffix.lower()}"
    shutil.copyfile(src, dest)
    return {**cv_state, "photoSrc": "/api/publications/photo"}, str(dest)


def get_active_for_application(
    db: Session, application_id: uuid.UUID
) -> CVPublication | None:
    """The single non-revoked publication for this Application, if any."""
    return db.scalar(
        select(CVPublication).where(
            CVPublication.application_id == application_id,
            CVPublication.revoked.is_(False),
        )
    )


def publish_for_application(
    db: Session, *, application: Application, actor: User
) -> tuple[CVPublication, str]:
    """Publish (or republish) `application`'s tailored CV. Returns `(pub, raw_token)`.

    The snapshot is always derived from the tailored CV's *current* stored
    state — never accepted from the caller — and sanitized before storage.
    Every call, first publish or republish alike, mints a fresh token: there is
    no "keeps its link" path, because Regenerate must invalidate whatever link
    was shown before (P-010).

    **Takes the Application row lock before reading anything else** (S5-P2) —
    the same lock `backend.applications.service.transition` takes to disable a
    publication and delete the tailored CV on a confirmed terminal status —
    and holds it through this function's own commit. Without this, Publish
    could read the tailored CV before a concurrent terminal transition deletes
    it, then commit a brand-new publication after that transition's cleanup
    already ran, leaving a live hiring-manager link for a closed Application
    (`S5-P1-R2` finding 2). With the shared lock, the two orderings are the
    only two possible outcomes: Publish commits first and the terminal
    transition's `disable_for_application` (looked up fresh, under its own
    hold of this same lock) then revokes it; or the terminal transition
    commits first and this call observes `is_terminal` and refuses before it
    ever reads the tailored CV. Follows the documented lock order (Job → pair
    → Application → tailored CV row), so this cannot deadlock against
    `backend/jobs/` or `backend/applications/`.
    """
    application = lock_application(db, application)
    if application.is_terminal:
        raise CVPublishError(TERMINAL_APPLICATION_MESSAGE, status=409)

    tailored_cv = db.scalar(
        select(TailoredCV).where(TailoredCV.application_id == application.id)
    )
    if tailored_cv is None or not tailored_cv.has_content:
        raise CVPublishError(
            "This application has no generated CV to publish yet.", status=400
        )
    job = db.get(Job, application.job_id)
    consultant = db.get(User, application.consultant_id)
    if job is None or consultant is None:
        raise CVPublishError("That application's job or consultant is gone.", status=404)

    cv_state = sanitize_cv_state(tailored_cv.cv_state or {})
    title = " — ".join(filter(None, [job.company, job.title])) or None
    expires = _now() + timedelta(days=config.CV_PUBLISH_TTL_DAYS)

    pub = get_active_for_application(db, application.id)
    raw_token = generate_token()
    token_hash = hash_token(raw_token)

    if pub is not None:
        cv_state, photo_path = _snapshot_photo(consultant.username, pub.id, cv_state)
        if pub.photo_path and pub.photo_path != photo_path:
            _delete_photo(pub.photo_path)
        pub.token_hash = token_hash
        pub.cv_state = cv_state
        pub.title = title
        pub.photo_path = photo_path
        pub.expires_at = expires
        db.commit()
        db.refresh(pub)
        return pub, raw_token

    new_id = uuid.uuid4()
    cv_state, photo_path = _snapshot_photo(consultant.username, new_id, cv_state)
    pub = CVPublication(
        id=new_id,
        token_hash=token_hash,
        application_id=application.id,
        consultant_username=consultant.username,
        job_id=str(job.id),
        title=title,
        cv_state=cv_state,
        photo_path=photo_path,
        created_by_id=actor.id,
        expires_at=expires,
        view_count=0,
    )
    db.add(pub)
    db.commit()
    db.refresh(pub)
    return pub, raw_token


def get_by_id(db: Session, pub_id: str) -> CVPublication | None:
    try:
        key = uuid.UUID(str(pub_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.get(CVPublication, key)


def get_by_token(db: Session, raw_token: str) -> CVPublication | None:
    """Look a publication up by its raw capability token (hashed for lookup).

    The only place a raw token is ever compared — never stored, so a matching
    row proves possession, not equality against a persisted secret.
    """
    return db.scalar(
        select(CVPublication).where(CVPublication.token_hash == hash_token(raw_token))
    )


def is_serveable(pub: CVPublication) -> bool:
    if pub.revoked:
        return False
    expires = pub.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires is None or expires > _now()


def list_for_usernames(
    db: Session, usernames: set[str]
) -> list[CVPublication]:
    if not usernames:
        return []
    return list(
        db.scalars(
            select(CVPublication)
            .where(CVPublication.consultant_username.in_(usernames))
            .order_by(CVPublication.created_at.desc())
        )
    )


def list_all(db: Session) -> list[CVPublication]:
    return list(
        db.scalars(
            select(CVPublication).order_by(CVPublication.created_at.desc())
        )
    )


def extend(db: Session, pub: CVPublication) -> CVPublication:
    """Reset the expiry clock to a full TTL from now."""
    pub.expires_at = _now() + timedelta(days=config.CV_PUBLISH_TTL_DAYS)
    db.commit()
    db.refresh(pub)
    return pub


def revoke(db: Session, pub: CVPublication) -> None:
    pub.revoked = True
    db.commit()


def disable_for_application(db: Session, application_id: uuid.UUID) -> None:
    """Revoke the active publication for `application_id`, if any. No commit.

    Called from `backend.applications.service.transition` inside the same
    transaction as a confirmed terminal status change, so the disable and the
    status move land atomically together or not at all.
    """
    pub = get_active_for_application(db, application_id)
    if pub is not None:
        pub.revoked = True


def purge_revoked(db: Session) -> dict:
    """Hard-delete every revoked publication, incl. its photo file.

    Expiry alone never deletes anything (`is_serveable` already blocks
    serving an expired-but-not-revoked row); only an explicit Revoke or a
    confirmed terminal Application transition (`disable_for_application`)
    marks a row `revoked`, and this purge is what eventually reclaims that
    row's snapshot/photo storage.
    """
    rows = db.scalars(select(CVPublication).where(CVPublication.revoked.is_(True))).all()
    purged = 0
    for pub in rows:
        _delete_photo(pub.photo_path)
        db.delete(pub)
        purged += 1
    db.commit()
    return {"purged": purged}


def record_view(db: Session, pub: CVPublication) -> None:
    """Count the open and notify the consultant's TA (deduped per day).

    No viewer IP or user-agent is stored (ADR-009 data minimisation). The
    notification target is the consultant's assigned TA; if none is assigned it
    falls back to whoever published the CV. The notification payload and dedupe
    key use this row's non-secret `id` — never the capability token.
    """
    now = _now()
    pub.view_count = (pub.view_count or 0) + 1
    pub.last_viewed_at = now
    db.commit()

    target_id = authz.ta_id_for_consultant_username(db, pub.consultant_username)
    if target_id is None:
        target_id = pub.created_by_id
    if target_id is None:
        # No one to notify (unassigned consultant, publisher since deleted).
        return

    who = pub.consultant_username
    label = pub.title or (pub.job_id or "the CV")
    target = db.get(User, target_id)
    notify_service.notify(
        db,
        user_id=target_id,
        type=notify_service.CV_VIEWED,
        title=f"Hiring manager opened {who}'s CV — {label}",
        body=f"View #{pub.view_count} of {who}'s CV for {label}.",
        consultant_username=who,
        job_id=pub.job_id,
        payload={"publication_id": str(pub.id), "view_count": pub.view_count},
        # One notification per publication per day; count still increments on
        # every recorded open.
        dedupe_key=f"cv_viewed:{pub.id}:{now.date().isoformat()}",
        email_to=target.email if target else None,
    )
