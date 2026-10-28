"""Job state changes. Every write to `jobs`/`job_decision_pairs`/`job_reviews`.

Three things here are load-bearing and easy to get subtly wrong:

**Duplicate blocking is a database constraint, not a lookup.** `create_job`
inserts and lets the unique index on `jobs.content_hash` decide. A
`SELECT ... then INSERT` would let two concurrent identical submissions both
find nothing and both insert; the integration gate proves the real Postgres
race. The IntegrityError path is the *expected* path for a duplicate, not an
error path, so it re-reads the winner and returns it.

**Reject is idempotent through the same mechanism.** `record_review` relies on
the unique `(job_id, consultant_id)` constraint rather than on having looked
first, so repeating it converges on one marker even under concurrency.

**Reject and Apply exclude each other, and no index can say so.** A pair holds
one live decision (Slice 2), but the two live in different tables, so
`record_review` and `applications.service.apply` both take the pair's
`job_decision_pairs` row lock first (`lock_pair`). Without it, a concurrent Apply
and Reject each read "no conflict" and each commit — which the integration gate
demonstrates. `withdraw_review` is the documented way back out of a Reject, so
the rule has an exit rather than being a trap.

Text normalization reuses `shared.job_record.clean_text` — the same primitive
the job-record contract uses — so a posting entered here is normalized exactly
as one arriving through any future ingestion path.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Sequence

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import (
    LIVE_APPLICATION_STATUSES,
    Application,
    Job,
    JobDecisionPair,
    JobReview,
    JobStatus,
    ReviewDecision,
    User,
)
from shared.job_record import clean_text


class JobServiceError(RuntimeError):
    """A caller-fixable problem with the submitted Job data."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


#: Short enough to accept a terse internal posting, long enough that an empty
#: box or a stray paste is rejected before it becomes a Job.
MIN_DESCRIPTION_LENGTH = 40

_WHITESPACE = re.compile(r"\s+")


# ── Normalization and the duplicate hash ──────────────────────────────────


def normalize_text(value: str | None) -> str:
    """Store-shape normalization: unescape entities, tidy newlines and runs of
    spaces. Reuses the job-record contract's own cleaner."""
    return clean_text(value or "")


def _fold(value: str | None) -> str:
    """Comparison-shape normalization, for the duplicate hash only.

    Casing and whitespace layout must not make two submissions of the same
    posting look different; wording must. So this folds case and collapses all
    whitespace, and does nothing else — it never drops words.
    """
    return _WHITESPACE.sub(" ", normalize_text(value).lower()).strip()


def content_hash(title: str, company: str | None, description: str) -> str:
    """The exact-duplicate key: sha256 over the folded (title, company, body).

    `\\x1f` (unit separator) joins the fields so that moving text across a field
    boundary — a title that swallows the company name, say — does not collide
    with the original.
    """
    joined = "\x1f".join((_fold(title), _fold(company), _fold(description)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ── Tier 1/2: lightweight near-duplicate warning (V3-P-005/V3-P-006, S9/S11) ─

#: Tier 1 — high-confidence, description alone (plan.md acceptance):
#: word-for-word repeats and superficial formatting changes score at or near
#: 1.0; a genuinely different posting that merely shares a boilerplate opening
#: scores well under this. No title/company gate: the body alone is enough.
NEAR_DUPLICATE_THRESHOLD = 0.85

#: Tier 2 — a moderately reworded body is only a match when paired with a
#: matching title and (if both postings name one) a matching company; without
#: that gate, two different postings that happen to share a boilerplate
#: opening would collide.
NEAR_DUPLICATE_TIER2_THRESHOLD = 0.55

#: How close two folded titles must be (`difflib.SequenceMatcher.ratio`) to
#: count as "the same posting, retitled" for Tier 2. An exact folded match
#: always qualifies too, checked separately so a ratio dip from unrelated
#: punctuation never masks a plain equality.
TITLE_SIMILARITY_THRESHOLD = 0.8

_WORD = re.compile(r"[a-z0-9]+")


def _word_set(description: str) -> set[str]:
    """The description alone, as a case/whitespace-folded word set — title and
    company do not participate in Tier 1, so shared boilerplate outside the
    description cannot inflate the score."""
    return set(_WORD.findall(_fold(description)))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _title_matches(a: str, b: str) -> bool:
    folded_a, folded_b = _fold(a), _fold(b)
    if folded_a == folded_b:
        return True
    return difflib.SequenceMatcher(None, folded_a, folded_b).ratio() >= TITLE_SIMILARITY_THRESHOLD


def _company_matches(a: str | None, b: str | None) -> bool:
    """Gate on company only when **both** postings name one — a posting with
    no company recorded must not be excluded from Tier 2 just because it is
    missing that field."""
    folded_a, folded_b = _fold(a), _fold(b)
    if not folded_a or not folded_b:
        return True
    return folded_a == folded_b


def find_near_duplicate(
    db: Session,
    *,
    site_id: uuid.UUID,
    title: str,
    company: str | None,
    description: str,
    exclude_hash: str,
) -> Job | None:
    """The best-matching **open** Job at `site_id` under the two-tier
    near-duplicate rule, or `None`.

    Tier 1: description-only Jaccard similarity at or above
    `NEAR_DUPLICATE_THRESHOLD` — high-confidence enough that title and company
    do not need to agree. Tier 2: description Jaccard at or above
    `NEAR_DUPLICATE_TIER2_THRESHOLD` **and** a matching title (`_title_matches`)
    **and** a matching company when both postings name one (`_company_matches`)
    — a moderately reworded repost is only a match when the surrounding facts
    agree, so two unrelated postings that merely share a boilerplate opening do
    not collide.

    A simple deterministic precheck (V3-P-006), not an LLM comparison and not a
    lock: two simultaneous near-duplicate submissions can both pass this and
    both create a row — an accepted pilot limitation, same as the exact
    constraint's own documented race.

    `exclude_hash` leaves out any Job whose exact `content_hash` already equals
    the candidate's. `create_job` already resolves that case with precedence
    before this ever runs (its own exact-match lookup, ahead of Tier 1/2); this
    exclusion is the second line of defense for the race window between that
    lookup and this scan — without it, a same-hash row created in that window
    would score 1.0 here too and could still surface as `"near_duplicate"`
    instead of `"duplicate"`.
    """
    candidate = _word_set(description)
    if not candidate:
        return None
    best: Job | None = None
    best_score = 0.0
    for job in db.scalars(
        select(Job).where(
            Job.site_id == site_id,
            Job.status == JobStatus.open.value,
            Job.content_hash != exclude_hash,
        )
    ):
        score = _jaccard(candidate, _word_set(job.description))
        if score < NEAR_DUPLICATE_TIER2_THRESHOLD or score <= best_score:
            continue
        matched = score >= NEAR_DUPLICATE_THRESHOLD or (
            _title_matches(title, job.title) and _company_matches(company, job.company)
        )
        if matched:
            best, best_score = job, score
    return best


def parse_close_date(value: str | date | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise JobServiceError(
            "close_date must be an ISO date (YYYY-MM-DD)."
        ) from None


@dataclass(frozen=True)
class JobInput:
    """Validated manual-entry fields. Built by `build_input`, never by hand."""

    title: str
    company: str
    location: str
    description: str
    source_url: str
    close_date: date | None

    @property
    def hash(self) -> str:
        return content_hash(self.title, self.company, self.description)


def build_input(
    *,
    title: str | None,
    description: str | None,
    company: str | None = None,
    location: str | None = None,
    source_url: str | None = None,
    close_date: str | date | None = None,
) -> JobInput:
    clean_title = normalize_text(title)
    clean_description = normalize_text(description)
    if not clean_title:
        raise JobServiceError("A job title is required.")
    if len(clean_description) < MIN_DESCRIPTION_LENGTH:
        raise JobServiceError(
            "That description is too short to read as a job posting. "
            f"Enter at least {MIN_DESCRIPTION_LENGTH} characters."
        )
    return JobInput(
        title=clean_title[:255],
        company=normalize_text(company)[:255],
        location=normalize_text(location)[:255],
        description=clean_description,
        source_url=(source_url or "").strip()[:1000],
        close_date=parse_close_date(close_date),
    )


# ── Consultant links ──────────────────────────────────────────────────────


def materialize_decision_pair(db: Session, job: Job, consultant: User) -> bool:
    """Link `consultant` to `job` if not already linked. Returns whether a new
    link was written. Idempotent under concurrency via the unique constraint."""
    try:
        # The context-manager form releases the savepoint on success and rolls
        # it back on the IntegrityError, leaving the enclosing transaction —
        # and any work already done in it — intact.
        with db.begin_nested():
            db.add(JobDecisionPair(job_id=job.id, consultant_id=consultant.id))
        return True
    except IntegrityError:
        return False


def linked_consultants(db: Session, job: Job) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(JobDecisionPair, JobDecisionPair.consultant_id == User.id)
            .where(JobDecisionPair.job_id == job.id)
            .order_by(User.username)
        )
    )


# ── Create / read / update / close ────────────────────────────────────────


@dataclass(frozen=True)
class CreateResult:
    job: Job
    #: "created" for a new Job, "duplicate" when the normalized content already
    #: existed, "near_duplicate" when Tier 1 or Tier 2 found a matching
    #: existing posting. All three are successful outcomes with a usable Job.
    status: str

    @property
    def is_duplicate(self) -> bool:
        return self.status == "duplicate"

    @property
    def is_near_duplicate(self) -> bool:
        return self.status == "near_duplicate"


def create_job(
    db: Session, data: JobInput, *, actor: User, consultant: User | None = None
) -> CreateResult:
    """Create a Job in `consultant`'s context, or return the existing (near-)duplicate.

    On an exact or near duplicate the requested Consultant is linked to the Job
    that already exists rather than a second Job being made: a Job is shared,
    and the caller submitted the same or a near-identical posting, so they
    already hold its content. That is what makes "one Job, many Consultants"
    reachable from ordinary use.

    An exact match, when one already exists as an open Job at this Site, takes
    precedence over any other qualifying near match: the lookup below runs
    **before** Tier 1/2, so a distinct open Job that happens to also be a
    genuine Tier 1/2 match can never relabel an exact resubmission as
    `"near_duplicate"` or point it at the wrong Job (acceptance 1's exact-match
    guarantee, restated as G12 in `docs/architecture/areas/jobs.md`). This is a
    read lookup, not the duplicate-blocking mechanism itself — the
    insert/IntegrityError path below still decides the genuine concurrent-race
    case, so a lookup that misses here because a race is in flight is caught
    there instead, not silently accepted as a second row.

    Tier 1/2 (`find_near_duplicate`) runs next: it is a read-only precheck
    against **open** Jobs at this Site only, so it never competes with the
    exact-duplicate unique index for a decision. A near-duplicate match returns
    immediately with no insert attempted.
    """
    existing_exact = db.scalar(
        select(Job).where(
            Job.content_hash == data.hash,
            Job.site_id == actor.site_id,
            Job.status == JobStatus.open.value,
        )
    )
    if existing_exact is not None:
        return CreateResult(job=existing_exact, status="duplicate")

    near = find_near_duplicate(
        db,
        site_id=actor.site_id,
        title=data.title,
        company=data.company,
        description=data.description,
        exclude_hash=data.hash,
    )
    if near is not None:
        return CreateResult(job=near, status="near_duplicate")

    job = Job(
        site_id=actor.site_id,
        title=data.title,
        company=data.company or None,
        location=data.location or None,
        description=data.description,
        source_url=data.source_url or None,
        close_date=data.close_date,
        content_hash=data.hash,
        status=JobStatus.open.value,
        created_by_id=actor.id,
    )
    try:
        with db.begin_nested():
            db.add(job)
        created = True
    except IntegrityError:
        # The expected duplicate path — the unique index rejected the insert.
        created = False

    if not created:
        existing = db.scalar(select(Job).where(Job.content_hash == data.hash, Job.site_id == actor.site_id, Job.status == "open"))
        if existing is None:
            # Only reachable if the winner was rolled back between our failed
            # insert and this read. Surface it honestly rather than inventing a
            # Job; the caller can retry.
            raise JobServiceError(
                "Could not create the job; please try again.", status=409
            )
        db.commit()
        return CreateResult(job=existing, status="duplicate")

    db.commit()
    db.refresh(job)
    return CreateResult(job=job, status="created")


def update_job(db: Session, job: Job, data: JobInput) -> Job:
    """Apply edited manual-entry fields.

    The content hash is recomputed, so an edit that turns this Job into an exact
    copy of another one is refused by the same unique constraint that blocks a
    duplicate create — reported as a conflict rather than silently merging two
    Jobs that separate Consultants are already reviewing.
    """
    job.title = data.title
    job.company = data.company or None
    job.location = data.location or None
    job.description = data.description
    job.source_url = data.source_url or None
    job.close_date = data.close_date
    job.content_hash = data.hash
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise JobServiceError(
            "Another job already has this exact title, company, and "
            "description.",
            status=409,
        ) from None
    db.refresh(job)
    return job


def close_job(db: Session, job: Job) -> Job:
    """Mark the Job closed. Idempotent: closing a closed Job changes nothing,
    and in particular does not move `closed_at` forward.

    Takes the Job row lock first so it serializes with the per-Consultant
    actions that require the Job to be open (`lock_job`). Without that shared
    boundary, close and Apply/Reject have no order at all and both can commit
    from stale reads of each other.
    """
    lock_job(db, job)
    if job.status != JobStatus.closed.value:
        job.status = JobStatus.closed.value
        job.closed_at = datetime.now(timezone.utc)
        from backend.db.models.matching import JobConsultantMatch
        db.execute(delete(JobConsultantMatch).where(JobConsultantMatch.job_id == job.id))
        db.commit()
        db.refresh(job)
    return job


#: The one client-safe message for "this Job is closed". Kept as a constant so
#: score and Reject cannot drift into reporting the same state two ways.
JOB_CLOSED_MESSAGE = "This job is closed. Reopen is not supported."


def ensure_job_is_open(job: Job) -> None:
    """Guard the per-Consultant actions that only make sense on a live posting.

    Closing is what a person does when the position is gone. Scoring it or
    filing a fresh Reject afterwards is not a smaller version of the same
    workflow — it produces advice nobody can act on and evaluation records
    against a vacancy that no longer exists. So a closed Job stays listed,
    fetchable, and editable (a record may still need correcting) while score and
    Reject refuse.

    409, not 403: the caller is authorized and the request is well-formed; it is
    the Job's *state* that conflicts. An existing Reject marker is untouched —
    closing does not retract a decision that was validly made.
    """
    if job.status == JobStatus.closed.value:
        raise JobServiceError(JOB_CLOSED_MESSAGE, status=409)


# ── Review markers ────────────────────────────────────────────────────────


def lock_job(db: Session, job: Job) -> None:
    """Take the Job row's lock and reload it from the database.

    `ensure_job_is_open` reads the caller's already-loaded instance, which a
    request that began before a concurrent `close_job` committed will have
    populated with the *old* status. Checking that stale copy let a brand-new
    Application or Reject marker land against a Job that was closed seconds
    earlier — the opposite of the documented rule, and distinct from the
    allowed case where the write wins first and the Job closes afterwards.

    So the open-state decision is serialized on the Job row itself: this reloads
    it under `FOR UPDATE`, and `close_job` takes the same lock before writing.
    Whichever transaction reaches the row first gets a real order — either the
    write sees `open` and close waits, or close commits and the write reloads
    `closed` and answers 409. Every caller takes this lock **before**
    `lock_pair`, so the two are always acquired in the same order and cannot
    deadlock.

    Two details that are easy to get wrong, both measured rather than assumed:

    - **`populate_existing` is mandatory.** A plain `SELECT ... FOR UPDATE` for
      a row already in the identity map returns the *existing* instance without
      overwriting its loaded attributes — so the lock would be taken and the
      stale status read anyway, silently restoring the defect this exists to fix.
    - **A vanished row is a 404, not a crash.** `Session.refresh` raises
      `InvalidRequestError` ("Could not refresh instance") when the row is gone,
      which is not a service error and escapes as a 500. Selecting instead
      yields `None` and lets that be answered honestly.
    """
    locked = db.scalars(
        select(Job)
        .where(Job.id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if locked is None:
        raise JobServiceError("Job not found", status=404)


def lock_pair(db: Session, job: Job, consultant: User) -> None:
    """Serialize the writes that decide a Consultant–Job pair's one decision.

    Apply and Reject exclude each other, but they write to *different tables*,
    so no unique index can enforce the rule the way `content_hash` enforces
    duplicate blocking: two concurrent transactions can each read "no conflict"
    and each commit, leaving a pair that is both rejected and live. A row lock
    on something they share is what makes the check-then-write safe, and the
    thing they share is the `job_decision_pairs` link — which both actions already
    require (`policy.ensure_consultant_operates_job`), so this locks a row that
    is guaranteed to exist on every path that reaches it rather than inventing
    one.

    Held until the caller's commit. On SQLite (the fast gate) `FOR UPDATE`
    renders as nothing and the engine serializes writers anyway. If the link
    row is somehow absent, no lock is taken and the pair rules degrade to
    check-then-write — the same behaviour as before this existed, never worse.
    """
    materialize_decision_pair(db, job, consultant)
    db.execute(
        select(JobDecisionPair.id)
        .where(
            JobDecisionPair.job_id == job.id,
            JobDecisionPair.consultant_id == consultant.id,
        )
        .with_for_update()
    )


#: The Reject side of the one-live-decision rule (Slice 2). Its mirror image is
#: `backend.applications.service.REJECTED_PAIR_MESSAGE`.
LIVE_APPLICATION_MESSAGE = (
    "That consultant already has a live application for this job. Withdraw or "
    "close it before rejecting."
)


def ensure_no_live_application(
    db: Session, job: Job, consultant: User
) -> None:
    """Guard the Reject half of "one live decision per Consultant–Job pair".

    Apply and Reject are the same question answered two ways, so a pair holding
    both is not a richer record — it is an incoherent one, and whichever of the
    two a later reader trusts is arbitrary. The Apply half of this rule lives in
    `backend/applications/service.apply`; this half reads the `Application`
    model directly rather than that service, which keeps the dependency one-way
    (Applications know about Jobs, not the reverse) with no shared vocabulary
    duplicated — `LIVE_APPLICATION_STATUSES` is defined once, on the model.

    409, not 403: the caller is authorized and the request well-formed; it is
    the pair's *state* that conflicts. The message names the way out, because a
    rule with no resolution is a dead end rather than an invariant.
    """
    live = db.scalar(
        select(Application.id).where(
            Application.job_id == job.id,
            Application.consultant_id == consultant.id,
            Application.status.in_(LIVE_APPLICATION_STATUSES),
        )
    )
    if live is not None:
        raise JobServiceError(LIVE_APPLICATION_MESSAGE, status=409)


def get_review(db: Session, job: Job, consultant: User) -> JobReview | None:
    return db.scalar(
        select(JobReview).where(
            JobReview.job_id == job.id,
            JobReview.consultant_id == consultant.id,
        )
    )


def record_review(
    db: Session,
    job: Job,
    consultant: User,
    *,
    decision: str,
    actor: User,
) -> tuple[JobReview, bool]:
    """Record a review marker for one Consultant–Job pair.

    Returns `(review, created)`. Repeating the same decision is a no-op that
    returns the existing marker, so a double-clicked Reject leaves exactly one
    row. `created` is False on a repeat.

    Refuses a closed Job, and refuses a pair that already holds a live
    Application (Slice 2 — see `ensure_no_live_application`). Both checks live
    here, at the write boundary, rather than only in the route: a second caller
    of this service must not be able to file a review against a closed posting,
    or contradict a live Application, by forgetting a precondition. Both are
    decided under a row lock, so neither can be defeated by a concurrent close
    or Apply that committed after this request loaded its objects.
    """
    if decision not in {d.value for d in ReviewDecision}:
        # Reached by any client still speaking V1's verdict vocabulary,
        # `maybe` included. There is no translation layer: the value simply
        # does not exist in V2 (P-008).
        #
        # Checked before the lifecycle guard: an unknown decision is a broken
        # client contract and should say so, rather than being masked by a 409
        # that implies the word would have been fine on an open Job.
        raise JobServiceError(f"Unknown review decision: {decision}")

    # Job row first, then the pair row — the same order `applications.service
    # .apply` uses, so the two can never deadlock against each other.
    lock_job(db, job)
    ensure_job_is_open(job)
    lock_pair(db, job, consultant)
    ensure_no_live_application(db, job, consultant)

    review = JobReview(
        job_id=job.id,
        consultant_id=consultant.id,
        decision=decision,
        created_by_id=actor.id,
    )
    try:
        with db.begin_nested():
            db.add(review)
    except IntegrityError:
        pass
    else:
        db.commit()
        db.refresh(review)
        return review, True

    existing = get_review(db, job, consultant)
    if existing is None:
        raise JobServiceError(
            "Could not record the review; please try again.", status=409
        )
    return existing, False


def withdraw_review(db: Session, job: Job, consultant: User) -> bool:
    """Remove this pair's review marker. Returns whether one was there.

    Slice 2 makes Reject and Apply mutually exclusive for a pair, which is only
    a workable rule if a Reject can be undone: without this, one mistaken click
    would permanently bar a Consultant from a Job. Idempotent, so a repeated
    request is `False` rather than an error.

    The one-marker invariant is preserved — this removes the marker, it does not
    add a second "un-reject" one — and a Reject *withdrawn* is not a Reject
    *retained*, so nothing here reintroduces V1's verdict history. Deleting a
    recorded human decision is deliberately its own explicit command rather than
    a side effect of Apply: a score or another action may never overwrite a
    person's decision on their behalf (CLAUDE.md invariant 9).

    Allowed on a closed Job: closing refuses *new* evaluations, and withdrawing
    is the removal of one, not the making of one.
    """
    review = get_review(db, job, consultant)
    if review is None:
        return False
    db.delete(review)
    db.commit()
    return True


def reviews_for_job(db: Session, job: Job) -> list[JobReview]:
    return list(
        db.scalars(
            select(JobReview)
            .where(JobReview.job_id == job.id)
            .order_by(JobReview.created_at)
        )
    )


def reviewed_job_ids(
    db: Session, consultant_ids: Sequence[uuid.UUID] | None
) -> dict[uuid.UUID, dict[uuid.UUID, str]]:
    """`{job_id: {consultant_id: decision}}` for the given Consultants.

    One query for a whole list page, so rendering N Jobs does not issue N
    review lookups.

    **`None` means unrestricted (Admin), an empty sequence means nothing** —
    the same contract as `list_jobs`, and the same one
    `policy.operable_consultant_ids` produces. The two were once different here,
    and coercing the sentinel with `or []` on the way in silently turned "show
    the Admin everything" into "show the Admin nothing": the list said no Job
    was reviewed while the detail view said otherwise. Both selectors now read
    the sentinel the same way, so there is no conversion left to get wrong.
    """
    if consultant_ids is not None and not consultant_ids:
        return {}
    out: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    query = select(JobReview.job_id, JobReview.consultant_id, JobReview.decision)
    if consultant_ids is not None:
        query = query.where(JobReview.consultant_id.in_(list(consultant_ids)))
    rows = db.execute(query)
    for job_id, consultant_id, decision in rows:
        out.setdefault(job_id, {})[consultant_id] = decision
    return out
