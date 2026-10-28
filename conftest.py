"""Repository-root pytest configuration: isolated data roots and test markers.

This file lives at the repository root rather than under `tests/` because its
first job has to happen before *anything* imports `backend.config`. Pytest
imports conftest files from the rootdir downwards, so this module body runs
before `tests/conftest.py` pulls in `backend.main`.

Why that ordering matters (V2 M1, gap G-037): `backend/config.py` reads
`DATA_DIR`, `JOB_STORE_DIR`, `APPLICATION_TRACKER_DIR`, `TA_CONFIG_DIR` and
`CV_PUBLISH_PHOTO_DIR` from the environment and then **creates those
directories at import time**. With the shipped defaults those are repo-relative
(`data/profiles`, `data/job_store/jobs`, ...), so a bare `pytest` run
materialised and could write into the developer's real `./data` tree. Any test
that forgot to monkeypatch a path wrote to live data and passed silently.

Isolation here is deliberately not opt-out. `CV_ADAPTER_TEST_DATA_ROOT` chooses
*where* the isolated root goes; it cannot point the suite back at `./data`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# ── Isolated data roots ─────────────────────────────────────────────────────

# Relative layout mirrors backend/config.py's defaults, so a test that reasons
# about the shape of the data tree still sees the shape it expects.
_DATA_ROOT_VARS: dict[str, str] = {
    "DATA_DIR": "profiles",
    "JOB_STORE_DIR": "job_store/jobs",
    "APPLICATION_TRACKER_DIR": "application_tracker",
    "TA_CONFIG_DIR": "ta_config",
    "CV_PUBLISH_PHOTO_DIR": "cv_publications/photos",
}

_EXPLICIT_ROOT = os.environ.get("CV_ADAPTER_TEST_DATA_ROOT")
if _EXPLICIT_ROOT:
    TEST_DATA_ROOT = Path(_EXPLICIT_ROOT).resolve()
    TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    _ROOT_IS_OURS = False
else:
    TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="cv-adapter-tests-")).resolve()
    _ROOT_IS_OURS = True

for _var, _rel in _DATA_ROOT_VARS.items():
    os.environ[_var] = str(TEST_DATA_ROOT / _rel)

# This is the only authentication-off setup used by the test suite. Backend
# configuration accepts it solely in APP_ENV=test/development and refuses it in
# production, so this cannot become an accidental deployment setting.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS", "true")
os.environ.setdefault("AUTH_ENFORCED", "false")

# Password hashing cost, for the same reason and on the same terms: bcrypt at
# the production factor of 12 costs ~0.24s per hash, the `actors` fixture
# hashes four per test plus a verify per login, and that was ~85% of the fast
# gate's entire runtime — roughly six minutes of the suite spent proving
# bcrypt is slow. 4 rounds is ~1ms. `backend/config.py` refuses this outright
# in production and elsewhere without the escape hatch set above, and
# `tests/test_auth.py` pins both the floor and a real 12-round round-trip so
# the production cost is still exercised.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

# Keep test runs off any real LLM account even if a developer has a key
# exported. No test should reach the network; this makes a slip fail loudly at
# the call site instead of spending money.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-a-real-credential")


def _allow_asyncio_portal_wakeup_in_codex_sandbox() -> None:
    """Keep in-process TestClient requests working without sandbox egress.

    Codex's Linux workspace-write sandbox marks network access as disabled with
    ``CODEX_SANDBOX_NETWORK_DISABLED=1``. Its seccomp filter then rejects the
    ``sendto`` syscall even for the Unix socket pair asyncio uses only to wake
    its own event-loop thread. Starlette's TestClient waits on that thread, so
    the first request otherwise hangs. Writing the same non-blocking socket fd
    directly has the identical wake-up effect and is permitted by the sandbox.

    This runs only in that explicitly identified sandbox, before FastAPI or
    Starlette are imported. It grants no network access and does not alter app
    runtime behaviour.
    """
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") != "1":
        return

    from asyncio.selector_events import BaseSelectorEventLoop

    def _write_to_self_with_os_write(self) -> None:
        csock = self._csock
        if csock is None:
            return
        try:
            os.write(csock.fileno(), b"\0")
        except OSError:
            # Match asyncio's normal best-effort wake-up semantics: the socket
            # is non-blocking, so a full or already-closed self-pipe is safe.
            pass

    BaseSelectorEventLoop._write_to_self = _write_to_self_with_os_write


_allow_asyncio_portal_wakeup_in_codex_sandbox()


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """Remove the throwaway root we created. A root the caller named is theirs."""
    if _ROOT_IS_OURS:
        shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)


# ── Test markers ────────────────────────────────────────────────────────────
#
# Four gates, documented in docs/testing.md and wired to Make targets:
#
#   fast         no external service; SQLite or pure Python. The dev loop.
#   static       static-client (HTML/JS/CSS) contract tests.
#   integration  needs a disposable Postgres 16 (tests/integration/).
#   complete     everything — the release gate.
#
# `fast` and `complete` are applied automatically so maintainers never have to
# annotate individual tests: `integration` comes from living under
# tests/integration/, `static` from a module-level `pytestmark`, and anything
# that is neither is `fast`. Every test is also `complete`, which makes
# `-m complete` a real selector rather than a label with no members.

_REPO_ROOT = Path(__file__).resolve().parent
_INTEGRATION_DIR = _REPO_ROOT / "tests" / "integration"


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001
    for item in items:
        path = Path(str(item.fspath)).resolve()
        is_integration = _INTEGRATION_DIR in path.parents
        if is_integration:
            item.add_marker(pytest.mark.integration)
        has_static = any(m.name == "static" for m in item.iter_markers())
        if not is_integration and not has_static:
            item.add_marker(pytest.mark.fast)
        item.add_marker(pytest.mark.complete)
