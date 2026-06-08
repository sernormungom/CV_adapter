import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from backend.config import APPLICATION_TRACKER_DIR, JOB_STORE_DIR
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="CV Adapter API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# CV Preparation Pipeline (existing)
from backend.cv_importer.file_receiver import router as importer_router
from backend.cv_pipeline.handoff_receiver import router as pipeline_router

# Opportunity Matching Pipeline (new)
from backend.opportunity_pipeline.source_collector.routes import router as source_router
from backend.opportunity_pipeline.pre_filter_matcher.routes import router as matcher_router
from backend.opportunity_pipeline.dashboard.routes import router as dashboard_router
from backend.opportunity_pipeline.learning_module.routes import router as learning_router

app.include_router(importer_router, prefix="/api")
app.include_router(pipeline_router, prefix="/api")
app.include_router(source_router, prefix="/api/opportunities")
app.include_router(matcher_router, prefix="/api/opportunities")
app.include_router(dashboard_router, prefix="/api/dashboard")
app.include_router(learning_router, prefix="/api/opportunities")

_HTML_DIR = Path(__file__).parent.parent / "html"
_HTML_FILE = _HTML_DIR / "cv-builder-mpya-import_ver5 1.html"
_DASHBOARD_FILE = _HTML_DIR / "opportunity-dashboard.html"
_PROFILES_DIR = Path(__file__).parent.parent / "data" / "profiles"


def _normalize_username(username: str) -> str:
    """Strip @domain suffix if email-format input."""
    return username.strip().split("@")[0].strip()


@app.get("/api/profiles")
def list_profiles() -> list:
    if not _PROFILES_DIR.exists():
        return []
    return [d for d in os.listdir(_PROFILES_DIR) if (_PROFILES_DIR / d).is_dir()]


@app.get("/api/profiles/{username}/cv-state")
def get_cv_state(username: str) -> Dict[str, Any]:
    uname = _normalize_username(username)
    profile_dir = _PROFILES_DIR / uname
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_file = profile_dir / "cv_state.json"
    if not state_file.exists():
        return {}
    with open(state_file, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/profiles/{username}/cv-state")
async def save_cv_state(username: str, request: Request) -> Dict[str, Any]:
    uname = _normalize_username(username)
    profile_dir = _PROFILES_DIR / uname
    profile_dir.mkdir(parents=True, exist_ok=True)
    state = await request.json()
    state_file = profile_dir / "cv_state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return {"ok": True}


_TA_CONFIG_DIR = Path(__file__).parent.parent / "data" / "ta_config"
_VERAMA_SENTINEL = _TA_CONFIG_DIR / "verama_continue.flag"

_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


@app.post("/api/profiles/{username}/photo")
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


@app.get("/api/profiles/{username}/photo")
def get_photo(username: str) -> FileResponse:
    uname = _normalize_username(username)
    profile_dir = _PROFILES_DIR / uname
    for ext in _PHOTO_EXTS:
        p = profile_dir / f"photo{ext}"
        if p.exists():
            return FileResponse(p)
    raise HTTPException(status_code=404, detail="No photo")


_STATUS_VALUES = {"to_apply", "cv_ready", "applied"}
_STATUS_ORDER  = {"to_apply": 0, "cv_ready": 1, "applied": 2}
_APPLIED_RETENTION_DAYS = 7


@app.get("/api/profiles/{username}/accepted-positions")
def get_accepted_positions(username: str) -> list:
    uname = _normalize_username(username)
    verdicts_path = APPLICATION_TRACKER_DIR / f"{uname}_verdicts.json"
    if not verdicts_path.exists():
        return []
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=_APPLIED_RETENTION_DAYS)
    result = []
    for job_id, v in verdicts.items():
        if v.get("verdict") != "yes":
            continue
        app_status = v.get("application_status", "to_apply")
        # Drop applied positions that have been on the board for more than retention days
        if app_status == "applied":
            applied_at_str = v.get("applied_at", "")
            if applied_at_str:
                try:
                    if datetime.fromisoformat(applied_at_str) < cutoff:
                        continue
                except ValueError:
                    pass
        raw_text = ""
        job_path = JOB_STORE_DIR / f"{job_id}.json"
        if job_path.exists():
            raw_text = json.loads(job_path.read_text(encoding="utf-8")).get("raw_text", "")
        result.append({
            "job_id": job_id,
            "title": v.get("title_guess", ""),
            "source_id": v.get("source_id", ""),
            "source_url": v.get("source_url", ""),
            "match_score": v.get("match_score"),
            "application_status": app_status,
            "raw_text": raw_text,
        })
    return sorted(result, key=lambda x: (
        _STATUS_ORDER.get(x["application_status"], 0),
        -(x["match_score"] or 0),
    ))


@app.post("/api/profiles/{username}/accepted-positions/{job_id}/status")
async def update_position_status(username: str, job_id: str, request: Request) -> Dict[str, Any]:
    uname = _normalize_username(username)
    verdicts_path = APPLICATION_TRACKER_DIR / f"{uname}_verdicts.json"
    if not verdicts_path.exists():
        raise HTTPException(status_code=404, detail="No verdicts file found")
    body = await request.json()
    new_status = body.get("status", "")
    if new_status not in _STATUS_VALUES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    if job_id not in verdicts:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not in verdicts")
    verdicts[job_id]["application_status"] = new_status
    if new_status == "applied":
        verdicts[job_id]["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verdicts_path.write_text(json.dumps(verdicts, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "job_id": job_id, "status": new_status}


@app.post("/api/opportunities/verama-continue")
def verama_continue() -> Dict[str, Any]:
    """Signal the Verama Playwright adapter to continue collection."""
    _VERAMA_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    _VERAMA_SENTINEL.touch()
    return {"ok": True}


@app.get("/")
def serve_app() -> FileResponse:
    return FileResponse(_HTML_FILE, media_type="text/html")


@app.get("/opportunity-dashboard.html")
def serve_dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD_FILE, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Serve all HTML files at /html/<filename>
app.mount("/html", StaticFiles(directory=str(_HTML_DIR)), name="html")
