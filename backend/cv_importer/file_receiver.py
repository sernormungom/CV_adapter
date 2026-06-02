import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

from backend.cv_importer.cv_parser import extract_text
from backend.cv_importer.experience_extractor import extract_experience
from backend.cv_importer.experience_writer import write_profile

router = APIRouter()

ALLOWED_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}


@router.post("/import-cv")
async def import_cv(file: UploadFile = File(...)) -> JSONResponse:
    mime = file.content_type or ""
    # Some browsers send generic type for .docx; sniff by extension
    if mime not in ALLOWED_TYPES:
        ext = Path(file.filename or "").suffix.lower()
        if ext == ".pdf":
            mime = "application/pdf"
        elif ext in (".docx",):
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif ext in (".doc",):
            mime = "application/msword"
        else:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type '{file.filename}'. Upload a PDF or DOCX.",
            )

    file_bytes = await file.read()
    original_suffix = Path(file.filename or "cv").suffix

    with tempfile.NamedTemporaryFile(suffix=original_suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        raw_text = extract_text(tmp_path, mime)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Could not extract text: {exc}") from exc

    if not raw_text.strip():
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Could not extract text from file.")

    profile_data, errors = await extract_experience(raw_text)

    if profile_data is None:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail={"message": "LLM could not structure the CV.", "errors": errors},
        )

    # Pass the original filename and temp path so write_profile can archive it
    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    original_name = f"{datestamp}_{Path(file.filename or 'cv').name}"

    result = write_profile(profile_data, source_tmp_path=tmp_path, source_filename=original_name)
    result["extraction_errors"] = errors
    return JSONResponse(content=result)
