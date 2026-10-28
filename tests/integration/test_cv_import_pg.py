"""Durable CV import against real Postgres 16 (V2 Slice 4).

What needs the real engine, and why the fast gate cannot answer it:

- **Concurrent first-time imports for one consultant.** The unique
  `consultant_username` index exists because a check-then-insert is not
  enough: two concurrent uploads racing the same consultant's first import are
  two transactions, and only two real connections prove the index — not a
  prior SELECT — is what decides.
- **Concurrent handler invocations of the same attempt.** The runner's
  contract is at-least-once *invocation*: a dead lock session can leave one
  invocation running while a standby recovers the run and starts another.
  Unlike tailored-CV generation (which overwrites its content wholesale), this
  handler's effect — merging into `profile.md` — is not naturally idempotent,
  so it is guarded by a file lock plus a re-read-under-the-lock, and the only
  way to prove that actually admits one writer is two real threads racing a
  real `flock`.
- **Schema shape after `alembic upgrade`.** The fast gate builds its schema
  with `create_all`; production runs the migration. A missing unique index or
  an absent check constraint would pass every fast test and then allow two
  import rows for one consultant, or a status the code cannot handle.

Every database here is created and dropped by the fixtures in `conftest.py`.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.cv_importer import handler
from backend.cv_importer import service as cv_import_service
from backend.db.models import CVImport, User, WorkflowRun
from backend.workflows import runner as workflow_runner


def _extracted(full_name: str = "Nora M") -> dict:
    return {
        "identity": {
            "full_name": full_name,
            "email": "nora@example.invalid",
            "location": {"city": "Gothenburg", "country": "Sweden", "country_code": "SE"},
            "spoken_languages": [
                {"language": "English", "proficiency": "native", "is_native": True}
            ],
        },
        "education": [],
        "career_history": {
            "role_groups": [
                {
                    "role_group_id": "",
                    "organization": {"name": "Volvo", "country_code": "SE", "industries": []},
                    "display_role_title": "Engineer",
                    "started": "2020-01",
                    "ended": "present",
                    "employment_type": "permanent",
                    "blocks": [
                        {
                            "block_id": "",
                            "role_title": "Engineer",
                            "started": "2020-01",
                            "ended": "present",
                            "block_type": "technical_delivery",
                            "evidence_items": [
                                {"evidence_id": "", "type": "responsibility", "text": "Built things."}
                            ],
                        }
                    ],
                }
            ]
        },
        "gaps": [],
    }


@pytest.fixture
def pg_import(migrated_db, tmp_path, monkeypatch):
    """A sessionmaker on the migrated database, one live User, and an isolated
    profile root wired everywhere the import path reads it."""
    from backend import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.cv_importer.experience_writer.DATA_DIR", tmp_path)

    engine = create_engine(migrated_db)
    Session = sessionmaker(bind=engine)
    site_id, user_id = uuid.uuid4(), uuid.uuid4()
    username = "pg-import-consultant"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sites (id, code, name, is_active, created_at, "
                "updated_at) VALUES (:id, 'GBG', 'Test', true, now(), now())"
            ),
            {"id": site_id},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, email, username, is_active, "
                "actively_looking, site_id, created_at, updated_at) VALUES "
                "(:id, :email, :username, true, true, :site, now(), now())"
            ),
            {"id": user_id, "email": f"{username}@example.invalid", "username": username, "site": site_id},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, role) VALUES (:id, :uid, 'consultant')"
            ),
            {"id": uuid.uuid4(), "uid": user_id},
        )
    return Session, user_id, username, tmp_path


def _race(target, count=2):
    barrier = threading.Barrier(count)
    results: list = []
    errors: list = []
    threads = [
        threading.Thread(target=target, args=(barrier, results, errors))
        for _ in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


# ── Concurrent first-time import ──────────────────────────────────────────


def test_two_concurrent_identical_first_imports_yield_one_row_and_one_run(pg_import):
    """The double-click / retried-POST race: the same bytes, submitted twice,
    genuinely concurrently. Both transactions race the same consultant's first
    import; the unique `consultant_username` index decides the winner, and the
    loser must join rather than conflict — it computes the *same* input hash,
    so this is acceptance check 1's "duplicate submissions converge without
    duplicate work", not the different-uploads conflict a later test covers.
    """
    Session, user_id, username, tmp_path = pg_import
    same_bytes = b"%PDF-1.4 identical upload"

    def attempt(barrier, results, errors) -> None:
        db = Session()
        try:
            actor = db.get(User, user_id)
            source_path, input_hash = cv_import_service.save_upload(
                username, filename="cv.pdf", content_type="application/pdf",
                data=same_bytes,
            )
            barrier.wait(timeout=30)
            cv_import, run, started = cv_import_service.request_import(
                db, username, actor=actor, source_path=source_path,
                source_filename="cv.pdf", content_type="application/pdf",
                input_hash=input_hash,
            )
            results.append((started, str(cv_import.id), str(run.id)))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(attempt)

    assert not errors, f"a racing import raised instead of resolving: {errors}"
    assert len(results) == 2
    assert len({row_id for _, row_id, _ in results}) == 1, (
        "two concurrent identical imports produced two rows for one consultant"
    )
    assert len({run_id for _, _, run_id in results}) == 1, (
        "the loser started a second run instead of joining the winner's"
    )
    assert sorted(started for started, _, _ in results) == [False, True]

    verify = Session()
    try:
        assert verify.query(CVImport).count() == 1
        assert verify.query(WorkflowRun).count() == 1
    finally:
        verify.close()


def test_two_concurrent_different_uploads_for_one_consultant_refuse_the_second(
    pg_import,
):
    """The genuine conflict: two *different* uploads racing one consultant's
    first import. Whichever transaction's insert wins proceeds normally; the
    loser, once unblocked by the winner's commit, sees a different input hash
    already `processing` and is refused (409) rather than silently starting a
    second import for the same consultant."""
    Session, user_id, username, tmp_path = pg_import

    def attempt(barrier, results, errors, payload: bytes) -> None:
        db = Session()
        try:
            actor = db.get(User, user_id)
            source_path, input_hash = cv_import_service.save_upload(
                username, filename="cv.pdf", content_type="application/pdf",
                data=payload,
            )
            barrier.wait(timeout=30)
            cv_import, run, started = cv_import_service.request_import(
                db, username, actor=actor, source_path=source_path,
                source_filename="cv.pdf", content_type="application/pdf",
                input_hash=input_hash,
            )
            results.append((started, str(cv_import.id)))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
        finally:
            db.close()

    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []
    t1 = threading.Thread(
        target=attempt, args=(barrier, results, errors, b"%PDF-1.4 first")
    )
    t2 = threading.Thread(
        target=attempt, args=(barrier, results, errors, b"%PDF-1.4 second")
    )
    t1.start(); t2.start()
    t1.join(timeout=60); t2.join(timeout=60)

    # Exactly one of the two outcomes is a real result; the other is the
    # service's 409. Both threads reaching the barrier and one of them
    # resolving (rather than deadlocking) is itself part of what this proves.
    assert len(results) + len(errors) == 2
    assert len(results) == 1
    assert results[0][0] is True
    if errors:
        assert getattr(errors[0], "status", None) == 409

    verify = Session()
    try:
        assert verify.query(CVImport).count() == 1
        assert verify.query(WorkflowRun).count() == 1
    finally:
        verify.close()


# ── Concurrent handler invocations of one attempt ────────────────────────


def test_two_concurrent_handler_invocations_write_the_profile_exactly_once(
    pg_import, monkeypatch
):
    """The at-least-once-invocation contract, at this handler's own boundary.

    Unlike `commit_generated_state_once` (an overwrite, so a duplicate effect
    is merely wasted work), this handler's effect is a *merge*: two
    invocations each calling `write_profile` on the same attempt would each
    merge the same extraction into an already-merged `profile.md`, duplicating
    every role group and evidence item. The profile-write lock plus the
    re-read-under-the-lock (`backend/cv_importer/handler.py`) exists
    specifically to prevent that — this proves it does, against a real `flock`
    and two real threads, not a sequential call.
    """
    Session, user_id, username, tmp_path = pg_import

    setup = Session()
    try:
        actor = setup.get(User, user_id)
        source_path, input_hash = cv_import_service.save_upload(
            username, filename="cv.pdf", content_type="application/pdf",
            data=b"%PDF-1.4 once",
        )
        cv_import, run, started = cv_import_service.request_import(
            setup, username, actor=actor, source_path=source_path,
            source_filename="cv.pdf", content_type="application/pdf",
            input_hash=input_hash,
        )
        assert started
        run_id = run.id
    finally:
        setup.close()

    monkeypatch.setattr(handler, "extract_text", lambda path, mime: "Raw CV text. " * 20)

    async def _extract(raw_text):  # noqa: ARG001
        return _extracted(), [], "raw"

    monkeypatch.setattr(handler, "extract_experience", _extract)

    def invoke(barrier, results, errors) -> None:
        db = Session()
        try:
            run_row = db.get(WorkflowRun, run_id)
            barrier.wait(timeout=30)
            result_ref = handler.handle_cv_import(db, run_row)
            results.append(result_ref)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(invoke)

    assert not errors, f"a racing handler invocation raised: {errors}"
    assert len(results) == 2

    verify = Session()
    try:
        cv_import = verify.query(CVImport).one()
        assert cv_import.status == "succeeded"
        assert cv_import.source_ref is None

        profile_text = (tmp_path / username / "profile.md").read_text(encoding="utf-8")
        # A duplicated merge would emit the role group's organization twice.
        assert profile_text.count("name: Volvo") == 1
    finally:
        verify.close()


# ── Retry decisions that turn on a real row lock ──────────────────────────
#
# Both cases below were previously evidenced only by a hand-written HTTP demo
# script, re-authored and thrown away once per review round (`S4-P1`,
# `S4-P1-R1`, `S4-P1-R2`, `S4-P1-R3`). They belong here instead: the decision
# they exercise is made while holding a `FOR UPDATE` lock on the import row
# and finishes with a guarded conditional `UPDATE`, neither of which SQLite
# models, and as gate tests they are permanent regressions a reviewer can
# re-run rather than prose to take on trust.


def _stub_extraction(monkeypatch, full_name: str = "Nora M") -> None:
    monkeypatch.setattr(handler, "extract_text", lambda path, mime: "Raw CV text. " * 20)

    async def _extract(raw_text):  # noqa: ARG001
        return _extracted(full_name), [], "raw"

    async def _merge(existing_profile, new_extraction):  # noqa: ARG001
        merged = dict(existing_profile)
        merged["career_history"] = new_extraction.get("career_history", {})
        merged["identity"] = new_extraction.get(
            "identity", existing_profile.get("identity", {})
        )
        return merged, []

    monkeypatch.setattr(handler, "extract_experience", _extract)
    monkeypatch.setattr("backend.cv_importer.experience_writer.merge_profiles", _merge)


def _import_once(Session, user_id, username, payload: bytes):
    db = Session()
    try:
        actor = db.get(User, user_id)
        source_path, input_hash = cv_import_service.save_upload(
            username, filename="cv.pdf", content_type="application/pdf", data=payload
        )
        return cv_import_service.request_import(
            db, username, actor=actor, source_path=source_path,
            source_filename="cv.pdf", content_type="application/pdf",
            input_hash=input_hash,
        )
    finally:
        db.close()


def test_a_stuck_finalize_commit_is_reconciled_by_the_next_identical_upload(
    pg_import, monkeypatch
):
    """`S4-P1-R2`/`S4-P1-R3`. The handler's write lands and its marker with it,
    then `commit_import_success_once` fails — the row stays `processing` with
    no active run. The next identical upload must finalize *that* attempt under
    its own unchanged key instead of creating a new one, because a new attempt
    could never match the marker already on disk. The reconciliation runs
    inside `request_import`'s `FOR UPDATE` lock and ends in the same guarded
    conditional `UPDATE` the handler uses, so real row locking is the point.
    """
    Session, user_id, username, tmp_path = pg_import
    _stub_extraction(monkeypatch)
    payload = b"%PDF-1.4 stuck"

    cv_import, run, started = _import_once(Session, user_id, username, payload)
    assert started
    cv_import_id = cv_import.id

    real_commit = cv_import_service.commit_import_success_once

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transient commit failure")

    monkeypatch.setattr(cv_import_service, "commit_import_success_once", _boom)

    # Through the real runner, not a direct handler call: `run_once` is what
    # claims the run and marks it terminally `failed` once the handler raises.
    # A direct call would leave the run `queued`, and the retry below would
    # then *join* that still-active run instead of reaching the reconciliation
    # this test exists to prove.
    db = Session()
    try:
        assert workflow_runner.run_once(db) is True
    finally:
        db.close()

    verify = Session()
    try:
        row = verify.get(CVImport, cv_import_id)
        assert row.status == "processing", (
            "a failed finalize commit must not be recorded as an import "
            "failure -- the content is already correct on disk"
        )
    finally:
        verify.close()
    profile_path = tmp_path / username / "profile.md"
    written_once = profile_path.read_bytes()
    assert b"_last_import_key" in written_once

    monkeypatch.setattr(cv_import_service, "commit_import_success_once", real_commit)
    write_calls = {"count": 0}
    real_write_profile = handler.write_profile

    async def _counting_write_profile(*args, **kwargs):
        write_calls["count"] += 1
        return await real_write_profile(*args, **kwargs)

    monkeypatch.setattr(handler, "write_profile", _counting_write_profile)

    reconciled, run2, started2 = _import_once(Session, user_id, username, payload)
    assert started2 is False, "a reconciled stuck attempt is not a fresh start"
    assert run2 is None, "reconciliation must enqueue no run at all"
    assert reconciled.status == "succeeded"
    assert reconciled.attempt == 0, "the stuck attempt's own number is finalized"
    assert write_calls["count"] == 0, "the retry merged again instead of reconciling"
    assert profile_path.read_bytes() == written_once

    verify = Session()
    try:
        assert verify.query(WorkflowRun).count() == 1
        # `S4-P1-R4`: the row and its run are finalized together. A run left
        # `failed` beside a `succeeded` import is what `routes._envelope`
        # would then serve on every poll.
        run_row = verify.query(WorkflowRun).one()
        assert run_row.status == "succeeded"
        assert run_row.result_ref
        assert run_row.error_code is None
    finally:
        verify.close()


def test_reconciliation_finalizes_the_row_and_its_run_in_one_transaction(
    pg_import, monkeypatch
):
    """`S4-P1-R5`, against the engine that actually has to roll both back.

    Reconciliation writes two tables — `cv_imports` and that attempt's
    `workflow_runs` row — and `routes._envelope` serves both on every poll. As
    two commits, a process death in between durably left a succeeded import
    beside a failed run. This interrupts at the boundary by failing the second
    commit the request makes: there is only one now, so nothing is interrupted;
    while there were two, the interrupted one was the run upgrade. Either way
    the pair must never be observed disagreeing.
    """
    Session, user_id, username, tmp_path = pg_import
    _stub_extraction(monkeypatch)
    payload = b"%PDF-1.4 atomic"

    cv_import, run, started = _import_once(Session, user_id, username, payload)
    assert started
    cv_import_id, key = cv_import.id, run.idempotency_key

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transient commit failure")

    real_commit_once = cv_import_service.commit_import_success_once
    monkeypatch.setattr(cv_import_service, "commit_import_success_once", _boom)
    db = Session()
    try:
        assert workflow_runner.run_once(db) is True
    finally:
        db.close()
    monkeypatch.setattr(
        cv_import_service, "commit_import_success_once", real_commit_once
    )

    db = Session()
    commits = {"count": 0}
    real_commit = db.commit

    def _die_on_the_second_commit():
        commits["count"] += 1
        if commits["count"] == 2:
            raise RuntimeError("process death at the transaction boundary")
        return real_commit()

    try:
        actor = db.get(User, user_id)
        source_path, input_hash = cv_import_service.save_upload(
            username, filename="cv.pdf", content_type="application/pdf", data=payload
        )
        monkeypatch.setattr(db, "commit", _die_on_the_second_commit)
        try:
            cv_import_service.request_import(
                db, username, actor=actor, source_path=source_path,
                source_filename="cv.pdf", content_type="application/pdf",
                input_hash=input_hash,
            )
        except RuntimeError:
            pass  # the simulated death; the invariant below is the assertion
    finally:
        monkeypatch.undo()
        db.close()

    verify = Session()
    try:
        row = verify.get(CVImport, cv_import_id)
        run_row = verify.scalar(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == key)
        )
        assert (row.status == "succeeded") == (run_row.status == "succeeded"), (
            f"a poll can observe the import {row.status!r} while its own run "
            f"is {run_row.status!r}"
        )
    finally:
        verify.close()


def test_two_concurrent_retries_of_one_stuck_attempt_reconcile_it_once(
    pg_import, monkeypatch
):
    """The open question `S4-P1-R3` and `S4-P1-R4` both left unproven: two
    retries of the same stuck attempt, genuinely concurrent, against a real
    row lock.

    Only one may reconcile — `request_import` takes the import row's
    `FOR UPDATE` lock before deciding, and the finalization is a guarded
    conditional `UPDATE`, so the loser cannot finalize the same attempt twice.
    What the loser *does* do is deliberate rather than incidental: it wakes to
    a row that now reads `succeeded`, which is indistinguishable from an
    ordinary re-upload after a finished import, and `S4-P1-R3` requires that
    to become a genuine new attempt. So the invariant proved here is not "the
    loser does nothing" but "nothing is corrupted and nothing deadlocks": one
    reconciliation, the stuck run upgraded exactly once, and any new attempt a
    loser starts is a well-formed one whose row and run agree.
    """
    Session, user_id, username, tmp_path = pg_import
    _stub_extraction(monkeypatch)
    payload = b"%PDF-1.4 two retries"

    cv_import, run, started = _import_once(Session, user_id, username, payload)
    assert started
    cv_import_id, stuck_key = cv_import.id, run.idempotency_key

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transient commit failure")

    real_commit_once = cv_import_service.commit_import_success_once
    monkeypatch.setattr(cv_import_service, "commit_import_success_once", _boom)
    db = Session()
    try:
        assert workflow_runner.run_once(db) is True
    finally:
        db.close()
    monkeypatch.setattr(
        cv_import_service, "commit_import_success_once", real_commit_once
    )

    profile_path = tmp_path / username / "profile.md"
    written_once = profile_path.read_bytes()

    def retry(barrier, results, errors) -> None:
        db = Session()
        try:
            actor = db.get(User, user_id)
            source_path, input_hash = cv_import_service.save_upload(
                username, filename="cv.pdf", content_type="application/pdf",
                data=payload,
            )
            barrier.wait(timeout=30)
            row, run, started = cv_import_service.request_import(
                db, username, actor=actor, source_path=source_path,
                source_filename="cv.pdf", content_type="application/pdf",
                input_hash=input_hash,
            )
            results.append((started, row.status, row.attempt))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(retry)

    assert not errors, f"a racing retry raised instead of resolving: {errors}"
    assert len(results) == 2
    reconciled = [r for r in results if r[0] is False]
    assert len(reconciled) == 1, (
        f"expected exactly one reconciliation, got {results}"
    )
    assert reconciled[0][1] == "succeeded" and reconciled[0][2] == 0

    verify = Session()
    try:
        stuck_run = verify.scalar(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == stuck_key)
        )
        assert stuck_run.status == "succeeded", (
            "the stuck attempt's own run was not finalized with its row"
        )
        row = verify.get(CVImport, cv_import_id)
        current = verify.scalar(
            select(WorkflowRun).where(
                WorkflowRun.idempotency_key
                == f"cv_import:{row.id}:{row.input_hash}:{row.attempt}"
            )
        )
        assert current is not None, "the row's current attempt has no run"
        if row.attempt == 0:
            # Neither retry started a new attempt: the row and its run are the
            # reconciled pair, and nothing re-merged.
            assert row.status == "succeeded" and current.status == "succeeded"
            assert profile_path.read_bytes() == written_once
        else:
            # The loser started a genuine new attempt, which is what an upload
            # arriving after a finished import is supposed to do.
            assert row.status == "processing" and current.status == "queued"
    finally:
        verify.close()


def test_a_reupload_of_the_same_bytes_after_success_is_a_genuine_new_attempt(
    pg_import, monkeypatch
):
    """`S4-P1-R3` finding 1, the other side of the line above. Once an import
    has *succeeded*, identical bytes uploaded again must merge into whatever
    `profile.md` holds *now* — it may have been edited since — rather than
    being recognised by content hash as already done. Matching a marker on
    `(cv_import_id, input_hash)` alone conflated the two cases; only the row's
    own live state, read under its lock, tells them apart.
    """
    Session, user_id, username, tmp_path = pg_import
    _stub_extraction(monkeypatch)
    payload = b"%PDF-1.4 twice"

    cv_import, run, started = _import_once(Session, user_id, username, payload)
    assert started

    db = Session()
    try:
        assert workflow_runner.run_once(db) is True
    finally:
        db.close()

    verify = Session()
    try:
        assert verify.get(CVImport, cv_import.id).status == "succeeded"
    finally:
        verify.close()

    profile_path = tmp_path / username / "profile.md"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace("Nora M", "Edited Elsewhere"),
        encoding="utf-8",
    )
    edited = profile_path.read_bytes()

    again, run2, started2 = _import_once(Session, user_id, username, payload)
    assert started2 is True, "a re-upload after success must be a real new attempt"
    assert run2 is not None
    assert again.attempt == 1

    db = Session()
    try:
        assert workflow_runner.run_once(db) is True
    finally:
        db.close()

    assert profile_path.read_bytes() != edited, (
        "the second import reported success without writing the merged profile"
    )
    verify = Session()
    try:
        row = verify.get(CVImport, cv_import.id)
        assert row.status == "succeeded" and row.attempt == 1
        assert verify.query(WorkflowRun).count() == 2
    finally:
        verify.close()


# ── Schema shape after the migration ──────────────────────────────────────


def test_one_import_row_per_consultant_is_enforced_by_the_migration(pg_engine):
    """Not merely `create_all` in the fast gate: production runs the
    migration, and a non-unique index here would allow two import rows for one
    consultant with every fast test still green."""
    with pg_engine.connect() as conn:
        definition = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = "
                "'cv_imports' AND indexname = 'ix_cv_imports_consultant_username'"
            )
        ).scalar_one()
    assert "UNIQUE" in definition.upper()


def test_the_status_vocabulary_is_closed_by_the_migration(pg_engine):
    with pg_engine.connect() as conn:
        with pytest.raises(Exception):  # noqa: B017 - asserting a DB-level rejection
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO cv_imports (id, consultant_username, "
                        "status, attempt, input_hash, created_at, updated_at) "
                        "VALUES (:id, 'nobody', 'bogus', 0, 'x', now(), now())"
                    ),
                    {"id": uuid.uuid4()},
                )
