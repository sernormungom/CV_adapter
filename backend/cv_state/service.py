"""The base CV artifact — the consultant's own working CV, with an identity.

V1 kept this as an unversioned JSON blob under
`data/profiles/<username>/cv_state.json`, read and written by two routes inside
`backend/main.py`. Two things were wrong with that for Slice 3:

**It had no identity.** P-009 requires the base and each Application-tailored CV
to be separate artifacts, and the slice has to *prove* that tailoring never
writes base state. "Prove" needs something to compare, so the base CV now
carries a `revision` and a `content_hash` — the same two facts a tailored CV
carries — and a test can assert both are unmoved across a whole generate-and-edit
path (`tests/test_tailored_cv.py`).

**Any write won.** Two editor tabs, or an autosave landing after a newer save,
silently overwrote each other. A write now states the revision it is based on
and is refused if that is not the current one.

The storage stays a file on the profile volume, deliberately: it is one of the
four canonical base-profile artifacts the plan's §1.1 seed check validates, and
moving it into Postgres would change what that check means for a gain the pilot
does not need. The identity travels *inside* the same file under a reserved key
(`_cv_identity`), so there is exactly one store and nothing to drift — a seeded
file that predates this simply reads as revision 0 with its hash computed from
its content.

`canonical_json`/`content_hash` are shared with the tailored artifact so both
identities are computed one way. The canonical form sorts keys and uses compact
separators: the hash must answer "is this the same CV", not "did the client
serialize its dictionary in the same order this time".
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from backend import config

try:  # POSIX — the deployed backend and the WSL test environment.
    import fcntl
except ImportError:  # pragma: no cover - Windows dev tooling only
    fcntl = None  # type: ignore[assignment]

#: The reserved key the identity lives under, inside the state file. Stripped
#: from every read, so a client never sees it and cannot send it back as
#: content. A key starting with `_` cannot collide with an editor form field.
IDENTITY_KEY = "_cv_identity"

BASE_STATE_FILENAME = "cv_state.json"

#: The compare-and-write lock file, beside the state file. Dot-prefixed so it is
#: not mistaken for one of the profile's content artifacts.
LOCK_FILENAME = ".cv_state.lock"

#: The two artifact modes, spelled once. A route is addressed by mode; nothing
#: infers one from a payload.
MODE_BASE = "base"
MODE_APPLICATION = "application"


class CVStateError(RuntimeError):
    """A caller-fixable problem with a CV-state request."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


#: The one message for a write that lost a race. Kept as a constant so the base
#: and tailored halves cannot drift into describing the same refusal two ways.
STALE_WRITE_MESSAGE = (
    "This CV was changed by someone else (or another tab) since you loaded it. "
    "Reload before saving so their edit is not overwritten."
)


# ── The shared artifact vocabulary ────────────────────────────────────────


def canonical_json(state: Any) -> str:
    """The byte form a CV state is hashed from.

    Sorted keys and compact separators, so the hash answers "is this the same
    CV" rather than "did the client happen to serialize its dictionary in the
    same order". `default=str` keeps a stray non-JSON value (a date from a
    future writer) from turning a hash into a 500.
    """
    return json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    )


def content_hash(state: Any) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CVArtifact:
    """One CV artifact, whichever identity it has.

    The base and the tailored halves return this same shape, so the editor has
    one loader and one saver and the *mode* is the only thing that differs. A
    client that had to tell them apart by inspecting the payload would be one
    refactor away from writing tailored content into a base CV.
    """

    mode: str
    #: The artifact's address within its mode: a username for `base`, the
    #: tailored CV's id for `application`.
    id: str
    revision: int
    content_hash: str
    state: dict
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "id": self.id,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "updated_at": self.updated_at,
            "state": self.state,
        }


def strip_identity(document: Any) -> dict:
    """The CV state alone — the stored document minus its reserved identity."""
    if not isinstance(document, dict):
        return {}
    return {k: v for k, v in document.items() if k != IDENTITY_KEY}


# ── The base artifact ─────────────────────────────────────────────────────


def _profile_dir(username: str) -> Path:
    # Read through the module, not a from-import binding: the profile root is
    # environment-dependent and is redirected per test, and a value captured at
    # import time cannot follow it.
    return config.DATA_DIR / username


def _state_file(username: str) -> Path:
    return _profile_dir(username) / BASE_STATE_FILENAME


#: In-process serialization, per (username, lock file). `flock` below is per
#: open file *description*, so it normally serializes threads too — but Linux
#: implements `flock()` over NFS as a whole-file POSIX lock, which is per
#: **process**, and the deployed profile store is EFS (`docs/DECISIONS.md`).
#: Two threads in one task would therefore both hold "the" lock there. This
#: layer closes that, and makes the fast gate's threaded regression
#: deterministic rather than filesystem-dependent.
#:
#: Keyed by username plus lock filename and never pruned. That is bounded, not
#: a leak: every caller reaches this through an authorized, per-consultant
#: write path, so a key exists only for a consultant/file the caller was
#: authorized to write.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def exclusive_profile_write(username: str, lock_filename: str) -> Iterator[None]:
    """Serialize one consultant's whole compare-and-write, across tasks.

    Shared by every writer that must not let two concurrent attempts silently
    destroy one another on this consultant's profile directory — the base CV's
    `save_base` (via `_exclusive_base_write` below, `lock_filename=LOCK_FILENAME`)
    and `cv_importer`'s durable import handler, which writes `profile.md` under
    its own `lock_filename` so an import and a base-CV save never contend on the
    same file for no reason.

    Why this is required and an atomic rename is not enough: the rename makes a
    *torn file* impossible, and nothing more. Two writers that both load the
    same prior state, both find it current, and both write next each produce a
    complete, valid file — and the second silently destroys the first, while
    **both** callers are told they succeeded. Losing a consultant's data while
    reporting success is the failure this refusal exists to prevent, so the
    check and the write have to be one operation.

    The lock is a separate dot-prefixed file next to the one being written
    rather than that file itself, because the target file is replaced by
    rename (or fully rewritten): a lock held on it would be a lock on an
    unlinked inode the moment the write landed, and the next writer would lock
    the *new* file and see no contention.

    Scope: `flock` on the shared profile volume, so it holds between ECS tasks
    (EFS supports NFSv4 file locking), plus the per-process layer above. On a
    platform without `fcntl` the process layer alone applies — that is dev
    tooling on Windows, not the deployed path.
    """
    directory = _profile_dir(username)
    directory.mkdir(parents=True, exist_ok=True)
    with _thread_lock(f"{username}:{lock_filename}"):
        if fcntl is None:  # pragma: no cover - Windows dev tooling only
            yield
            return
        # O_CREAT, never O_TRUNC: the lock file's *content* is meaningless, and
        # truncating it is a write that a concurrent holder would see.
        handle = os.open(directory / lock_filename, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            # Closing the descriptor releases the lock, including if the
            # interpreter is killed mid-write.
            os.close(handle)


def _exclusive_base_write(username: str) -> Iterator[None]:
    return exclusive_profile_write(username, LOCK_FILENAME)


def _read_document(username: str) -> dict:
    path = _state_file(username)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # An unreadable base CV is an operator-fixable data problem, not a
        # server fault, and saying so beats a 500 that hides which file is bad.
        raise CVStateError(
            "The stored CV state for this consultant could not be read.",
            status=409,
        ) from exc
    return document if isinstance(document, dict) else {}


def load_base(username: str) -> CVArtifact:
    """The consultant's base CV, with its identity.

    A file written before this module existed has no `_cv_identity`; it reads as
    revision 0 with its hash computed from its content, so the seeded pilot
    profiles keep working untouched and get an identity on their first save.
    """
    document = _read_document(username)
    state = strip_identity(document)
    identity = document.get(IDENTITY_KEY) or {}
    stored_hash = identity.get("content_hash")
    computed_hash = content_hash(state)
    return CVArtifact(
        mode=MODE_BASE,
        id=username,
        revision=int(identity.get("revision") or 0),
        # The content is authoritative over a recorded hash: if a file was
        # edited outside the app, reporting the stale hash would let a client
        # believe nothing changed.
        content_hash=computed_hash if stored_hash != computed_hash else stored_hash,
        state=state,
        updated_at=identity.get("updated_at"),
    )


def save_base(
    username: str, state: dict, *, expected_revision: int
) -> CVArtifact:
    """Write the base CV, refusing a write based on a stale revision.

    `expected_revision` is what the caller loaded. It must equal the current
    revision, so two editor tabs (or a 30-second autosave landing behind a newer
    save) cannot silently overwrite each other — the loser is told to reload.

    The write is atomic: a temporary file in the same directory, then a rename.
    A partial write here is a destroyed CV, and the previous implementation
    truncated the real file before serializing.

    The revision check and the write are **one** operation, serialized by
    `_exclusive_base_write`. Checking and then writing would let two writers that
    both loaded the same revision both pass the check and both write, so the
    refusal above would report success to a caller whose edit had just been
    destroyed (`S3-P1-R1` finding 2). Atomicity of the rename does not help:
    both files are whole.

    **A content-identical save is a no-op** (V3-P-022/V3-P-033): nothing is
    written and the stored artifact comes back unchanged — same revision, hash
    and `updated_at`. The editor autosaves every 30 seconds whether or not
    anything changed, and bumping the revision on each tick made "revision"
    mean "minutes the tab was open". The check sits after the stale-revision
    refusal (a stale identical save is still refused) and inside the same
    critical section, so it compares against the state this write would replace.
    """
    if not isinstance(state, dict):
        raise CVStateError("A CV state must be a JSON object.")

    with _exclusive_base_write(username):
        current = load_base(username)
        if expected_revision != current.revision:
            raise CVStateError(STALE_WRITE_MESSAGE, status=409)

        clean = strip_identity(state)
        new_hash = content_hash(clean)
        # `load_base` recomputes its hash from the stored content, so this is
        # never comparing against a missing or stale recorded hash.
        if new_hash == current.content_hash:
            return current

        revision = current.revision + 1
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        document = dict(clean)
        document[IDENTITY_KEY] = {
            "revision": revision,
            "content_hash": new_hash,
            "updated_at": now,
        }
        _atomic_write_json(_state_file(username), document)

    return CVArtifact(
        mode=MODE_BASE,
        id=username,
        revision=revision,
        content_hash=document[IDENTITY_KEY]["content_hash"],
        state=clean,
        updated_at=now,
    )


def _atomic_write_json(path: Path, document: dict) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise
