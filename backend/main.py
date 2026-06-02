from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="CV Adapter API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from backend.cv_importer.file_receiver import router as importer_router
from backend.cv_pipeline.handoff_receiver import router as pipeline_router
app.include_router(importer_router, prefix="/api")
app.include_router(pipeline_router, prefix="/api")

_HTML_FILE = Path(__file__).parent.parent / "html" / "cv-builder-mpya-import_ver5 1.html"


@app.get("/")
def serve_app() -> FileResponse:
    return FileResponse(_HTML_FILE, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
