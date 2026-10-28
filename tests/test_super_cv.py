"""Lossless SuperCV profile artifacts and stale-save refusal."""

from __future__ import annotations

import copy

import pytest
import yaml

from backend import config, profile_reader
from backend.cv_assistant import profile_utils
from backend.super_cv import service


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Bind every profile reader and the shared profile lock to one test root."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile_utils, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile_reader, "DATA_DIR", tmp_path)
    return tmp_path


def _document(full_name: str = "Nora M") -> dict:
    return {
        "metadata": {
            "schema_version": "1.3",
            "consultant_id": "nora",
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            "as_of": "2026-09",
            "_last_import_key": "cv_import:example",
        },
        "identity": {
            "full_name": full_name,
            "email": "nora@example.invalid",
            "location": {"city": "Gothenburg", "country_code": "SE"},
            "spoken_languages": [
                {"language": "English", "proficiency": "native", "is_native": True}
            ],
        },
        "preferences": {"target_roles": ["Embedded engineer"]},
        "education": [
            {
                "education_id": "e_stable",
                "type": "degree",
                "institution": "Chalmers",
                "qualification": "MSc",
            }
        ],
        "career_history": {
            "role_groups": [
                {
                    "role_group_id": "rg_stable",
                    "organization": {"name": "Mpya", "industries": ["technology"]},
                    "display_role_title": "Engineer",
                    "started": "2020-01",
                    "ended": "present",
                    "employment_type": "permanent",
                    "blocks": [
                        {
                            "block_id": "b_stable",
                            "role_title": "Engineer",
                            "started": "2020-01",
                            "ended": "present",
                            "block_type": "technical_delivery",
                            "industries": ["technology"],
                            "evidence_items": [
                                {
                                    "evidence_id": "ev_stable",
                                    "type": "responsibility",
                                    "text": "Built a safe profile service.",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "gaps": [
            {
                "gap_id": "g_stable",
                "kind": "missing_required",
                "status": "open",
                "suggested_question": "Which month did the role start?",
            }
        ],
    }


def _write_profile(root, document: dict) -> None:
    path = root / "nora" / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Consultant Profile — Nora M\n\n```yaml\n"
        + yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        + "```\n",
        encoding="utf-8",
    )


def test_a_legacy_profile_without_an_identity_reads_at_revision_zero(profiles):
    document = _document()
    _write_profile(profiles, document)

    artifact = service.load_profile("nora")

    assert artifact.revision == 0
    assert artifact.document == document
    assert service.IDENTITY_KEY not in artifact.document["metadata"]


def test_lossless_read_keeps_every_stable_id_the_clean_reader_strips(profiles):
    document = _document()
    _write_profile(profiles, document)

    lossless = service.load_profile("nora").document
    cleaned = profile_reader.load_profile("nora")

    assert lossless["education"][0]["education_id"] == "e_stable"
    assert lossless["career_history"]["role_groups"][0]["role_group_id"] == "rg_stable"
    assert lossless["career_history"]["role_groups"][0]["blocks"][0]["block_id"] == "b_stable"
    assert lossless["career_history"]["role_groups"][0]["blocks"][0]["evidence_items"][0]["evidence_id"] == "ev_stable"
    assert lossless["gaps"][0]["gap_id"] == "g_stable"
    assert lossless["career_history"]["role_groups"][0]["blocks"][0]["industries"] == ["technology"]
    assert "education_id" not in cleaned["education"][0]
    clean_role_group = cleaned["career_history"]["role_groups"][0]
    assert "role_group_id" not in clean_role_group
    clean_block = clean_role_group["blocks"][0]
    assert "block_id" not in clean_block
    assert "industries" not in clean_block
    assert "evidence_id" not in clean_block["evidence_items"][0]
    assert "gap_id" not in cleaned["gaps"][0]


def test_a_stale_profile_save_is_refused_and_keeps_the_winner(profiles):
    _write_profile(profiles, _document())
    initial = service.load_profile("nora")

    winner = copy.deepcopy(initial.document)
    winner["identity"]["full_name"] = "Winner"
    saved = service.save_profile("nora", winner, expected_revision=initial.revision)
    assert saved.revision == 1

    stale = copy.deepcopy(initial.document)
    stale["identity"]["full_name"] = "Stale"
    with pytest.raises(service.ProfileServiceError) as exc:
        service.save_profile("nora", stale, expected_revision=initial.revision)

    assert exc.value.status == 409
    final = service.load_profile("nora")
    assert final.revision == saved.revision
    assert final.document["identity"]["full_name"] == "Winner"


def test_a_revision_match_with_a_mismatched_content_hash_is_refused(profiles):
    """S16 acceptance #1: the importer-drift hole P1 deferred. The CV importer
    writes `profile.md` under the same lock but never bumps
    `_profile_identity`, so a completed import is invisible to the revision
    check alone — a caller's ``expected_content_hash`` closes it."""
    document = _document()
    _write_profile(profiles, document)
    initial = service.load_profile("nora")

    # Simulate the importer: real content changes, the revision marker does
    # not move (there was none to move — the fixture starts at revision 0).
    drifted = copy.deepcopy(document)
    drifted["identity"]["full_name"] = "Imported Winner"
    _write_profile(profiles, drifted)
    assert service.load_profile("nora").revision == initial.revision

    edit = copy.deepcopy(initial.document)
    edit["identity"]["email"] = "changed@example.invalid"
    with pytest.raises(service.ProfileServiceError) as exc:
        service.save_profile(
            "nora",
            edit,
            expected_revision=initial.revision,
            expected_content_hash=initial.content_hash,
        )

    assert exc.value.status == 409
    final = service.load_profile("nora")
    assert final.document["identity"]["full_name"] == "Imported Winner"


def test_a_matching_content_hash_is_not_required_and_does_not_block_a_save(profiles):
    """``expected_content_hash`` is optional (S16 acceptance #1) — an existing
    caller that only ever checked the revision, like the chat patcher before
    this packet, keeps working unchanged."""
    _write_profile(profiles, _document())
    initial = service.load_profile("nora")

    edit = copy.deepcopy(initial.document)
    edit["identity"]["full_name"] = "No Hash Passed"
    saved = service.save_profile("nora", edit, expected_revision=initial.revision)
    assert saved.document["identity"]["full_name"] == "No Hash Passed"


def test_untouched_facts_survive_an_identity_only_save(profiles):
    """S16 acceptance #8: the whole-document save model's correctness
    property. Editing one identity field must leave every other fact —
    career history, education, other gaps, every stable ID, and specifically
    `metadata._last_import_key` (the importer's crash-recovery marker,
    cv-import.md G3.2) — unchanged in the parsed document."""
    document = _document()
    _write_profile(profiles, document)
    initial = service.load_profile("nora")

    edited = copy.deepcopy(initial.document)
    edited["identity"]["full_name"] = "New Name"
    saved = service.save_profile("nora", edited, expected_revision=initial.revision)

    assert saved.document["identity"]["full_name"] == "New Name"
    assert saved.document["career_history"] == document["career_history"]
    assert saved.document["education"] == document["education"]
    assert saved.document["gaps"] == document["gaps"]
    assert saved.document["preferences"] == document["preferences"]
    assert saved.document["metadata"]["_last_import_key"] == document["metadata"]["_last_import_key"]
    # Stable IDs, specifically: assign_new_ids() must not mint over them.
    role_group = saved.document["career_history"]["role_groups"][0]
    assert role_group["role_group_id"] == "rg_stable"
    assert role_group["blocks"][0]["block_id"] == "b_stable"
    assert role_group["blocks"][0]["evidence_items"][0]["evidence_id"] == "ev_stable"
    assert saved.document["education"][0]["education_id"] == "e_stable"
    assert saved.document["gaps"][0]["gap_id"] == "g_stable"


def test_save_profile_stamps_updated_at_itself(profiles):
    """S16 acceptance #6: one writer owns `metadata.updated_at` so no future
    caller can forget it — `save_profile` sets it, not its callers."""
    document = _document()
    _write_profile(profiles, document)
    initial = service.load_profile("nora")

    edited = copy.deepcopy(initial.document)
    edited["identity"]["full_name"] = "Stamped"
    saved = service.save_profile("nora", edited, expected_revision=initial.revision)

    assert saved.document["metadata"]["updated_at"]
    assert saved.document["metadata"]["updated_at"] != document["metadata"]["updated_at"]


# ── The HTTP surface ────────────────────────────────────────────────────


def _write_actor_profile(root, username: str, full_name: str) -> None:
    document = _document(full_name)
    document["metadata"]["consultant_id"] = username
    path = root / username / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Consultant Profile — {full_name}\n\n```yaml\n"
        + yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        + "```\n",
        encoding="utf-8",
    )


def test_the_profile_route_round_trips_over_http_with_a_content_hash_guard(
    client, actors, profiles
):
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _write_actor_profile(profiles, username, "Original Name")

    loaded = client.get(f"/api/super-cv/{username}")
    assert loaded.status_code == 200, loaded.text
    body = loaded.json()
    assert body["revision"] == 0

    edited = copy.deepcopy(body["profile"])
    edited["identity"]["full_name"] = "Updated Name"
    saved = client.put(
        f"/api/super-cv/{username}",
        json={
            "profile": edited,
            "revision": body["revision"],
            "content_hash": body["content_hash"],
        },
    )
    assert saved.status_code == 200, saved.text
    saved_body = saved.json()
    assert saved_body["revision"] == 1
    assert saved_body["profile"]["identity"]["full_name"] == "Updated Name"
    assert client.get(f"/api/super-cv/{username}").json()["profile"]["identity"]["full_name"] == "Updated Name"

    # Acceptance #1: revision matches, content hash does not — refused.
    drift = client.put(
        f"/api/super-cv/{username}",
        json={
            "profile": edited,
            "revision": saved_body["revision"],
            "content_hash": "not-the-real-hash",
        },
    )
    assert drift.status_code == 409


def test_an_unrelated_actor_cannot_put_someone_elses_profile(
    client, actors, profiles, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_ENFORCED", True)
    username = actors.consultant.username
    _write_actor_profile(profiles, username, "Original Name")

    actors.login(client, actors.other_ta)
    r = client.put(
        f"/api/super-cv/{username}",
        json={"profile": _document(), "revision": 0, "content_hash": "irrelevant"},
    )
    assert r.status_code == 403


def test_the_put_route_refuses_an_omitted_content_hash(client, actors, profiles):
    """An omitted ``content_hash`` must not reach ``save_profile`` and
    silently disable the importer-drift guard acceptance 1 added — the route
    requires it even though the service keeps it optional for other callers
    (`profile_patcher.py`)."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _write_actor_profile(profiles, username, "Original Name")
    loaded = client.get(f"/api/super-cv/{username}").json()

    edited = copy.deepcopy(loaded["profile"])
    edited["identity"]["full_name"] = "Should Not Save"

    r = client.put(
        f"/api/super-cv/{username}",
        json={"profile": edited, "revision": loaded["revision"]},
    )
    assert r.status_code == 422
    assert client.get(f"/api/super-cv/{username}").json()["profile"]["identity"]["full_name"] == "Original Name"


def test_the_put_route_refuses_a_null_content_hash_over_importer_drift(
    client, actors, profiles
):
    """The exact bypass the review flagged: an authorized caller loads a
    revision, an importer-style write changes the actual content without
    bumping the revision marker, and the caller PUTs the stale document back
    with ``content_hash: null``. That must 422, not silently overwrite the
    importer's change the way an optional route field would."""
    actors.login(client, actors.consultant)
    username = actors.consultant.username
    _write_actor_profile(profiles, username, "Original Name")
    loaded = client.get(f"/api/super-cv/{username}").json()
    assert loaded["revision"] == 0

    # Simulate the importer: real content changes, the revision marker does
    # not move.
    _write_actor_profile(profiles, username, "Imported Winner")
    assert client.get(f"/api/super-cv/{username}").json()["revision"] == 0

    stale = copy.deepcopy(loaded["profile"])
    stale["identity"]["email"] = "changed@example.invalid"
    r = client.put(
        f"/api/super-cv/{username}",
        json={"profile": stale, "revision": loaded["revision"], "content_hash": None},
    )
    assert r.status_code == 422
    assert client.get(f"/api/super-cv/{username}").json()["profile"]["identity"]["full_name"] == "Imported Winner"
