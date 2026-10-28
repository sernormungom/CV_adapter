"""Durable async CV import (V2 Slice 4).

What this file has to prove, in the order the risk sits:

1. **Accepted durably, idempotent, and observable.** 202 + a run to poll;
   retrying an identical upload joins the run in flight rather than starting a
   second one; a different upload while one is in flight is refused; a retry
   after a finished attempt is a genuine new one.
2. **The transient upload is deleted after the handler's last required
   stage** — on success, on failure, and when a request's own upload turns out
   to be redundant (it joined an in-flight run instead).
3. **A hard extraction/validation failure is recorded honestly** without
   leaking upstream text, and does not touch `profile.md`.
4. **Authorization** matches the other `/api/profiles/{username}/...` routes:
   self, the related TA, an Admin; an unrelated TA is refused; Site is never a
   factor.
5. **A crash between the profile write landing on disk and the row's success
   commit does not duplicate the merge on recovery** (`S4-P1-R1` finding 1) —
   the durable, on-disk `_last_import_key` marker is what a recovered
   invocation checks instead of re-deriving "did this already happen" from a
   database row that, by construction, never got written.
6. **An unexpected failure while accepting the request still deletes this
   request's own upload** (`S4-P1-R1` finding 2), not only the expected
   validation/conflict refusals.
7. **A finalize-commit failure *after* the marked write lands does not cause
   a later attempt to merge again** (`S4-P1-R2` finding 1, corrected by
   `S4-P1-R3`) — a *stuck* attempt (its own write landed, its own commit
   failed) is reconciled directly under its own unchanged key by
   `service.request_import`, before a new attempt is ever created; the
   handler's own marker check stays an exact match on
   `(cv_import_id, input_hash, attempt)`.
8. **A legitimate re-upload of the same bytes after a *successful* prior
   import still merges, rather than being silently skipped** (`S4-P1-R3`
   finding 1) — an earlier fix relaxed the handler's marker match to ignore
   `attempt`, which also matched this case by content hash alone and produced
   a false "success" that left the profile unchanged.

The LLM extraction call is stubbed at `handler.extract_experience`, and text
extraction at `handler.extract_text` — everything below them (`write_profile`,
the atomic effect, the route/authz layers) runs for real. Nothing here reaches
the network.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from backend import config
from backend.cv_importer import experience_writer, handler, service
from backend.cv_importer.keys import ImportKey
from backend.db.models import CVImport, Site, User, WorkflowRun
from backend.workflows import runner as workflow_runner

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def import_profiles(tmp_path, monkeypatch):
    """An isolated profile root, wired everywhere the import path reads it.

    Two bindings: `experience_writer` binds `DATA_DIR` at import time
    (`from backend.config import DATA_DIR`), so patching only `config` would
    leave it pointed at the real root. `cv_importer.service` and
    `cv_importer.preview` read `config.DATA_DIR` through the module, so one
    patch covers both — see `docs/testing.md`.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.cv_importer.experience_writer.DATA_DIR", tmp_path)
    return tmp_path


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
def stub_extraction(monkeypatch):
    """Stub text extraction and both LLM calls. Everything below them is real.

    Two LLM calls, not one: `write_profile` calls `merge_profiles` (a second,
    separate model call) whenever a profile already exists — exercised by a
    re-import. `handler.extract_text`/`handler.extract_experience` cover the
    first import; `experience_writer.merge_profiles` (the name
    `experience_writer.py` binds it under) covers a re-import merging into one.

    Returns a mutable box so a test can change what "extraction" produces, or
    make it fail, between imports.
    """
    box = {"data": _extracted(), "errors": [], "calls": 0}

    async def _extract(raw_text):  # noqa: ARG001
        box["calls"] += 1
        if box["data"] is None:
            return None, box["errors"], "raw"
        return box["data"], [], "raw"

    async def _merge(existing_profile, new_extraction):  # noqa: ARG001
        # The real merge rules are `profile_merger`'s own concern; for these
        # tests it is enough that a re-import onto an existing profile
        # produces *a* valid merged document without reaching the network.
        merged = dict(existing_profile)
        merged["career_history"] = new_extraction.get("career_history", {})
        merged["identity"] = new_extraction.get("identity", existing_profile.get("identity", {}))
        return merged, []

    monkeypatch.setattr(handler, "extract_text", lambda path, mime: "Raw CV text. " * 20)
    monkeypatch.setattr(handler, "extract_experience", _extract)
    monkeypatch.setattr("backend.cv_importer.experience_writer.merge_profiles", _merge)
    return box


def _upload(client, username: str, *, filename="cv.pdf", content_type="application/pdf", data=b"%PDF-1.4 fake"):
    return client.post(
        f"/api/profiles/{username}/cv-import",
        files={"file": (filename, io.BytesIO(data), content_type)},
    )


def _drain(db) -> int:
    return workflow_runner.drain(db)


def _scratch_is_empty(root, username: str) -> bool:
    scratch = root / username / service.SCRATCH_DIRNAME
    return not scratch.exists() or not any(scratch.iterdir())


# ── 1. Accept, poll, idempotency ─────────────────────────────────────────


def test_an_import_is_accepted_durably_and_completes_on_drain(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    res = _upload(client, username)
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["started"] is True
    assert body["cv_import"]["status"] == "processing"
    assert body["run"]["type"] == "cv_import"

    assert _drain(db) == 1

    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"
    assert status["cv_import"]["result"]["roles_count"] == 1
    assert status["run"]["status"] == "succeeded"

    profile_path = import_profiles / username / "profile.md"
    assert profile_path.exists()
    assert "Nora M" in profile_path.read_text(encoding="utf-8")


def test_the_status_route_never_carries_a_workflow_stage_before_polling(
    client, actors, import_profiles, stub_extraction
):
    """The response is metadata/progress only — no profile content, ever."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    res = _upload(client, username)
    body = res.json()
    assert "identity" not in str(body)
    assert "career_history" not in str(body)


def test_a_duplicate_upload_joins_the_run_in_flight_rather_than_duplicating_it(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    first = _upload(client, username)
    assert first.status_code == 202
    run_id = first.json()["run"]["id"]

    # Same bytes again, before the run has been drained — the double-click /
    # retried-POST case.
    second = _upload(client, username)
    assert second.status_code == 200
    assert second.json()["started"] is False
    assert second.json()["run"]["id"] == run_id

    assert _drain(db) == 1  # exactly one run executed, not two


def test_a_different_upload_while_one_is_in_flight_is_refused(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    first = _upload(client, username)
    assert first.status_code == 202

    second = _upload(client, username, data=b"%PDF-1.4 a genuinely different file")
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]

    assert _drain(db) == 1  # only the first request's run exists


def test_retrying_after_a_finished_import_is_a_genuine_new_attempt(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    first = _upload(client, username)
    first_run_id = first.json()["run"]["id"]
    assert _drain(db) == 1

    second = _upload(client, username, data=b"%PDF-1.4 an updated CV")
    assert second.status_code == 202
    assert second.json()["started"] is True
    assert second.json()["run"]["id"] != first_run_id
    assert second.json()["cv_import"]["attempt"] == 1

    assert _drain(db) == 1
    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"


def test_a_crash_between_the_profile_write_and_the_success_commit_does_not_duplicate_the_merge(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R1` finding 1. A SIGKILL between `write_profile()` landing on
    disk and `commit_import_success_once()` committing leaves `profile.md`
    already merged while the row still reads `processing`, with no live lock
    holder left — the flock only coordinates *live* contenders and is released
    by the OS the instant its holder's process dies. Recovery invokes the same
    key again; without a durable, on-disk record of "this exact attempt's
    merge already happened", it would call `write_profile` a second time.

    Simulated by performing exactly what the crashed process's write stage
    would have done — write `profile.md` with this attempt's marker — without
    going through the handler at all, so nothing commits the row and nothing
    deletes the source upload, matching what a real SIGKILL actually leaves
    behind (a Python `finally` block never runs on a real crash, so raising an
    exception inside the handler to simulate one would be dishonest: it would
    still delete the upload and could still record a failure, neither of which
    a real crash does).

    Asserted by a call counter on `write_profile`, not by counting duplicated
    content: `profile_merger.merge_profiles` is an LLM instructed to recognise
    a role group that already exists and update it rather than duplicate it
    (`profile_merger.py`'s MERGE RULES), so even a *faithful* stub — let alone
    the real model — could plausibly re-merge the same extraction without
    visibly duplicating anything. Relying on that semantic judgement is
    exactly the weaker guarantee this fix replaces with a deterministic one:
    the assertion that matters is that recovery does not invoke the merge a
    second time at all, and `profile.md` is provably untouched by it.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    started = _upload(client, username).json()
    run_id = started["run"]["id"]
    cv_import_id = started["cv_import"]["id"]

    row = db.get(CVImport, uuid.UUID(cv_import_id))
    key = ImportKey(
        cv_import_id=row.id, input_hash=row.input_hash, attempt=row.attempt
    )
    asyncio.run(
        experience_writer.write_profile(
            stub_extraction["data"],
            source_tmp_path=None,
            source_filename=None,
            username=username,
            import_marker=key.format(),
        )
    )
    db.expire(row)
    assert row.status == "processing"  # the crash never reached the commit
    assert row.source_ref and (import_profiles / username / service.SCRATCH_DIRNAME).exists()
    profile_path = import_profiles / username / "profile.md"
    written_by_the_crash = profile_path.read_bytes()

    write_calls = {"count": 0}
    real_write_profile = handler.write_profile

    async def _counting_write_profile(*args, **kwargs):
        write_calls["count"] += 1
        return await real_write_profile(*args, **kwargs)

    monkeypatch.setattr(handler, "write_profile", _counting_write_profile)

    # Recovery: a fresh invocation of the *same* run, exactly as the runner's
    # at-least-once-invocation contract requires after a restart.
    run = db.get(WorkflowRun, uuid.UUID(run_id))
    handler.handle_cv_import(db, run)

    assert write_calls["count"] == 0, (
        "recovery re-invoked write_profile instead of recognising the "
        "on-disk marker from the crashed attempt"
    )
    db.expire(row)
    assert row.status == "succeeded"
    assert row.result_roles_count == 1
    assert profile_path.read_bytes() == written_by_the_crash, (
        "profile.md changed during recovery even though write_profile was "
        "not supposed to run again"
    )
    assert _scratch_is_empty(import_profiles, username)


def _stick_a_finalize_commit(client, db, username, monkeypatch):
    """Drive one import into the `S4-P1-R2` state and return the POST body.

    On return: `profile.md` is written and carries this attempt's marker, the
    workflow run is terminally `failed`, and the `cv_imports` row is still
    `processing` — the honest state, since the content is already correct and
    only the bookkeeping is unfinished.

    Through the real runner (`run_once`), never a direct handler call: the
    runner is what claims the run and marks it terminally failed, and a run
    left `queued` would make the retry *join* it instead of reaching the
    reconciliation these tests are about.
    """
    body = _upload(client, username).json()
    real_commit = service.commit_import_success_once

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transient commit failure")

    monkeypatch.setattr(service, "commit_import_success_once", _boom)
    assert workflow_runner.run_once(db) is True
    monkeypatch.setattr(service, "commit_import_success_once", real_commit)
    return body


def test_a_reconciled_stuck_attempt_reports_a_consistent_run_and_import_status(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R4`. Reconciliation finalizes the row, and the run for that same
    attempt is still terminally `failed` from when the handler raised.
    `routes._envelope` looks the run up by the row's own key, so leaving it
    that way ships a failed run beside a succeeded import — on the POST
    response *and* on every later status poll. The editor happens to treat
    `cv_import.status` as the terminal authority and would not notice; an
    operator, another client, or the workflow audit trail would be simply
    misinformed. The effect is committed and the marker proves it, so the run
    is upgraded rather than left contradicting the row.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    stuck = _stick_a_finalize_commit(client, db, username, monkeypatch)
    run_id = stuck["run"]["id"]
    assert stuck["run"]["status"] in {"queued", "running"}

    body = _upload(client, username).json()  # the supported retry
    assert body["started"] is False
    assert body["cv_import"]["status"] == "succeeded"
    assert body["run"] is not None, (
        "the envelope lost the run entirely; the poller has nothing to read"
    )
    assert body["run"]["status"] == "succeeded", (
        "the POST envelope reports a failed run beside a succeeded import"
    )
    assert body["run"]["id"] == run_id, (
        "the stuck attempt's own run must be upgraded, not replaced by a new one"
    )
    assert body["run"]["result_ref"], "an upgraded run carries what it produced"

    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"
    assert status["run"]["status"] == "succeeded", (
        "the durable status read still contradicts itself after the response did not"
    )
    assert status["run"]["error_code"] is None


def test_a_death_at_the_reconciliation_boundary_cannot_split_the_row_from_its_run(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R5`. Reconciliation finalizes two records — the `cv_imports` row
    and that attempt's `workflow_runs` row — and `routes._envelope` serves both
    on every poll. While they were two separate commits, a process death
    between them durably left a `succeeded` import beside a `failed` run, which
    is the very contradiction `S4-P1-R4` set out to remove; it just moved into
    a narrower window. They are now one transaction.

    Interrupts at the boundary by failing the *second* commit this request
    makes. There is only one commit in the reconciliation now, so nothing is
    interrupted and both records finalize; while there were two, the second was
    the run upgrade and the interruption left the pair disagreeing. The
    assertion is the invariant itself rather than a particular outcome — the
    pair must never be observed one-finalized-one-not, however the request
    ends.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    _stick_a_finalize_commit(client, db, username, monkeypatch)
    row = db.query(CVImport).one()
    key = ImportKey(
        cv_import_id=row.id, input_hash=row.input_hash, attempt=row.attempt
    )

    source_path, input_hash = service.save_upload(
        username, filename="cv.pdf", content_type="application/pdf",
        data=b"%PDF-1.4 fake",
    )
    real_commit = db.commit
    commits = {"count": 0}

    def _die_on_the_second_commit():
        commits["count"] += 1
        if commits["count"] == 2:
            raise RuntimeError("process death at the transaction boundary")
        return real_commit()

    monkeypatch.setattr(db, "commit", _die_on_the_second_commit)
    try:
        service.request_import(
            db, username, actor=actors.consultant.user, source_path=source_path,
            source_filename="cv.pdf", content_type="application/pdf",
            input_hash=input_hash,
        )
    except RuntimeError:
        pass  # the simulated death; the invariant below is what matters
    finally:
        monkeypatch.undo()

    db.rollback()
    row = db.query(CVImport).one()
    run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.idempotency_key == key.format())
    )
    assert (row.status == "succeeded") == (run.status == "succeeded"), (
        f"a poll can observe the import {row.status!r} while its own run is "
        f"{run.status!r} -- the two must finalize in one transaction or not "
        f"at all"
    )


def test_a_failed_finalize_commit_does_not_cause_a_later_attempt_to_merge_again(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R2` finding 1, corrected by `S4-P1-R3`. Unlike the crash above,
    the process does not die here: `write_profile()` succeeds and durably
    lands the marker, but `commit_import_success_once` itself raises (a
    transient DB error) — `handle_cv_import` runs its normal `except`/
    `finally` machinery. Recording this as an ordinary import failure would be
    actively wrong: it would flip the row to `failed`, and the row would then
    look like nothing had happened even though `profile.md` already carries
    the merge.

    An early fix matched the marker on `(cv_import_id, input_hash)` alone,
    ignoring `attempt`, inside the *handler* — but that also matched a
    legitimate re-upload of the same bytes after a *successful* prior import,
    silently skipping a merge that should have happened (`S4-P1-R3` finding 1,
    see the test below). The corrected design keeps the handler's own marker
    check an exact `(id, hash, attempt)` match, and instead has
    `service.request_import` recognise this specific case — a `processing`
    row with no active run, requested again with the same content — and
    reconcile that *exact* stuck attempt directly, under its own unchanged
    key: no new attempt, no new run, no merge.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    first = _upload(client, username)
    run_id = first.json()["run"]["id"]
    cv_import_id = first.json()["cv_import"]["id"]
    row = db.get(CVImport, uuid.UUID(cv_import_id))

    real_commit = service.commit_import_success_once

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transient commit failure")

    monkeypatch.setattr(service, "commit_import_success_once", _boom)

    # Through the real runner, not a direct handler call: `run_once` is what
    # claims the run (queued -> running) and marks it terminally `failed`
    # after the handler raises. Calling the handler directly would leave the
    # run stuck `queued`, which would make the retry below wrongly "join" it
    # instead of exercising the genuine-new-attempt path this finding is
    # about.
    assert workflow_runner.run_once(db) is True

    db.expire(row)
    assert row.status == "processing", (
        "a finalize-commit failure must not be recorded as an import failure "
        "-- the content is already correct on disk"
    )
    run_row = db.get(WorkflowRun, uuid.UUID(run_id))
    db.expire(run_row)
    assert run_row.status == "failed"  # the run itself is terminal either way
    profile_path = import_profiles / username / "profile.md"
    written_once = profile_path.read_bytes()
    assert b"_last_import_key" in written_once

    # The supported retry: a fresh upload of the same file. request_import
    # recognises the stuck attempt (same content, row still `processing`, no
    # active run) and reconciles it directly -- no new attempt, no new run,
    # no merge, and therefore no wasted extraction/LLM call either.
    monkeypatch.setattr(service, "commit_import_success_once", real_commit)
    write_calls = {"count": 0}
    real_write_profile = handler.write_profile

    async def _counting_write_profile(*args, **kwargs):
        write_calls["count"] += 1
        return await real_write_profile(*args, **kwargs)

    monkeypatch.setattr(handler, "write_profile", _counting_write_profile)

    second = _upload(client, username)
    assert second.json()["started"] is False, (
        "a reconciled stuck attempt must not report a freshly started import"
    )
    assert second.json()["cv_import"]["attempt"] == 0, (
        "reconciliation finalizes the stuck attempt's own number; it must "
        "not be bumped as if a new attempt had been created"
    )
    assert second.json()["cv_import"]["status"] == "succeeded"
    assert _drain(db) == 0, "no workflow run should have been enqueued at all"

    assert write_calls["count"] == 0, (
        "the retry re-invoked write_profile instead of reconciling the "
        "stuck attempt directly"
    )
    db.expire(row)
    assert row.status == "succeeded"
    assert row.attempt == 0
    assert profile_path.read_bytes() == written_once
    assert _scratch_is_empty(import_profiles, username)


def test_a_superseded_attempt_can_neither_complete_nor_fail_the_current_one(
    client, actors, db, import_profiles, stub_extraction
):
    """A late invocation of an attempt that has since been superseded — the
    runner's at-least-once contract permits it — must change nothing. Both of
    this service's terminal writes are guarded by the same
    `(input_hash, attempt, status)` predicate rather than a prior read, so
    neither the success nor the failure path can reach a row that has moved
    on. `tests/test_tailored_cv.py::test_a_superseded_attempt_cannot_overwrite_the_current_one`
    is the same proof at the tailored-CV boundary; CV import had none until
    this packet's failure-mode table asked what proved the guard.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _upload(client, username)
    assert _drain(db) == 1

    row = db.query(CVImport).one()
    stale = ImportKey(
        cv_import_id=row.id, input_hash="a-superseded-hash", attempt=row.attempt
    )

    _reloaded, performed = service.commit_import_success_once(
        db, stale, result={"success": True, "roles_count": 99, "skills_count": 99}
    )
    assert performed is False
    db.expire(row)
    assert row.status == "succeeded"
    assert row.result_roles_count != 99, "a superseded attempt overwrote the winner"

    assert service.record_import_failure(db, stale, code="late", message="x") is False
    db.expire(row)
    assert row.status == "succeeded", "a superseded attempt failed a finished import"


def test_a_legitimate_reupload_after_success_still_merges_into_the_changed_profile(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R3` finding 1. The `S4-P1-R2` fix, in its first form, matched a
    stuck attempt's marker on `(cv_import_id, input_hash)` alone from inside
    the handler — which also matched this entirely different, legitimate
    case: a consultant's first import **succeeds**, their profile then changes
    by some other means (a manual edit, a chat profile patch), and they
    re-upload the identical CV file expecting it to merge into the
    *now-current* profile — exactly what `docs/plans/v2-pilot.md`'s own retry
    contract promises ("a retry after a finished import is a genuine new
    attempt"). Matching by content hash alone cannot tell "this exact stuck
    attempt's own write already landed" apart from "this content happens to
    be identical to some unrelated, already-*finished* import" — the fixed
    design never even asks the question unless the row is still `processing`
    with no active run, which a **successful** prior import is not.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    first = _upload(client, username)
    assert _drain(db) == 1
    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"

    # The profile changes by some other means before the re-upload.
    profile_path = import_profiles / username / "profile.md"
    before_reupload = profile_path.read_text(encoding="utf-8")
    assert "Nora M" in before_reupload
    profile_path.write_text(
        before_reupload.replace("Nora M", "Nora M (edited elsewhere)"),
        encoding="utf-8",
    )
    edited_bytes = profile_path.read_bytes()

    write_calls = {"count": 0}
    real_write_profile = handler.write_profile

    async def _counting_write_profile(*args, **kwargs):
        write_calls["count"] += 1
        return await real_write_profile(*args, **kwargs)

    monkeypatch.setattr(handler, "write_profile", _counting_write_profile)

    second = _upload(client, username)  # identical bytes as the first upload
    assert second.status_code == 202
    assert second.json()["started"] is True
    assert second.json()["cv_import"]["attempt"] == 1
    assert _drain(db) == 1

    assert write_calls["count"] == 1, (
        "a legitimate re-upload after a successful prior import must merge "
        "again, not be silently skipped as already done"
    )
    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"
    assert status["cv_import"]["attempt"] == 1
    assert profile_path.read_bytes() != edited_bytes, (
        "the second import must actually write a new profile.md, not leave "
        "the (stale, manually edited) one in place"
    )


def test_an_unreadable_profile_lets_a_stuck_attempt_fall_through_to_a_new_one(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """Reconciling a stuck attempt reads `profile.md` from inside
    `request_import` — a route that never touched that file before
    `S4-P1-R3`. A corrupt or unreadable document says nothing about whether
    the stuck attempt's write landed, and must not turn every subsequent
    upload of that file into a 500 the consultant can never get past: the
    request falls through to a genuine new attempt, whose handler records a
    bounded failure if it cannot read the file either.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    _upload(client, username)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transient commit failure")

    monkeypatch.setattr(service, "commit_import_success_once", _boom)
    assert workflow_runner.run_once(db) is True  # leaves the row `processing`

    profile_path = import_profiles / username / "profile.md"
    profile_path.write_text("not a profile document at all", encoding="utf-8")

    second = _upload(client, username)
    assert second.status_code == 202, second.text
    assert second.json()["started"] is True
    assert second.json()["cv_import"]["attempt"] == 1


# ── 2. The transient upload is always deleted ────────────────────────────


def test_the_uploaded_file_is_deleted_after_a_successful_import(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _upload(client, username)
    _drain(db)
    assert _scratch_is_empty(import_profiles, username)


def test_the_uploaded_file_is_deleted_when_extraction_fails(
    client, actors, db, import_profiles, stub_extraction
):
    stub_extraction["data"] = None
    stub_extraction["errors"] = [{"kind": "unparseable", "context": "secret raw CV text"}]
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    _upload(client, username)
    _drain(db)

    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "failed"
    assert status["cv_import"]["error_code"] == "extraction_failed"
    # The bounded reason is a stable slug, never the free-text `context`.
    assert "secret raw CV text" not in str(status)

    assert _scratch_is_empty(import_profiles, username)
    assert not (import_profiles / username / "profile.md").exists()


def test_a_redundant_duplicate_uploads_own_file_is_deleted_immediately(
    client, actors, db, import_profiles, stub_extraction
):
    """Joining an in-flight run deletes *this request's* upload right away —
    the in-flight run already owns its own `source_ref`."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _upload(client, username)
    _upload(client, username)  # the duplicate

    scratch = import_profiles / username / service.SCRATCH_DIRNAME
    assert len(list(scratch.iterdir())) == 1  # only the first upload remains


def test_the_uploaded_file_is_deleted_even_when_accepting_the_request_fails_unexpectedly(
    client, actors, db, import_profiles, monkeypatch
):
    """`S4-P1-R1` finding 2. An operational failure inside `request_import`
    (not one of the expected `CVImportServiceError` refusals) used to escape
    the route with the freshly saved upload never cleaned up — nothing durable
    was created for any handler to ever find and delete it through. The route
    must delete this request's own upload on *any* failure, not only the
    validation/conflict ones, and must not mask the original error while doing
    so."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated enqueue/commit failure")

    monkeypatch.setattr(service, "request_import", _boom)

    with pytest.raises(RuntimeError, match="simulated enqueue/commit failure"):
        _upload(client, username)

    assert _scratch_is_empty(import_profiles, username)
    assert db.query(CVImport).count() == 0
    assert db.query(WorkflowRun).count() == 0


def test_a_failed_reference_clear_commit_does_not_strand_the_session_or_the_run(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R8` finding 2. `finish_upload_cleanup`'s reference-clear commit
    (`clear_source_ref_if_matches`) can itself fail after the file has already
    been deleted — a transient DB error, not a crash. SQLAlchemy does not
    auto-rollback a Session after a failed commit; it stays unusable
    (`db.is_active is False`, the next statement raises `InvalidRequestError`)
    until something calls `rollback()`. Swallowing that failure without doing
    so left the shared Session poisoned for whoever used it next —
    `run_once()`'s own `mark_succeeded(db, run, ...)`, called right after the
    handler returns, on the *same* Session — stranding an already-succeeded
    import's workflow at `running` forever.

    Forces a genuine commit failure via SQLAlchemy's engine-level `commit`
    event (matching the review's own reproduction) rather than a bare
    monkeypatched raise, because a plain Python exception with no real
    database-level fault would not actually leave the Session in the
    `PendingRollbackError` state this finding is about — the fix (or its
    absence) would be indistinguishable either way. The handler makes several
    other commits first (`claim_next`, two `record_stage` progress writes,
    `commit_import_success_once`), so the failure is armed only around the one
    call this test targets — a context flag, not a fragile ordinal count of
    "the Nth commit", which would silently mistarget the moment any of those
    other calls changes.
    """
    from sqlalchemy import event

    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _upload(client, username)

    engine = db.get_bind()
    armed = {"active": False}
    real_clear = service.clear_source_ref_if_matches

    def _clear_wrapper(db_, cv_import_id, source_ref):
        armed["active"] = True
        try:
            return real_clear(db_, cv_import_id, source_ref)
        finally:
            armed["active"] = False

    def _fail_while_armed(conn):
        if armed["active"]:
            raise RuntimeError("simulated reference-clear commit failure")

    monkeypatch.setattr(service, "clear_source_ref_if_matches", _clear_wrapper)
    event.listen(engine, "commit", _fail_while_armed)
    try:
        assert workflow_runner.run_once(db) is True
    finally:
        event.remove(engine, "commit", _fail_while_armed)

    row = db.query(CVImport).one()
    assert row.status == "succeeded", "the business effect must survive"
    run = service.current_run(db, row)
    assert run.status == "succeeded", (
        "the workflow must not strand at `running` because a best-effort "
        "cleanup commit failed after the business effect already landed"
    )
    # The regression itself: a poisoned Session raises right here, before any
    # assertion about its content even runs.
    assert db.query(CVImport).count() == 1


def test_an_unlink_failure_leaves_the_reference_discoverable_and_is_retried_on_next_touch(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R7`. `commit_import_success_once` no longer clears `source_ref`
    itself — only a *confirmed* delete does, via `finish_upload_cleanup`. If
    the handler's own unlink call raises (an EFS hiccup, not "already gone"),
    the row stays `succeeded` with `source_ref` still naming the file: durably
    discoverable, rather than the old design's silent, permanent orphan. The
    very next request for this consultant — any content — retries that delete
    before starting its own new attempt, so the common case recovers without
    waiting for the daily sweep.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    real_unlink = Path.unlink
    calls = {"count": 0}

    def _flaky_unlink(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated EFS unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)
    _upload(client, username)
    assert _drain(db) == 1

    row = db.query(CVImport).one()
    assert row.status == "succeeded"
    assert row.source_ref, "a failed delete must leave the reference in place"
    stranded = Path(row.source_ref)
    assert stranded.exists(), "the file the reference names must still be there"

    # Restore only this — not `monkeypatch.undo()`, which would also revert
    # the `import_profiles` fixture's own `config.DATA_DIR` patch (the same
    # shared `monkeypatch` instance) and misroute the next upload's file.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    second = _upload(client, username)
    assert second.json()["started"] is True
    assert not stranded.exists(), (
        "the next request for this consultant did not retry the stale delete"
    )


def test_a_crash_after_the_terminal_commit_leaves_a_recoverable_reference(
    client, actors, db, import_profiles, stub_extraction
):
    """`S4-P1-R7`. A real SIGKILL between the terminal commit and the
    handler's `finally` never runs `finally` at all — simulated here the same
    way the dead-sibling crash regression above does, by performing the
    crashed process's own terminal effect directly rather than raising inside
    the handler (a raise would still run `finally`, which a real crash does
    not). `source_ref` must still name the file afterward: nothing else can
    recover it if the reference is already gone.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    res = _upload(client, username)
    cv_import_id = uuid.UUID(res.json()["cv_import"]["id"])
    row = db.query(CVImport).one()
    source_ref = row.source_ref
    assert source_ref and Path(source_ref).exists()

    key = ImportKey(cv_import_id=cv_import_id, input_hash=row.input_hash, attempt=0)
    service.commit_import_success_once(
        db, key, result={"success": True, "roles_count": 1, "skills_count": 1}
    )
    # No `finally` ever ran for this "crash" -- the file is untouched.
    db.expire(row)
    assert row.status == "succeeded"
    assert row.source_ref == source_ref, (
        "a crash right after the terminal commit must not have already lost "
        "the reference -- it was never cleared by the commit in the first place"
    )
    assert Path(source_ref).exists()


def test_reconciling_a_stuck_attempt_also_retries_its_own_stranded_upload(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """`S4-P1-R7`. When the stuck attempt's *own* handler invocation could not
    delete its file (still stranded, `source_ref` still populated), the
    reconciliation that later finalizes that attempt must retry the delete
    too -- not just leave it for the daily sweep. This is a different code
    path from the ordinary stuck-commit case
    (`test_a_failed_finalize_commit_does_not_cause_a_later_attempt_to_merge_again`):
    there, the handler's own `finally` already deletes the file successfully
    before the commit failure is even reached, so `source_ref` is already
    clear by the time reconciliation runs and this second retry is a no-op.
    Here, unlink fails *inside that same invocation*, so the file is still
    there and still referenced when the next request reconciles it.
    """
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    real_unlink = Path.unlink
    real_commit = service.commit_import_success_once

    def _fail_unlink(self, *args, **kwargs):
        raise OSError("simulated EFS unlink failure")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(Path, "unlink", _fail_unlink)
    monkeypatch.setattr(service, "commit_import_success_once", _boom)
    _upload(client, username)
    assert _drain(db) == 1

    row = db.query(CVImport).one()
    assert row.status == "processing"
    stranded = Path(row.source_ref)
    assert stranded.exists()

    # Restore only these two — not `monkeypatch.undo()`, which would also
    # revert the `import_profiles` fixture's own `config.DATA_DIR` patch
    # (same shared `monkeypatch` instance) and silently misroute every
    # subsequent file read to the real profile directory.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(service, "commit_import_success_once", real_commit)
    second = _upload(client, username)
    assert second.json()["started"] is False
    assert second.json()["cv_import"]["status"] == "succeeded"
    assert not stranded.exists(), (
        "reconciliation did not retry the stuck attempt's own stranded upload"
    )


# ── 3. Validation and safety ─────────────────────────────────────────────


def test_an_unsupported_file_type_is_refused_before_anything_is_saved(
    client, actors, import_profiles
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    res = client.post(
        f"/api/profiles/{username}/cv-import",
        files={"file": ("cv.txt", io.BytesIO(b"plain text"), "text/plain")},
    )
    assert res.status_code == 400
    scratch = import_profiles / username / service.SCRATCH_DIRNAME
    assert not scratch.exists() or not any(scratch.iterdir())


def test_a_markdown_upload_is_accepted_regardless_of_content_type(
    client, actors, db, import_profiles, stub_extraction
):
    """S8/F-025: `.md` is admitted by extension, not content-type — browsers
    report it inconsistently (`text/markdown`, `text/plain`,
    `application/octet-stream`, or empty)."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    res = _upload(
        client, username,
        filename="cv.md", content_type="application/octet-stream",
        data="# Nora M\n\nEngineer at Volvo.".encode("utf-8"),
    )
    assert res.status_code == 202, res.text
    assert _drain(db) == 1
    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"


def test_a_markdown_extension_upload_is_accepted(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    res = _upload(
        client, username,
        filename="cv.markdown", content_type="text/plain",
        data="# Nora M\n\nEngineer at Volvo.".encode("utf-8"),
    )
    assert res.status_code == 202, res.text
    assert _drain(db) == 1
    status = client.get(f"/api/profiles/{username}/cv-import/status").json()
    assert status["cv_import"]["status"] == "succeeded"


def test_a_non_utf8_file_renamed_to_md_is_refused_before_anything_is_saved(
    client, actors, import_profiles
):
    """Mirrors `test_an_unsupported_file_type_is_refused_before_anything_is_saved`'s
    assert-nothing-written shape: a binary file renamed to `.md` fails the
    UTF-8 decode gate and is refused before `dest.write_bytes` (G10)."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    res = client.post(
        f"/api/profiles/{username}/cv-import",
        files={"file": ("cv.md", io.BytesIO(b"\xff\xfe\x00\x01binary garbage"), "text/markdown")},
    )
    assert res.status_code == 400
    scratch = import_profiles / username / service.SCRATCH_DIRNAME
    assert not scratch.exists() or not any(scratch.iterdir())


def test_an_oversized_file_is_refused(client, actors, import_profiles, monkeypatch):
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 10)
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    res = _upload(client, username, data=b"x" * 100)
    assert res.status_code == 400


# ── 4. Authorization ──────────────────────────────────────────────────────


def test_the_related_ta_may_import_for_their_consultant(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    actors.login(client, actors.ta)
    res = _upload(client, actors.consultant.username)
    assert res.status_code == 202
    assert _drain(db) == 1


def test_an_unrelated_ta_is_refused(client, actors, import_profiles, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    actors.login(client, actors.other_ta)
    res = _upload(client, actors.consultant.username)
    assert res.status_code == 403


def test_admin_may_import_for_any_consultant(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    actors.login(client, actors.admin)
    res = _upload(client, actors.consultant.username)
    assert res.status_code == 202
    assert _drain(db) == 1


def test_site_is_never_read_on_the_import_path(
    client, actors, db, import_profiles, stub_extraction, monkeypatch
):
    """If Site were an access input, moving the consultant to another Site row
    would start refusing these routes (P-005 / `docs/DECISIONS.md`)."""
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    other = Site(code="OTHER", name="Elsewhere", is_active=False)
    db.add(other)
    db.commit()
    user = db.get(User, actors.consultant.user_id)
    user.site_id = other.id
    db.commit()

    assert _upload(client, username).status_code == 202
    assert client.get(f"/api/profiles/{username}/cv-import/status").status_code == 200


# ── 5. Preview surfaces imported facts without duplicating them ─────────


def test_the_preview_route_serves_current_profile_content_after_success(
    client, actors, db, import_profiles, stub_extraction
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _upload(client, username)
    _drain(db)

    res = client.get(f"/api/profiles/{username}/cv-import/preview")
    assert res.status_code == 200
    assert res.json()["form_data"]["iName"] == "Nora M"


def test_the_preview_route_404s_before_any_import_exists(
    client, actors, import_profiles
):
    actors.login(client, actors.consultant)
    res = client.get(f"/api/profiles/{actors.consultant.username}/cv-import/preview")
    assert res.status_code == 404


# ── 6. Structural: no stored workflow-run reference ──────────────────────


def test_the_cv_import_row_stores_no_reference_to_its_workflow_run(db):
    """Mirrors `tailored_cvs`'s equivalent guard — see
    `backend/cv_importer/service.py::current_run` for why: a stored reference
    can only be written after `enqueue` commits, reopening the exact window
    that let a concurrent tailored-CV request start a duplicate generation
    before Slice 3 removed the column entirely."""
    columns = {c.name for c in CVImport.__table__.columns}
    assert not any("run" in name for name in columns)
