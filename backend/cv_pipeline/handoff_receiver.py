from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend.config import DATA_DIR
from backend.cv_pipeline.context_assembler import assemble_context
from backend.cv_pipeline.cv_content_generator import generate_cv_content
from backend.cv_pipeline.cv_renderer import render_cv

router = APIRouter()


class GenerateCVRequest(BaseModel):
    consultant_id: str
    job_description: str


@router.post("/generate-cv", response_class=HTMLResponse)
async def generate_cv(req: GenerateCVRequest) -> HTMLResponse:
    profile_path: Path = DATA_DIR / req.consultant_id / "profile.md"
    if not profile_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No profile found for consultant '{req.consultant_id}'. Import a CV first.",
        )

    if not req.job_description.strip():
        raise HTTPException(status_code=422, detail="job_description must not be empty.")

    context = assemble_context(req.consultant_id, req.job_description)
    cv_content, errors = await generate_cv_content(context)

    if cv_content is None:
        raise HTTPException(
            status_code=422,
            detail={"message": "CV generation failed.", "errors": errors},
        )

    html = render_cv(cv_content, req.consultant_id)
    return HTMLResponse(content=html)
