"""The Application-owned tailored CV (V2 Slice 3, P-009 / P-006).

What this file has to prove, in the order the risk sits:

1. **The base CV never moves.** `docs/plans/v2-pilot.md` §7 asks for exactly
   this, "including in the fast test gate", because it is the correctness
   boundary the whole slice exists for.
2. **Generation is durable and idempotent.** Accepted as a row plus a run, not
   a held-open connection; a retry joins the run in flight rather than
   producing a second document; a retry *after* a finished run is a real new
   attempt rather than a silent no-op.
3. **The inputs are frozen.** Editing `profile.md` after a generation is
   approved cannot change what that generation produces.
4. **Stale, cross-artifact, and unauthorized writes fail** — the last of them
   without disclosing anything.

The LLM is stubbed at `handler.generate_cv_content`, so everything below it —
`cv_content_to_form_data`, `build_cv_state`, the atomic effect — runs for real.
Nothing here reaches the network.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy import event

from backend import config
from backend.cv_state import service as cv_state_service
from backend.db.models import Application, TailoredCV, User, WorkflowRun
from backend.tailored_cv import handler, service
from backend.workflows import runner as workflow_runner

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """An isolated profile root, wired everywhere the generation path reads it.

    Four bindings because three modules `from backend.config import DATA_DIR`
    at import time; patching only `config` would leave them pointed at the real
    root and the test would silently exercise nothing.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.profile_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.matching.config_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.cv_pipeline.context_assembler.DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def profile_for(profiles):
    """Give a consultant the profile + style a generation needs."""

    def _install(
        username: str, *, headline: str = "Embedded work", photo: bool = True
    ) -> None:
        root = profiles / username
        root.mkdir(parents=True, exist_ok=True)
        (root / "profile.md").write_text(
            "# Profile\n\n```yaml\n"
            + yaml.dump({"summary": headline, "career_history": {}})
            + "```\n",
            encoding="utf-8",
        )
        (root / "style.md").write_text(
            "## Rules\n- Keep it terse.\n", encoding="utf-8"
        )
        if photo:
            # Generation refuses a consultant with no photo (V3-P-023).
            base = cv_state_service.load_base(username)
            saved = cv_state_service.save_base(
                username,
                {**base.state, "photoSrc": f"/api/profiles/{username}/photo"},
                expected_revision=base.revision,
            )
            assert saved.state.get("photoSrc") == f"/api/profiles/{username}/photo"

    return _install


#: What the stubbed LLM returns. A valid cv-content document, so the real
#: mapping and state builder run against a real shape.
def _generated(job_title: str = "Embedded Engineer") -> dict:
    return {
        "header": {
            "full_name": "Nora M",
            "job_title": job_title,
            "contact": {"email": "nora@example.invalid"},
        },
        "summary": "Tailored summary.",
        "competencies": {
            "hard_skills": ["C", "ISO 26262"],
            "soft_skills": ["Coaching"],
            "tools": ["CAN"],
        },
        "experience": [
            {
                "organization": "Volvo",
                "role_title": "Engineer",
                "date_range": "2022 – Present",
                "relevant_items": ["AUTOSAR"],
                "bullets": ["Owned an ECU.", "Shipped it.", "Kept it alive."],
            }
        ],
        "education": [
            {"institution": "Chalmers", "qualification": "MSc", "years": "2018-2020"}
        ],
        "languages": [{"language": "English", "proficiency": "fluent"}],
    }


@pytest.fixture
def stub_llm(monkeypatch):
    """Stub only the model call. Everything below it runs for real.

    Returns a mutable dict so a test can change what the "model" produces, or
    make it fail, between generations.
    """
    box = {"document": _generated(), "error": None, "calls": 0}

    async def _generate(context):  # noqa: ARG001
        box["calls"] += 1
        box["last_context"] = context
        if box["error"] is not None:
            raise box["error"]
        if box["document"] is None:
            return None, [{"kind": "validation_error", "context": "secret prompt text"}]
        return box["document"], []

    monkeypatch.setattr(handler, "generate_cv_content", _generate)
    return box


_description_nonce = itertools.count()


def _distinct_filler() -> str:
    """Five never-repeating tokens, appended to every generated description so
    two calls can never collide as a Tier 1/2 near-duplicate of each other
    (`backend/jobs/service.py::find_near_duplicate`) — a bare title swap (e.g.
    "First position" vs "Second position") is not enough difference on its
    own to clear that check by itself."""
    start = next(_description_nonce) * 5
    return " ".join(f"ref{i}" for i in range(start, start + 5))


def _apply(client, actors, *, title: str = "Senior Embedded Engineer") -> dict:
    """Create a Job for the consultant and Apply. Returns the Application."""
    created = client.post(
        "/api/jobs",
        json={
            "consultant_username": actors.consultant.username,
            "title": title,
            "description": f"We need an embedded engineer. {title}. {_distinct_filler()}. " * 4,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["id"]
    applied = client.post(
        "/api/applications",
        json={"job_id": job_id, "consultant_username": actors.consultant.username},
    )
    assert applied.status_code == 201, applied.text
    return applied.json()["application"]


def _drain(db) -> int:
    """Execute every queued run synchronously — the runner's testable core."""
    return workflow_runner.drain(db)


# ── 1. The base CV never moves ────────────────────────────────────────────


def test_generating_and_editing_a_tailored_cv_never_moves_the_base_cv(
    client, actors, db, profile_for, stub_llm
):
    """**The** boundary this slice exists for (P-009), in the fast gate as the
    plan (§7) requires.

    Captures the base CV's revision *and* hash, runs the whole tailored path —
    generate, execute, edit, regenerate — and asserts both are unmoved and the
    content is byte-identical. A regression here is the V1 behaviour returning:
    generating for a position overwrote the consultant's own CV.
    """
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    seeded = cv_state_service.load_base(username)
    base_state = {
        "iName": "Nora",
        "iRole": "My own CV",
        "iFtText": "Mpya",
        "photoSrc": seeded.state["photoSrc"],
    }
    put = client.put(
        f"/api/cv/base/{username}",
        json={"state": base_state, "expected_revision": seeded.revision},
    )
    assert put.status_code == 200, put.text
    before = cv_state_service.load_base(username)
    assert before.revision == seeded.revision + 1

    application = _apply(client, actors)
    app_id = application["id"]

    assert client.post(f"/api/applications/{app_id}/cv/generate").status_code == 202
    assert _drain(db) == 1

    loaded = client.get(f"/api/applications/{app_id}/cv").json()
    client.put(
        f"/api/applications/{app_id}/cv",
        json={
            "state": {**loaded["artifact"]["state"], "iName": "Tailored name"},
            "expected_revision": loaded["artifact"]["revision"],
            "tailored_cv_id": loaded["tailored_cv"]["id"],
        },
    )
    assert client.post(f"/api/applications/{app_id}/cv/generate").status_code == 202
    _drain(db)

    after = cv_state_service.load_base(username)
    assert after.revision == before.revision
    assert after.content_hash == before.content_hash
    assert after.state == base_state


def test_the_tailored_cv_takes_presentation_from_the_base_without_writing_it(
    client, actors, db, profile_for, stub_llm
):
    """A tailored CV should look like the consultant's CV — same fonts, footer,
    photo — which means *copying* those in, from the snapshot. The copy is what
    keeps it a copy: nothing reads the base file during generation, so nothing
    can write it either."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    seeded = cv_state_service.load_base(actors.consultant.username)
    put = client.put(
        f"/api/cv/base/{actors.consultant.username}",
        json={
            "state": {
                "iName": "Nora",
                "pfFont": "Georgia",
                "iFtText": "Mpya Sci Tech",
                "photoSrc": "/api/profiles/nora/photo",
            },
            "expected_revision": seeded.revision,
        },
    )
    assert put.status_code == 200, put.text
    app_id = _apply(client, actors)["id"]
    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)

    state = client.get(f"/api/applications/{app_id}/cv").json()["artifact"]["state"]
    assert state["pfFont"] == "Georgia"
    assert state["iFtText"] == "Mpya Sci Tech"
    assert state["photoSrc"] == "/api/profiles/nora/photo"
    # …and the generated content genuinely replaced the base content.
    assert state["iName"] == "Nora M"
    assert state["iRole"] == "Embedded Engineer"
    assert state["skills"]["main"] == ["C", "ISO 26262"]


# ── 2. Durable, idempotent generation ─────────────────────────────────────


def test_generation_is_accepted_as_a_row_and_a_run_not_a_held_connection(
    client, actors, db, profile_for, stub_llm
):
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]

    r = client.post(f"/api/applications/{app_id}/cv/generate")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["started"] is True
    assert body["tailored_cv"]["status"] == "generating"
    assert body["tailored_cv"]["has_content"] is False
    assert body["run"]["status"] == "queued"
    # Nothing was generated yet: acceptance is the commit, not the work.
    assert stub_llm["calls"] == 0

    assert _drain(db) == 1
    status = client.get(f"/api/applications/{app_id}/cv/status").json()
    assert status["tailored_cv"]["status"] == "ready"
    assert status["tailored_cv"]["revision"] == 1
    assert status["run"]["status"] == "succeeded"
    assert status["run"]["progress"] == 100


def test_a_repeated_request_joins_the_run_in_flight_and_starts_nothing(
    client, actors, db, profile_for, stub_llm
):
    """The double-clicked button and the POST retried after the 60-second edge
    (`docs/evidence/m1-timeout-inventory.md`) — the case P-006 exists for."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]

    first = client.post(f"/api/applications/{app_id}/cv/generate")
    second = client.post(f"/api/applications/{app_id}/cv/generate")
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["started"] is False
    assert first.json()["run"]["id"] == second.json()["run"]["id"]

    assert db.query(TailoredCV).count() == 1
    assert db.query(WorkflowRun).count() == 1
    assert _drain(db) == 1
    assert stub_llm["calls"] == 1
    assert db.query(TailoredCV).one().revision == 1


def test_a_retry_after_a_finished_run_is_a_real_new_attempt(
    client, actors, db, profile_for, stub_llm
):
    """Without `generation_attempt` in the idempotency key this is the silent
    dead end: the second request would collide with the finished run's key,
    `enqueue` would hand that run back, and nothing would ever execute again."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]

    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)

    again = client.post(f"/api/applications/{app_id}/cv/generate")
    assert again.status_code == 202
    assert again.json()["started"] is True
    assert db.query(WorkflowRun).count() == 2
    assert db.query(TailoredCV).count() == 1          # still one artifact
    assert _drain(db) == 1
    assert stub_llm["calls"] == 2

    tailored = db.query(TailoredCV).one()
    assert tailored.revision == 2
    assert tailored.generation_attempt == 1


def test_a_failed_generation_keeps_the_previous_cv_and_says_what_failed(
    client, actors, db, profile_for, stub_llm
):
    """A transient model failure during a *re*generation must not cost the
    consultant the document they already had."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]

    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)
    good = client.get(f"/api/applications/{app_id}/cv").json()["artifact"]

    stub_llm["error"] = RuntimeError("model said no: prompt was <secret CV text>")
    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)

    after = client.get(f"/api/applications/{app_id}/cv").json()
    assert after["tailored_cv"]["status"] == "failed"
    assert after["tailored_cv"]["revision"] == good["revision"]
    # Byte-for-byte, *including the envelope's `updated_at`*. Recording the
    # failure is a row write, so reporting the row's `updated_at` here would
    # tell the client the CV changed when it demonstrably did not — which is
    # why the artifact reports `content_changed_at` instead. Found by the
    # pilot demo, which compared the whole artifact rather than its state.
    assert after["artifact"] == good
    # Bounded and safe: the class name, never the exception's text, which can
    # embed the prompt or the CV (`workflows.service._safe_error`).
    assert after["tailored_cv"]["error_code"] == "unexpected_error"
    assert after["tailored_cv"]["error_message"] == "RuntimeError"
    assert "secret CV text" not in str(after)
    assert after["run"]["error_message"] == "RuntimeError"


def test_a_rejected_generation_records_only_the_error_kind(
    client, actors, db, profile_for, stub_llm
):
    """The generator's `context` can quote the model's raw output — which can
    contain the prompt or the consultant's CV. Only the stable `kind` slug is
    persisted."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]

    stub_llm["document"] = None
    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)

    tailored = client.get(
        f"/api/applications/{app_id}/cv/status"
    ).json()["tailored_cv"]
    assert tailored["status"] == "failed"
    assert tailored["error_code"] == "generation_invalid"
    assert tailored["error_message"] == "validation_error"
    assert "secret prompt text" not in str(tailored)


def test_a_re_queued_run_does_not_produce_a_second_document(
    client, actors, db, profile_for, stub_llm
):
    """The runner's contract is at-least-once *invocation*, exactly-once
    *effect*. Re-queue a succeeded run's work by hand and execute it again: the
    conditional UPDATE finds no row to change, so the revision does not move."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)

    tailored = db.query(TailoredCV).one()
    assert tailored.revision == 1

    run = db.query(WorkflowRun).one()
    run.status = "queued"
    db.commit()
    assert _drain(db) == 1

    db.refresh(tailored)
    assert tailored.revision == 1
    # It did not even call the model: it observed that it was not the current
    # generation and returned.
    assert stub_llm["calls"] == 1


def test_a_superseded_attempt_cannot_overwrite_the_current_one(
    db, actors, client, profile_for, stub_llm
):
    """A slow attempt still executing beside a newer one must change nothing.

    Driven at the effect boundary rather than through two threads, because the
    guard *is* the predicate on the UPDATE — the Postgres file races the real
    invocations.
    """
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)

    tailored = db.query(TailoredCV).one()
    stale = service.GenerationKey(
        tailored_cv_id=tailored.id,
        input_hash="a-superseded-snapshot-hash",
        attempt=tailored.generation_attempt,
    )
    _, performed = service.commit_generated_state_once(
        db, stale, {"iName": "From a superseded attempt"}
    )
    assert performed is False
    db.refresh(tailored)
    assert tailored.revision == 1
    assert tailored.cv_state["iName"] == "Nora M"


def test_the_artifact_stores_no_reference_to_its_workflow_run():
    """Structural, and in the fast gate so it never skips.

    The run is found by the idempotency key built from
    `(id, input_hash, generation_attempt)`. A stored `generation_run_id` could
    only be written *after* the enqueue committed, and a second concurrent
    request that took the artifact's lock in that window read a null reference,
    concluded nothing was in flight, and started a duplicate generation. The
    Postgres file races that; this keeps the column from coming back.
    """
    assert not any(
        "run" in column.name for column in TailoredCV.__table__.columns
    ), {c.name for c in TailoredCV.__table__.columns}


def test_a_crash_before_the_enqueue_does_not_strand_the_application(
    client, actors, db, profile_for, stub_llm
):
    """`status == 'generating'` alone must not bar every later request.

    A process that died between marking the artifact and enqueuing its run
    leaves it claiming to generate with nothing behind it. Refusing forever
    would strand the Application; "in flight" therefore means a run exists *and*
    is not terminal.
    """
    from backend.db.models import TailoredCVStatus

    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    client.post(f"/api/applications/{app_id}/cv/generate")

    # Simulate the crash: the artifact says generating, its run never existed.
    tailored = db.query(TailoredCV).one()
    tailored.status = TailoredCVStatus.generating.value
    db.query(WorkflowRun).delete()
    db.commit()

    recovered = client.post(f"/api/applications/{app_id}/cv/generate")
    assert recovered.status_code == 202
    assert recovered.json()["started"] is True
    assert _drain(db) == 1
    assert client.get(
        f"/api/applications/{app_id}/cv/status"
    ).json()["tailored_cv"]["status"] == "ready"


def test_the_generation_key_round_trips_and_rejects_anything_else():
    key = service.GenerationKey(
        tailored_cv_id=uuid.uuid4(), input_hash="a" * 64, attempt=3
    )
    assert service.parse_generation_key(key.format()) == key
    assert len(key.format()) <= 200          # the idempotency_key column's cap
    for junk in ("", "probe-result:x", "tailored_cv:not-a-uuid:h:0", None):
        assert service.parse_generation_key(junk) is None


# ── 3. The inputs are frozen ──────────────────────────────────────────────


def test_generation_uses_the_snapshot_not_whatever_the_disk_holds_later(
    client, actors, db, profile_for, stub_llm
):
    """The handler runs minutes later, in another process, possibly twice. A
    profile edit landing in between must not change a document whose inputs a
    human already approved."""
    profile_for(actors.consultant.username, headline="Approved profile")
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]

    client.post(f"/api/applications/{app_id}/cv/generate")
    profile_for(actors.consultant.username, headline="Edited after approval")

    _drain(db)
    assert "Approved profile" in stub_llm["last_context"]["profile_yaml"]
    assert "Edited after approval" not in stub_llm["last_context"]["profile_yaml"]


def test_deleting_the_profile_after_enqueue_does_not_break_the_generation(
    client, actors, db, profiles, profile_for, stub_llm
):
    """The other half of the same property: the handler opens no file at all."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    client.post(f"/api/applications/{app_id}/cv/generate")

    (profiles / actors.consultant.username / "profile.md").unlink()
    assert _drain(db) == 1
    assert client.get(
        f"/api/applications/{app_id}/cv/status"
    ).json()["tailored_cv"]["status"] == "ready"


def test_the_snapshot_carries_no_timestamp(
    client, actors, db, profile_for, stub_llm
):
    """A clock reading in the hashed snapshot would make every double-click a
    different generation, turning the idempotent path into a 409."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    client.post(f"/api/applications/{app_id}/cv/generate")

    application = db.get(Application, uuid.UUID(app_id))
    first = service.snapshot_hash(service.build_input_snapshot(db, application))
    second = service.snapshot_hash(service.build_input_snapshot(db, application))
    assert first == second


def test_a_consultant_with_no_profile_gets_an_honest_409(
    client, actors, db, profiles, stub_llm
):
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    r = client.post(f"/api/applications/{app_id}/cv/generate")
    assert r.status_code == 409
    assert "Import a CV first" in r.json()["detail"]
    # Nothing was accepted, so nothing sits queued pretending to work.
    assert db.query(WorkflowRun).count() == 0
    assert db.query(TailoredCV).count() == 0


def test_a_consultant_with_no_photo_gets_an_honest_409(
    client, actors, db, profile_for, stub_llm
):
    """V3-P-023: a profile without a photo is refused before generation, with a
    message distinct from the missing-profile one."""
    profile_for(actors.consultant.username, photo=False)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    r = client.post(f"/api/applications/{app_id}/cv/generate")
    assert r.status_code == 409
    assert r.json()["detail"] == service.NO_PHOTO_MESSAGE
    assert "Import a CV first" not in r.json()["detail"]
    assert db.query(WorkflowRun).count() == 0
    assert db.query(TailoredCV).count() == 0


def test_an_inline_data_url_photo_is_not_a_photo(
    client, actors, db, profile_for, stub_llm
):
    username = actors.consultant.username
    profile_for(username, photo=False)
    base = cv_state_service.load_base(username)
    cv_state_service.save_base(
        username,
        {**base.state, "photoSrc": "data:image/png;base64,AAAA"},
        expected_revision=base.revision,
    )
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    r = client.post(f"/api/applications/{app_id}/cv/generate")
    assert r.status_code == 409
    assert r.json()["detail"] == service.NO_PHOTO_MESSAGE


# ── 4. Stale, cross-artifact, and unauthorized writes ─────────────────────


def _generate_and_load(client, db, app_id) -> dict:
    client.post(f"/api/applications/{app_id}/cv/generate")
    _drain(db)
    return client.get(f"/api/applications/{app_id}/cv").json()


def test_editing_bumps_the_revision_and_a_stale_edit_is_refused(
    client, actors, db, profile_for, stub_llm
):
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored = loaded["artifact"], loaded["tailored_cv"]

    saved = client.put(
        f"/api/applications/{app_id}/cv",
        json={
            "state": {**artifact["state"], "iSummary": "Edited by hand"},
            "expected_revision": artifact["revision"],
            "tailored_cv_id": tailored["id"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["artifact"]["revision"] == artifact["revision"] + 1
    assert saved.json()["artifact"]["state"]["iSummary"] == "Edited by hand"

    stale = client.put(
        f"/api/applications/{app_id}/cv",
        json={
            "state": {**artifact["state"], "iSummary": "From a stale tab"},
            "expected_revision": artifact["revision"],
            "tailored_cv_id": tailored["id"],
        },
    )
    assert stale.status_code == 409
    assert client.get(
        f"/api/applications/{app_id}/cv"
    ).json()["artifact"]["state"]["iSummary"] == "Edited by hand"


# ── Content-identical saves (S22, V3-P-022/V3-P-033) ──────────────────────

_LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _age_the_row(db, tailored_id: str, *, by) -> None:
    """Backdate `content_changed_at` and attribute the row to someone else.

    Both are what a write would move, and a value nobody would write is what
    makes "unchanged" evidence: back-to-back saves can share a timestamp
    without either being a no-op, so comparing two fresh ones proves nothing.
    """
    row = db.get(TailoredCV, uuid.UUID(tailored_id))
    row.content_changed_at = _LONG_AGO
    row.updated_by_id = by
    db.commit()


def _fresh_row(db, tailored_id: str) -> TailoredCV:
    db.expire_all()
    return db.get(TailoredCV, uuid.UUID(tailored_id))


@pytest.fixture
def tailored_updates(db):
    """Every UPDATE statement against `tailored_cvs`, from any session."""
    seen: list[str] = []

    def _record(conn, cursor, statement, *args):  # noqa: ARG001
        if statement.lstrip().upper().startswith("UPDATE TAILORED_CVS"):
            seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine, "before_cursor_execute", _record)


def _put(client, app_id, state, revision, tailored_id):
    return client.put(
        f"/api/applications/{app_id}/cv",
        json={"state": state, "expected_revision": revision,
              "tailored_cv_id": tailored_id},
    )


def test_an_identical_autosave_moves_nothing_and_a_real_edit_still_does(
    client, actors, db, profile_for, stub_llm, tailored_updates
):
    """The owner's symptom (revision 4 → 10 with no edit), for the tailored CV.
    A no-op also leaves `content_changed_at` (the artifact's reported
    `updated_at`) and `updated_by_id` alone."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored_id = loaded["artifact"], loaded["tailored_cv"]["id"]
    _age_the_row(db, tailored_id, by=actors.admin.user_id)
    stored = client.get(f"/api/applications/{app_id}/cv").json()["artifact"]
    assert stored["updated_at"].startswith("2020-01-01")
    tailored_updates.clear()

    for _tick in range(3):
        again = _put(client, app_id, artifact["state"], artifact["revision"], tailored_id)
        assert again.status_code == 200, again.text
        assert again.json()["artifact"] == stored

    assert tailored_updates == []
    row = _fresh_row(db, tailored_id)
    assert row.revision == artifact["revision"]
    assert row.content_hash == artifact["content_hash"]
    assert row.content_changed_at.replace(tzinfo=timezone.utc) == _LONG_AGO
    assert row.updated_by_id == actors.admin.user_id

    edited = _put(client, app_id, {**artifact["state"], "iSummary": "Edited by hand"},
                  artifact["revision"], tailored_id)
    assert edited.status_code == 200, edited.text
    assert edited.json()["artifact"]["revision"] == artifact["revision"] + 1
    row = _fresh_row(db, tailored_id)
    assert row.content_changed_at.replace(tzinfo=timezone.utc) > _LONG_AGO
    assert row.updated_by_id == actors.consultant.user_id


def test_an_identical_save_with_a_stale_revision_is_still_refused(
    client, actors, db, profile_for, stub_llm
):
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored_id = loaded["artifact"], loaded["tailored_cv"]["id"]
    edited = _put(client, app_id, {**artifact["state"], "iSummary": "Second tab"},
                  artifact["revision"], tailored_id).json()["artifact"]
    # Back to the original content, so the stale tab's copy is identical again.
    current = _put(client, app_id, artifact["state"], edited["revision"],
                   tailored_id).json()["artifact"]

    stale = _put(client, app_id, artifact["state"], artifact["revision"], tailored_id)
    assert stale.status_code == 409
    assert "changed by someone else" in stale.json()["detail"]
    assert _fresh_row(db, tailored_id).revision == current["revision"]


def test_an_identical_save_keeps_the_earlier_refusals(
    client, actors, db, profile_for, stub_llm
):
    """Order unchanged: wrong-artifact, then mid-generation, then stale, then
    the no-op. Identical content does not get past either earlier refusal."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    first = _apply(client, actors, title="First position")["id"]
    second = _apply(client, actors, title="Second position")["id"]
    a = _generate_and_load(client, db, first)
    b = _generate_and_load(client, db, second)

    crossed = _put(client, second, b["artifact"]["state"], b["artifact"]["revision"],
                   a["tailored_cv"]["id"])
    assert crossed.status_code == 409
    assert "different application" in crossed.json()["detail"]

    client.post(f"/api/applications/{first}/cv/generate")     # queued, not run
    during = _put(client, first, a["artifact"]["state"], a["artifact"]["revision"],
                  a["tailored_cv"]["id"])
    assert during.status_code == 409
    assert "Wait for it to finish" in during.json()["detail"]


def test_a_missing_stored_hash_counts_as_changed(
    client, actors, db, profile_for, stub_llm
):
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored_id = loaded["artifact"], loaded["tailored_cv"]["id"]
    row = db.get(TailoredCV, uuid.UUID(tailored_id))
    row.content_hash = None
    db.commit()

    saved = _put(client, app_id, artifact["state"], artifact["revision"], tailored_id)
    assert saved.status_code == 200, saved.text
    assert saved.json()["artifact"]["revision"] == artifact["revision"] + 1
    assert saved.json()["artifact"]["content_hash"] == artifact["content_hash"]


def test_a_no_op_save_ends_its_transaction(
    client, actors, db, profile_for, stub_llm
):
    """The row lock taken by `lock_tailored` lasts as long as the transaction.
    The no-op path assigns nothing, so it has nothing to commit — and must
    still end the transaction rather than hold the lock until teardown.
    (`tests/integration/test_tailored_cv_pg.py` proves the lock itself is free.)
    """
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored_id = loaded["artifact"], loaded["tailored_cv"]["id"]

    db.rollback()
    returned = service.save_state(
        db,
        db.get(TailoredCV, uuid.UUID(tailored_id)),
        state=artifact["state"],
        expected_revision=artifact["revision"],
        expected_id=tailored_id,
        actor=db.get(User, actors.consultant.user_id),
    )
    assert not db.in_transaction()
    assert returned.revision == artifact["revision"]


def _no_op_save(db, actors, artifact, tailored_id):
    db.rollback()
    return service.save_state(
        db,
        db.get(TailoredCV, uuid.UUID(tailored_id)),
        state=artifact["state"],
        expected_revision=artifact["revision"],
        expected_id=tailored_id,
        actor=db.get(User, actors.consultant.user_id),
    )


def test_a_no_op_reply_carries_the_values_read_under_the_lock(
    client, actors, db, profile_for, stub_llm, Session
):
    """The lock is released before the route builds its reply. If the reply
    re-read the row, a real edit committed in that gap would hand this client
    the *other* writer's revision, and its next autosave — still holding the
    old content — would pass the stale check and overwrite that edit. Found
    in review (S22, Codex finding 1, sharpened)."""
    from backend.tailored_cv.routes import _envelope

    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored_id = loaded["artifact"], loaded["tailored_cv"]["id"]

    returned = _no_op_save(db, actors, artifact, tailored_id)

    other = Session()
    try:
        row = other.get(TailoredCV, uuid.UUID(tailored_id))
        row.cv_state = {**artifact["state"], "iSummary": "Another tab's edit"}
        row.revision = artifact["revision"] + 1
        other.commit()
    finally:
        other.close()

    body = _envelope(db, returned, include_state=True)
    assert body["artifact"]["revision"] == artifact["revision"]
    assert body["artifact"]["state"] == artifact["state"]


def test_a_no_op_reply_survives_the_row_being_deleted_meanwhile(
    client, actors, db, profile_for, stub_llm, Session
):
    """A terminal Application transition deletes its tailored CV; one landing
    between the lock release and the reply must not turn an accepted no-op
    into a 500 (`ObjectDeletedError`). Codex finding 1."""
    from backend.tailored_cv.routes import _envelope

    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    artifact, tailored_id = loaded["artifact"], loaded["tailored_cv"]["id"]

    returned = _no_op_save(db, actors, artifact, tailored_id)

    other = Session()
    try:
        other.delete(other.get(TailoredCV, uuid.UUID(tailored_id)))
        other.commit()
    finally:
        other.close()

    body = _envelope(db, returned, include_state=True)
    assert body["tailored_cv"]["id"] == tailored_id
    assert body["artifact"]["revision"] == artifact["revision"]


def test_an_edit_addressed_at_another_applications_cv_is_refused(
    client, actors, db, profile_for, stub_llm
):
    """The case neither authorization nor the URL alone catches: both artifacts
    are legitimately this consultant's, and a stale tab is one click from
    saving one position's CV over another's."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    first = _apply(client, actors, title="First position")["id"]
    second = _apply(client, actors, title="Second position")["id"]
    a = _generate_and_load(client, db, first)
    b = _generate_and_load(client, db, second)

    crossed = client.put(
        f"/api/applications/{second}/cv",
        json={
            "state": {**a["artifact"]["state"], "iSummary": "First position's CV"},
            "expected_revision": b["artifact"]["revision"],
            "tailored_cv_id": a["tailored_cv"]["id"],     # the *other* artifact
        },
    )
    assert crossed.status_code == 409
    assert "different application" in crossed.json()["detail"]
    unchanged = client.get(f"/api/applications/{second}/cv").json()
    assert unchanged["artifact"] == b["artifact"]


def test_an_edit_during_a_generation_is_refused_rather_than_silently_lost(
    client, actors, db, profile_for, stub_llm
):
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    client.post(f"/api/applications/{app_id}/cv/generate")     # queued, not run

    r = client.put(
        f"/api/applications/{app_id}/cv",
        json={
            "state": {**loaded["artifact"]["state"], "iSummary": "Doomed"},
            "expected_revision": loaded["artifact"]["revision"],
            "tailored_cv_id": loaded["tailored_cv"]["id"],
        },
    )
    assert r.status_code == 409
    assert "Wait for it to finish" in r.json()["detail"]


def test_a_terminal_application_cannot_start_a_generation_and_deletes_its_cv(
    client, actors, db, profile_for, stub_llm
):
    """Mirrors the closed-Job rule for *new* work: closing refuses a fresh
    generation. It does **not** mirror it for existing content — since Slice 5
    (`docs/plans/v2-pilot.md` §7, S5-P1-R1), a confirmed terminal Application
    action deletes the tailored CV and disables any public link atomically
    with the status move (`backend/applications/service.py::transition`,
    `backend/cv_publish/service.py::disable_for_application`). This
    supersedes Slice 3's original "keep it as a record of what was sent" —
    once the process is over, the tailored artifact and any capability link
    to it are retired together rather than lingering indefinitely.
    """
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    _generate_and_load(client, db, app_id)

    changed = client.post(f"/api/applications/{app_id}/status", json={"status": "closed"})
    assert changed.status_code == 200, changed.text

    refused = client.post(f"/api/applications/{app_id}/cv/generate")
    assert refused.status_code == 409
    assert "has ended" in refused.json()["detail"]
    # The artifact itself is gone, same 404 shape as one never generated.
    gone = client.get(f"/api/applications/{app_id}/cv")
    assert gone.status_code == 404
    assert "not been generated" in gone.json()["detail"]
    assert db.query(TailoredCV).filter_by(application_id=uuid.UUID(app_id)).first() is None


def test_a_missing_tailored_cv_is_404_with_an_actionable_reason(
    client, actors, profile_for, stub_llm
):
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    for path in (f"/api/applications/{app_id}/cv", f"/api/applications/{app_id}/cv/status"):
        r = client.get(path)
        assert r.status_code == 404
        assert "not been generated" in r.json()["detail"]


# ── Authorization: self / related TA / unrelated TA / Admin ───────────────


_MUTATIONS = ("generate", "read", "status", "save")


def _call(client, app_id, what, *, artifact=None, tailored=None):
    if what == "generate":
        return client.post(f"/api/applications/{app_id}/cv/generate")
    if what == "read":
        return client.get(f"/api/applications/{app_id}/cv")
    if what == "status":
        return client.get(f"/api/applications/{app_id}/cv/status")
    return client.put(
        f"/api/applications/{app_id}/cv",
        json={
            "state": {**(artifact or {}).get("state", {}), "iSummary": "x"},
            "expected_revision": (artifact or {}).get("revision", 0),
            "tailored_cv_id": (tailored or {}).get("id", str(uuid.uuid4())),
        },
    )


@pytest.mark.parametrize("what", _MUTATIONS)
def test_an_unrelated_ta_gets_404_and_learns_nothing(
    client, actors, db, profile_for, stub_llm, what, monkeypatch
):
    """404, not 403 — an Application's existence is content, and so is the fact
    that a CV was tailored for it."""
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors, title="A confidential posting")["id"]
    loaded = _generate_and_load(client, db, app_id)
    actors.logout(client)

    actors.login(client, actors.other_ta)
    r = _call(client, app_id, what,
              artifact=loaded["artifact"], tailored=loaded["tailored_cv"])
    assert r.status_code == 404, r.text
    body = r.text
    for secret in ("A confidential posting", actors.consultant.username,
                   loaded["tailored_cv"]["id"], "Nora M"):
        assert secret not in body


@pytest.mark.parametrize("what", _MUTATIONS)
def test_the_related_ta_and_the_admin_reach_every_route(
    client, actors, db, profile_for, stub_llm, what, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    loaded = _generate_and_load(client, db, app_id)
    actors.logout(client)

    for actor in (actors.ta, actors.admin):
        actors.login(client, actor)
        r = _call(client, app_id, what,
                  artifact=client.get(f"/api/applications/{app_id}/cv").json()["artifact"],
                  tailored=loaded["tailored_cv"])
        assert r.status_code in (200, 202), (actor.username, what, r.text)
        actors.logout(client)


def test_a_consultant_reaches_no_other_consultants_tailored_cv(
    client, actors, second_pair, db, profile_for, stub_llm, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    _generate_and_load(client, db, app_id)
    actors.logout(client)

    actors.login(client, second_pair["consultant"])
    assert client.get(f"/api/applications/{app_id}/cv").status_code == 404
    actors.logout(client)
    # …nor may that consultant's own TA, who has no relationship to this one.
    actors.login(client, second_pair["ta"])
    assert client.get(f"/api/applications/{app_id}/cv").status_code == 404


def test_the_tailored_cv_path_reads_no_site(client, actors, db, profile_for, stub_llm):
    """P-005: Site is configuration and display, never an access predicate. The
    complement of `tests/test_authz_fixture.py`'s policy check, for the routes
    this slice adds."""
    from backend.db.models import Site, User

    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    _generate_and_load(client, db, app_id)

    # Move the consultant to a different Site row. If Site were an access
    # input, every route below would start refusing.
    other = Site(code="OTHER", name="Elsewhere", is_active=False)
    db.add(other)
    db.commit()
    user = db.get(User, actors.consultant.user_id)
    user.site_id = other.id
    db.commit()

    assert client.get(f"/api/applications/{app_id}/cv").status_code == 200
    assert client.get(f"/api/applications/{app_id}/cv/status").status_code == 200


# ── Projections and error mapping ────────────────────────────────────────


def test_the_metadata_projection_never_carries_cv_content(
    client, actors, db, profile_for, stub_llm
):
    """A list of Applications must not become a way to read every tailored CV
    in it — the content is served one Application at a time."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    _generate_and_load(client, db, app_id)

    tailored = db.query(TailoredCV).one()
    projection = tailored.to_dict()
    assert "cv_state" not in projection
    assert "input_snapshot" not in projection

    listing = client.get("/api/applications").json()
    assert listing["applications"][0]["tailored_cv"]["revision"] == 1
    assert "Nora M" not in listing["applications"][0]["tailored_cv"].get("id", "")
    assert "cv_state" not in str(listing)


def test_the_status_route_never_ships_the_document(
    client, actors, db, profile_for, stub_llm
):
    """A client polling every two seconds must not be sent the whole CV each
    time, and a page that only needs "is it done" must not read one at all."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    _generate_and_load(client, db, app_id)

    body = client.get(f"/api/applications/{app_id}/cv/status").json()
    assert set(body) == {"tailored_cv", "run"}
    assert "Nora M" not in str(body)


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("post", "/cv/generate", None),
        ("get", "/cv", None),
        ("get", "/cv/status", None),
        ("put", "/cv", {"state": {}, "expected_revision": 0, "tailored_cv_id": "x"}),
    ],
)
def test_every_tailored_cv_route_maps_a_service_error_rather_than_500(
    client, actors, db, profile_for, stub_llm, monkeypatch, method, path, payload
):
    """The same guard `tests/test_jobs.py` carries, and for the same reason: the
    service's exceptions have no global handler, so a route that forgets the
    wrapper turns an intended 404/409 into a 500 with the status lost.

    Parametrized over every route that calls the service, so a route added later
    without the mapping fails here rather than in production.
    """
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors)["id"]
    _generate_and_load(client, db, app_id)

    boom = service.TailoredCVServiceError("gone", status=404)

    def _raise(*args, **kwargs):
        raise boom

    # Whichever service function this route reaches, make it raise.
    for name in ("request_generation", "save_state", "get_for_application"):
        monkeypatch.setattr(f"backend.tailored_cv.routes.service.{name}", _raise)

    r = getattr(client, method)(f"/api/applications/{app_id}{path}", **(
        {"json": payload} if payload is not None else {}
    ))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "gone"


def test_the_generation_run_row_holds_references_not_content(
    client, actors, db, profile_for, stub_llm
):
    """`workflow_runs` is the generic queue and stays reference-only (plan
    §5.2), even though the *domain* row it points at legitimately holds a CV."""
    profile_for(actors.consultant.username, headline="Distinctive profile text")
    actors.login(client, actors.consultant)
    app_id = _apply(client, actors, title="Distinctive job title")["id"]
    _generate_and_load(client, db, app_id)

    run = db.query(WorkflowRun).one()
    serialized = str(run.to_status_dict()) + str(run.input_ref) + str(run.idempotency_key)
    for content in ("Distinctive profile text", "Distinctive job title",
                    "Tailored summary.", "Nora M"):
        assert content not in serialized
    assert run.subject_type == "tailored_cv"
    assert run.subject_id == str(db.query(TailoredCV).one().id)
