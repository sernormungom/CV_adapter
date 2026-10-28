"""The SuperCV profile lock must serialize actual concurrent writers."""

from __future__ import annotations

import copy
import threading

import pytest
import yaml

from backend import config
from backend.cv_assistant import profile_utils
from backend.super_cv import service


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile_utils, "DATA_DIR", tmp_path)
    return tmp_path


def _document(full_name: str = "Original") -> dict:
    return {
        "metadata": {"schema_version": "1.3", "consultant_id": "nora"},
        "identity": {"full_name": full_name, "email": "nora@example.invalid"},
        "preferences": {},
        "education": [],
        "career_history": {"role_groups": []},
        "gaps": [],
    }


def _write_profile(root, document: dict) -> None:
    path = root / "nora" / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Consultant Profile — Original\n\n```yaml\n"
        + yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        + "```\n",
        encoding="utf-8",
    )


def test_two_threads_at_the_same_profile_revision_cannot_both_save(profiles):
    """Both threads load revision zero before either can acquire the lock."""
    _write_profile(profiles, _document())
    start = threading.Barrier(2)
    results: list[object] = []
    result_lock = threading.Lock()

    def writer(name: str) -> None:
        loaded = service.load_profile("nora")
        assert loaded.revision == 0
        start.wait(timeout=10)
        document = copy.deepcopy(loaded.document)
        document["identity"]["full_name"] = name
        try:
            outcome: object = service.save_profile(
                "nora", document, expected_revision=loaded.revision
            ).revision
        except service.ProfileServiceError as exc:
            outcome = exc.status
        with result_lock:
            results.append(outcome)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("Writer A", "Writer B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "a writer deadlocked on the profile lock"

    assert sorted(results, key=str) == [1, 409], results
    final = service.load_profile("nora")
    assert final.revision == 1
    assert final.document["identity"]["full_name"] in {"Writer A", "Writer B"}
