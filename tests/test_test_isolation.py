"""The test suite must not be able to touch the real data tree (V2 M1, G-037).

`backend/config.py` creates its data directories at import time from
env-provided paths, so before M1 a bare `pytest` run materialised and could
write into the developer's live `./data`. The repository-root `conftest.py`
redirects every data root into a throwaway directory. These tests prove the
redirect is actually in force, because a silent regression here would put live
consultant data back in reach of the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import conftest as root_conftest
from backend import config

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every path constant that backend/config.py creates on import.
_DATA_ROOTS = (
    "DATA_DIR",
    "JOB_STORE_DIR",
    "APPLICATION_TRACKER_DIR",
    "TA_CONFIG_DIR",
    "CV_PUBLISH_PHOTO_DIR",
)


@pytest.mark.parametrize("name", _DATA_ROOTS)
def test_data_root_is_outside_the_repository(name):
    path = Path(getattr(config, name)).resolve()
    assert _REPO_ROOT not in path.parents and path != _REPO_ROOT, (
        f"{name} resolves to {path}, which is inside the repository — the suite "
        "can write to real data"
    )


@pytest.mark.parametrize("name", _DATA_ROOTS)
def test_data_root_is_under_the_isolated_root(name):
    path = Path(getattr(config, name)).resolve()
    assert root_conftest.TEST_DATA_ROOT in path.parents


def test_the_real_data_directory_is_not_used():
    """Named explicitly: the shipped default is ./data, and that is the exact
    path this isolation exists to keep out of the suite."""
    for name in _DATA_ROOTS:
        assert not str(Path(getattr(config, name)).resolve()).startswith(
            str(_REPO_ROOT / "data")
        )


def test_markers_are_applied_automatically(request):
    """This test declares no marker of its own, so the root conftest must have
    given it `fast` and `complete`. If auto-marking regresses, `make test-fast`
    silently runs nothing."""
    names = {m.name for m in request.node.iter_markers()}
    assert "fast" in names
    assert "complete" in names
    assert "integration" not in names
