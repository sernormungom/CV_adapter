"""The base CV artifact (V2 Slice 3, P-009): identity and stale-write refusal.

Two things are under test that V1's `GET/POST /api/profiles/{u}/cv-state` did
not have and could not be given:

**The base CV has an identity.** A revision and a content hash, so the slice can
*prove* that generating and editing a tailored CV never moves it
(`tests/test_tailored_cv.py::test_generating_and_editing_a_tailored_cv_never_moves_the_base_cv`
is where that proof lives; here we establish that the two numbers mean what that
test assumes).

**A write states what it is based on.** Two editor tabs, or a 30-second autosave
landing behind a newer save, used to overwrite each other silently. Now the
loser is refused.
"""

from __future__ import annotations

import json
import threading

import pytest

from backend import config
from backend.cv_state import service


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Point the base-CV store at a throwaway directory."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _state(name: str = "Nora") -> dict:
    return {"iName": name, "iRole": "Embedded Engineer", "skills": {"main": ["C"]}}


# ── Identity ──────────────────────────────────────────────────────────────


def test_a_consultant_with_no_saved_cv_reads_as_an_empty_revision_zero(profiles):
    artifact = service.load_base("nora")
    assert artifact.mode == "base"
    assert artifact.revision == 0
    assert artifact.state == {}
    # Not a 404: the editor's job is to let them create one.
    assert artifact.content_hash == service.content_hash({})


def test_saving_bumps_the_revision_and_records_the_content_hash(profiles):
    first = service.save_base("nora", _state(), expected_revision=0)
    assert first.revision == 1
    assert first.content_hash == service.content_hash(_state())

    reloaded = service.load_base("nora")
    assert reloaded.revision == 1
    assert reloaded.content_hash == first.content_hash
    assert reloaded.state == _state()

    second = service.save_base("nora", _state("Nora M"), expected_revision=1)
    assert second.revision == 2
    assert second.content_hash != first.content_hash


def test_the_identity_never_leaks_into_the_state_a_client_sees(profiles):
    service.save_base("nora", _state(), expected_revision=0)
    on_disk = json.loads((profiles / "nora" / "cv_state.json").read_text("utf-8"))
    # It is stored in the same file — one store, nothing to drift.
    assert service.IDENTITY_KEY in on_disk
    # And it is never handed back as content, so a client cannot round-trip it
    # into the CV as if it were a form field.
    assert service.IDENTITY_KEY not in service.load_base("nora").state


def test_a_client_cannot_forge_an_identity_by_sending_one_as_content(profiles):
    # The second save carries genuinely different content (S22): an identical
    # one is now a no-op, which would make "revision 2" unreachable and this
    # test about the no-op rather than about the forged identity.
    service.save_base("nora", _state(), expected_revision=0)
    forged = dict(_state("Nora M"))
    forged[service.IDENTITY_KEY] = {"revision": 99, "content_hash": "nonsense"}
    saved = service.save_base("nora", forged, expected_revision=1)
    assert saved.revision == 2                      # not 100, not 99
    assert saved.content_hash == service.content_hash(_state("Nora M"))
    assert service.IDENTITY_KEY not in saved.state


def test_identical_content_with_a_forged_identity_is_a_no_op(profiles):
    """The forged key is stripped before hashing, so it cannot make an
    otherwise identical save look like a change — nor smuggle in a revision."""
    service.save_base("nora", _state(), expected_revision=0)
    forged = dict(_state())
    forged[service.IDENTITY_KEY] = {"revision": 99, "content_hash": "nonsense"}
    saved = service.save_base("nora", forged, expected_revision=1)
    assert saved.revision == 1
    assert saved.content_hash == service.content_hash(_state())
    assert service.load_base("nora").revision == 1


def test_a_file_written_before_this_module_existed_reads_at_revision_zero(profiles):
    """The seeded pilot profiles predate the identity and must keep working.

    `docs/plans/v2-pilot.md` §1.1 validates `cv_state.json` as one of the four
    canonical base-profile artifacts; a change that made those files unreadable
    would break the seed check for no gain.
    """
    root = profiles / "legacy"
    root.mkdir(parents=True)
    (root / "cv_state.json").write_text(json.dumps(_state()), encoding="utf-8")

    artifact = service.load_base("legacy")
    assert artifact.revision == 0
    assert artifact.state == _state()
    # And it acquires an identity on its first save rather than needing a migration.
    assert service.save_base("legacy", _state("Edited"), expected_revision=0).revision == 1


def test_a_file_edited_outside_the_app_reports_its_actual_content_hash(profiles):
    """The content is authoritative over a recorded hash.

    Reporting the stored hash for a file someone changed by hand would tell a
    client nothing had changed when it had.
    """
    service.save_base("nora", _state(), expected_revision=0)
    path = profiles / "nora" / "cv_state.json"
    document = json.loads(path.read_text("utf-8"))
    document["iName"] = "Edited by hand"
    path.write_text(json.dumps(document), encoding="utf-8")

    artifact = service.load_base("nora")
    assert artifact.content_hash == service.content_hash(artifact.state)


def test_the_hash_ignores_key_order_but_not_content(profiles):
    """It answers "is this the same CV", not "did the client serialize it the
    same way this time" — otherwise every save would look like a change."""
    assert service.content_hash({"a": 1, "b": 2}) == service.content_hash({"b": 2, "a": 1})
    assert service.content_hash({"a": 1}) != service.content_hash({"a": 2})


# ── Stale writes ──────────────────────────────────────────────────────────


def test_a_write_based_on_a_stale_revision_is_refused(profiles):
    service.save_base("nora", _state(), expected_revision=0)
    service.save_base("nora", _state("Second tab"), expected_revision=1)

    with pytest.raises(service.CVStateError) as exc:
        # The first tab still believes it is at revision 1.
        service.save_base("nora", _state("First tab"), expected_revision=1)
    assert exc.value.status == 409

    # The refusal is what matters: the winner's content survived intact.
    assert service.load_base("nora").state["iName"] == "Second tab"


# ── Content-identical saves (S22, V3-P-022/V3-P-033) ──────────────────────


def _age_the_stored_identity(profiles, username: str) -> dict:
    """Backdate the stored `updated_at`, so an unchanged value proves no write.

    Comparing two `updated_at`s from back-to-back saves proves nothing: they are
    stamped to the second, so a write in the same second as the first save
    leaves the value equal and the assertion passes vacuously.
    """
    path = profiles / username / "cv_state.json"
    document = json.loads(path.read_text("utf-8"))
    document[service.IDENTITY_KEY]["updated_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")
    return document[service.IDENTITY_KEY]


def test_an_identical_autosave_writes_nothing_and_a_real_edit_still_does(
    profiles, monkeypatch
):
    """The owner's symptom: the revision climbed on every 30-second autosave
    tick with no edit at all. An identical save is now a no-op — nothing
    written, the stored artifact handed back — and an edit still bumps."""
    service.save_base("nora", _state(), expected_revision=0)
    identity = _age_the_stored_identity(profiles, "nora")
    path = profiles / "nora" / "cv_state.json"
    bytes_before, mtime_before = path.read_bytes(), path.stat().st_mtime_ns

    writes: list = []
    real_write = service._atomic_write_json
    monkeypatch.setattr(
        service, "_atomic_write_json",
        lambda *a, **k: (writes.append(a), real_write(*a, **k)),
    )

    for _tick in range(3):
        again = service.save_base("nora", _state(), expected_revision=1)
        assert again.revision == 1
        assert again.content_hash == identity["content_hash"]
        assert again.updated_at == "2020-01-01T00:00:00+00:00"

    assert writes == []
    assert path.read_bytes() == bytes_before
    assert path.stat().st_mtime_ns == mtime_before

    edited = service.save_base("nora", _state("Nora M"), expected_revision=1)
    assert edited.revision == 2
    assert edited.content_hash == service.content_hash(_state("Nora M"))
    assert edited.updated_at != "2020-01-01T00:00:00+00:00"
    assert len(writes) == 1


def test_an_identical_save_with_a_stale_revision_is_still_refused(profiles):
    """Stale first: the no-op applies only at the current revision, so an
    out-of-date tab is told to reload even when it happens to hold the same
    content."""
    service.save_base("nora", _state(), expected_revision=0)
    service.save_base("nora", _state("Second tab"), expected_revision=1)
    service.save_base("nora", _state(), expected_revision=2)   # back to the original

    with pytest.raises(service.CVStateError) as exc:
        service.save_base("nora", _state(), expected_revision=1)
    assert exc.value.status == 409
    assert service.load_base("nora").revision == 3


def test_an_identical_save_over_http_returns_the_stored_artifact(
    client, actors, profiles
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    first = client.put(
        f"/api/cv/base/{username}",
        json={"state": _state(), "expected_revision": 0},
    )
    assert first.status_code == 200, first.text
    _age_the_stored_identity(profiles, username)
    stored = client.get(f"/api/cv/base/{username}").json()

    again = client.put(
        f"/api/cv/base/{username}",
        json={"state": _state(), "expected_revision": 1},
    )
    assert again.status_code == 200, again.text
    assert again.json() == stored


def test_a_partial_write_cannot_destroy_the_stored_cv(profiles, monkeypatch):
    """The write is a temp file plus a rename. The previous implementation
    truncated the real file before serializing, so a failure mid-dump left the
    consultant with a corrupt CV rather than the previous one."""
    service.save_base("nora", _state(), expected_revision=0)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    # A nested context, not `monkeypatch.undo()`: undo reverts *every* patch on
    # this fixture, including the `profiles` redirect, and the test would then
    # read an empty real root and pass for the wrong reason.
    with monkeypatch.context() as failing:
        failing.setattr(json, "dump", _boom)
        with pytest.raises(OSError):
            service.save_base("nora", _state("Never written"), expected_revision=1)

    assert service.load_base("nora").state == _state()
    assert service.load_base("nora").revision == 1
    # No temporary file was left behind. Named explicitly rather than as an
    # exact directory listing: the compare-and-write lock file is expected to be
    # there, and a listing equality would have to be loosened for every future
    # sibling artifact, which is how a leaked temp file stops being noticed.
    leftovers = [p.name for p in (profiles / "nora").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert (profiles / "nora" / "cv_state.json").exists()


def test_two_writers_at_the_same_revision_cannot_both_win(profiles):
    """The revision check and the write are one operation (`S3-P1-R1` finding 2).

    The first version of this module checked the revision, then wrote. Two
    writers that both loaded revision 0 both found their `expected_revision`
    current and both wrote revision 1: the file ended up holding one of the two
    CVs, and **both** callers were told they had saved. A silently discarded CV
    edit reported as success is a worse outcome than the overwrite this
    mechanism was added to prevent, because nobody can even know to redo it.

    The barrier is what makes this deterministic rather than a race the test
    might lose: both threads are held until both have loaded, so both genuinely
    believe revision 0 is current before either is allowed to write.
    """
    service.save_base("nora", _state("Original"), expected_revision=0)
    start = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def writer(name: str) -> None:
        loaded = service.load_base("nora")
        assert loaded.revision == 1
        start.wait(timeout=10)
        try:
            saved = service.save_base("nora", _state(name), expected_revision=1)
            outcome: object = saved.revision
        except service.CVStateError as exc:
            outcome = exc.status
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=writer, args=(name,))
        for name in ("Writer A", "Writer B")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "a writer deadlocked on the base-CV lock"

    # Exactly one write landed and exactly one was refused.
    assert sorted(results, key=str) == [2, 409], results

    # And the file agrees: one revision bump, holding one of the two writers'
    # content in full rather than a blend of both.
    final = service.load_base("nora")
    assert final.revision == 2
    assert final.state["iName"] in {"Writer A", "Writer B"}
    assert final.content_hash == service.content_hash(final.state)


def test_unreadable_stored_state_is_a_409_not_a_500(profiles):
    root = profiles / "nora"
    root.mkdir(parents=True)
    (root / "cv_state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(service.CVStateError) as exc:
        service.load_base("nora")
    assert exc.value.status == 409


# ── The HTTP surface ──────────────────────────────────────────────────────


def test_the_base_cv_round_trips_over_http(client, actors, profiles):
    actors.login(client, actors.consultant)
    username = actors.consultant.username

    loaded = client.get(f"/api/cv/base/{username}")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json() == {
        "mode": "base",
        "id": username,
        "revision": 0,
        "content_hash": service.content_hash({}),
        "updated_at": None,
        "state": {},
    }

    saved = client.put(
        f"/api/cv/base/{username}",
        json={"state": _state(), "expected_revision": 0},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1

    assert client.get(f"/api/cv/base/{username}").json()["state"] == _state()


def test_a_stale_write_over_http_is_409_and_keeps_the_winner(
    client, actors, profiles
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    client.put(
        f"/api/cv/base/{username}",
        json={"state": _state(), "expected_revision": 0},
    )
    stale = client.put(
        f"/api/cv/base/{username}",
        json={"state": _state("Stale"), "expected_revision": 0},
    )
    assert stale.status_code == 409
    assert client.get(f"/api/cv/base/{username}").json()["state"] == _state()


def test_a_write_must_state_a_revision(client, actors, profiles):
    actors.login(client, actors.consultant)
    r = client.put(
        f"/api/cv/base/{actors.consultant.username}", json={"state": _state()}
    )
    # Unprocessable, not "assume 0": a default would turn "this client has not
    # thought about concurrency" into "overwrite whatever is there".
    assert r.status_code == 422


def test_the_v1_cv_state_routes_are_gone(client, actors, profiles):
    """They were unversioned (any write won) and lived in `backend/main.py`,
    which the plan (§5) says composes routers and lifecycle only."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    assert client.get(f"/api/profiles/{username}/cv-state").status_code == 404
    assert client.post(
        f"/api/profiles/{username}/cv-state", json=_state()
    ).status_code == 404


def test_a_related_ta_may_read_and_write_a_consultants_base_cv(
    client, actors, profiles
):
    actors.login(client, actors.ta)
    username = actors.consultant.username
    assert client.get(f"/api/cv/base/{username}").status_code == 200
    assert client.put(
        f"/api/cv/base/{username}",
        json={"state": _state(), "expected_revision": 0},
    ).status_code == 200


def test_an_unrelated_ta_reaches_no_base_cv(client, actors, profiles, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    actors.login(client, actors.other_ta)
    username = actors.consultant.username
    assert client.get(f"/api/cv/base/{username}").status_code == 403
    assert client.put(
        f"/api/cv/base/{username}",
        json={"state": _state(), "expected_revision": 0},
    ).status_code == 403


def test_an_admin_reaches_any_base_cv(client, actors, profiles, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    actors.login(client, actors.admin)
    assert client.get(f"/api/cv/base/{actors.consultant.username}").status_code == 200
