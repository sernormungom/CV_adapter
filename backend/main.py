import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

# Configure root logging once, at the app entry point, so module-level
# `logging.getLogger(__name__)` records (including per-call LLM token usage from
# backend/llm.py) are actually emitted. Without this the root logger defaults to
# WARNING and all INFO logs are silently dropped.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from backend import config
from backend.db.models import User, UserRole
from backend.authz.dependencies import (
    require_roles,
    require_username_access,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.db.session import SessionLocal, engine
from backend.retention.service import run_retention_purges
from backend.workflows.leader import LeaderLock
from backend.workflows.runner import WorkflowRunner
import backend.workflows  # noqa: F401  (registers the built-in handlers)

# Daily retention purge (docs/plans/v2-pilot.md, ADR-009): there was no
# existing scheduled path to run uploads/publications cleanup — only
# the admin-triggered POST /api/admin/retention/run (backend/retention/routes.py).
# Without this, an expired publication's row + photo file (and old uploads)
# would sit until an admin remembered to click Purge. Runs once at
# startup, then every 24h; failures are logged and never crash the loop.
_RETENTION_PURGE_INTERVAL_SECONDS = 24 * 60 * 60


async def _run_retention_purge_once() -> None:
    db = SessionLocal()
    try:
        result = run_retention_purges(db)
        logger.info("retention: daily purge ran: %s", result)
    except Exception:  # noqa: BLE001 - a bad cycle must not kill the loop
        logger.exception("retention: daily purge failed")
    finally:
        db.close()


async def _retention_purge_loop() -> None:
    while True:
        await _run_retention_purge_once()
        await asyncio.sleep(_RETENTION_PURGE_INTERVAL_SECONDS)


def _build_workflow_runner() -> WorkflowRunner:
    """Compose the runner. All behaviour lives in backend/workflows/."""
    return WorkflowRunner(
        session_factory=SessionLocal,
        leader=LeaderLock(engine),
        poll_seconds=config.WORKFLOW_POLL_SECONDS,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_retention_purge_loop())

    # One durable workflow runner, gated by a Postgres advisory lock: during a
    # rolling deployment both tasks call this and only the lock holder runs a
    # loop (P-006). A non-leader serves HTTP normally.
    workflow_runner = (
        _build_workflow_runner() if config.WORKFLOW_RUNNER_ENABLED else None
    )
    if workflow_runner is not None:
        await workflow_runner.start()
        logger.info(
            "workflow runner: %s",
            "leading" if workflow_runner.is_leader else "standing by (not leader)",
        )

    try:
        yield
    finally:
        if workflow_runner is not None:
            await workflow_runner.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CV Adapter API", version="2.0.0", lifespan=_lifespan)

# CORS allowlist + HSTS (GDPR/Art 32 hardening — Session 4 of
# docs/legal/decisions.md). See backend/config.py for the origins list
# and why SESSION_COOKIE_SECURE also gates the HSTS header.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _add_hsts_header(request: Request, call_next):
    response = await call_next(request)
    if config.SESSION_COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
    return response

# `POST /api/import-cv` — the old synchronous importer disabled for Release Cut
# A (`docs/plans/v2-pilot.md` Slice 3) — stays retired permanently: it ran
# extraction and an LLM call on an open connection, the same known 504 path
# P-006 exists to remove, and it wrote the consultant's `profile.md` from
# whichever editor mode was open. V2 Slice 4 restores CV import as a durable
# workflow at a new address, `backend/cv_importer/routes.py`
# (`POST /api/profiles/{username}/cv-import`, 202 + polling, the transient
# upload deleted after the handler's last required stage on every path). Its
# extraction/merge components (`cv_parser`, `experience_extractor`,
# `experience_writer`, `profile_merger`) are unchanged; only the route and its
# durable orchestration are new.

# The CV editor's two artifact identities (V2 Slice 3, P-009). The base half is
# `/api/cv/base/{username}` — the consultant's own working CV, moved out of this
# module where it used to live as two unversioned routes; the Application half
# is `/api/applications/{id}/cv`. There is deliberately no endpoint serving
# both: a client picks an address, not a payload flag, so tailored work cannot
# reach base storage by getting one field wrong.
#
# `POST /api/generate-cv` is retired with them. It generated synchronously into
# whatever CV the editor happened to hold, so it was both the V1 base-overwrite
# P-009 forbids and the open-connection pattern P-006 replaces. Its generation
# machinery is reused unchanged by the durable handler.
from backend.cv_state.routes import router as cv_state_router
from backend.tailored_cv.routes import router as tailored_cv_router
from backend.cv_importer.routes import router as cv_importer_router

# SuperCV P1: authorized, lossless profile reads. The service also owns the
# revision-checked save path used by the existing chat patcher; this router is
# intentionally read-only until the separately reviewed editing packet.
from backend.super_cv.routes import router as super_cv_router

# Jobs (V2 Slice 1): relational Jobs, on-demand advisory scoring, and Reject.
# Carries its own /api/jobs prefix and owns its authorization; V1's JSON Job
# Store, review-batch matcher, and Maybe dashboard are retired.
from backend.jobs.routes import router as jobs_router

# Applications (V2 Slice 2): Apply is a first-class row with its own lifecycle,
# replacing V1's `application_status` field inside a verdict blob. Carries its
# own /api/applications prefix and owns its authorization.
from backend.applications.routes import router as applications_router

# The authorization policy's own read surface (/api/me/consultants): which
# Consultant contexts the caller may act in. A projection of the policy, so a
# client picker and the server's access decision are the same set.
from backend.authz.routes import router as authz_router

from backend.cv_assistant.routes import router as chat_router

# Auth & RBAC (Phase 2a). Router carries its own /api/auth prefix.
from backend.auth.routes import router as auth_router

# Tenancy management (Phase 2b): per-TA sources + consultant assignments.
from backend.tenancy.routes import router as tenancy_router

# Notifications (Phase 3a): in-app inbox + best-effort email/SMS. Router carries
# its own /api/notifications prefix.
from backend.notifications.routes import router as notifications_router

# CV publishing (ADR-009): consultant-scoped capability links to a read-only CV
# page + view tracking. The public /cv/view route is intentionally
# unauthenticated.
from backend.cv_publish.routes import api_router as cv_publish_api_router
from backend.cv_publish.routes import public_router as cv_publish_public_router

# Retention purges (Session 3 of docs/legal/decisions.md, GDPR Art
# 5(1)(c),(e)): admin-triggered upload/publication cleanup.
from backend.retention.routes import router as retention_router

# Data-subject rights (Session 2 of docs/legal/decisions.md, GDPR Art
# 15/17/20): admin-only consultant export (ZIP) + ordered hard erasure.
from backend.privacy.routes import router as privacy_router

app.include_router(auth_router)
app.include_router(authz_router)
app.include_router(tenancy_router)
app.include_router(notifications_router)
app.include_router(jobs_router)
app.include_router(applications_router)
app.include_router(tailored_cv_router)
app.include_router(cv_state_router)
app.include_router(cv_importer_router)
app.include_router(super_cv_router)
app.include_router(retention_router)
app.include_router(privacy_router)
app.include_router(cv_publish_api_router)
app.include_router(cv_publish_public_router)
app.include_router(chat_router)

_HTML_DIR = Path(__file__).parent.parent / "html"
_HTML_FILE = _HTML_DIR / "cv-importer.html"
_PRIVACY_FILE = _HTML_DIR / "privacy.html"
_PROFILES_DIR = Path(__file__).parent.parent / "data" / "profiles"


def _normalize_username(username: str) -> str:
    """Strip @domain suffix if email-format input."""
    return username.strip().split("@")[0].strip()


@app.get("/api/profiles")
def list_profiles(
    actor: User = Depends(require_roles(UserRole.admin, UserRole.ta)),
) -> list:
    # The named policy dependency owns the access decision; main.py merely
    # composes application routes.
    del actor
    if not _PROFILES_DIR.exists():
        return []
    return [d for d in os.listdir(_PROFILES_DIR) if (_PROFILES_DIR / d).is_dir()]


# `GET/POST /api/profiles/{username}/cv-state` used to live here. V2 Slice 3
# replaced them with `GET/PUT /api/cv/base/{username}`
# (`backend/cv_state/routes.py`), which gives the base CV a revision and a
# content hash and refuses a write based on a stale one. They were also CV
# business logic inside the module the plan (§5) says composes routers and
# lifecycle only.

_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


@app.post("/api/profiles/{username}/photo", dependencies=[Depends(require_username_access)])
async def upload_photo(username: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    uname = _normalize_username(username)
    profile_dir = _PROFILES_DIR / uname
    profile_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "photo.jpg").suffix.lower()
    if ext not in set(_PHOTO_EXTS):
        ext = ".jpg"
    for old in profile_dir.glob("photo.*"):
        old.unlink()
    (profile_dir / f"photo{ext}").write_bytes(await file.read())
    return {"ok": True}


@app.get("/api/profiles/{username}/photo", dependencies=[Depends(require_username_access)])
def get_photo(username: str) -> FileResponse:
    uname = _normalize_username(username)
    profile_dir = _PROFILES_DIR / uname
    for ext in _PHOTO_EXTS:
        p = profile_dir / f"photo{ext}"
        if p.exists():
            return FileResponse(p)
    raise HTTPException(status_code=404, detail="No photo")


# V1's application status vocabulary is retired (V2 Slice 3).
#
# `POST /api/profiles/{u}/accepted-positions/{job}/status` — the last writer of
# `verdicts.application_status`, which the CV editor called on generate and on
# print — was retired here, as Slice 2's handoff said it would be once this
# slice owned the editor path. Application state is `applications.status`
# (P-008) and a tailored CV is an artifact of an Application (P-009); nothing
# needs a second lifecycle stored in a verdict blob.
#
# `GET /api/profiles/{username}/accepted-positions`, the reader Slice 3 kept
# for the CV-publish "Published" group, was itself removed in STAB-7: the
# editor sources published-CV state from `GET /api/publications` (Slice 5)
# instead, and the survey found zero callers of this route in any shipped
# page. `verdict_store.py` itself — its last readers, the GDPR export and the
# daily retention purge — and the `verdicts` table were removed in V3 packet
# S2 (`docs/plans/v3/decisions.md` V3-P-008 decision 5): the verdict-fed
# learning path they backed had no live writer left to justify keeping them.


@app.get("/")
def serve_app() -> FileResponse:
    return FileResponse(_HTML_FILE, media_type="text/html")


@app.get("/privacy")
def serve_privacy_notice() -> FileResponse:
    return FileResponse(_PRIVACY_FILE, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Serve all HTML files at /html/<filename>
app.mount("/html", StaticFiles(directory=str(_HTML_DIR)), name="html")

from backend.matching.config_generation import router as matching_config_router
app.include_router(matching_config_router)
