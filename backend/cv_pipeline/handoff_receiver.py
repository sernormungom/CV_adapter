from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.cv_pipeline.context_assembler import assemble_context
from backend.cv_pipeline.cv_content_generator import generate_cv_content

router = APIRouter()


class GenerateCVRequest(BaseModel):
    consultant_id: str
    job_description: str


@router.post("/generate-cv")
async def generate_cv(req: GenerateCVRequest) -> JSONResponse:
    if not req.job_description.strip():
        raise HTTPException(status_code=422, detail="job_description must not be empty.")

    try:
        context = assemble_context(req.consultant_id, req.job_description)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No profile found for consultant '{req.consultant_id}'. Import a CV first.",
        )
    cv_content, errors = await generate_cv_content(context)

    if cv_content is None:
        raise HTTPException(
            status_code=422,
            detail={"message": "CV generation failed.", "errors": errors},
        )

    return JSONResponse(content={
        "success": True,
        "form_data": _cv_content_to_form_data(cv_content),
    })


def _cv_content_to_form_data(cv: dict) -> dict:
    """Convert cv-content YAML structure to the populateFromImport() JSON format."""
    header = cv.get("header", {})
    contact = header.get("contact", {})

    # Experience: each cv-content entry → one company entry with one assignment
    experience = []
    for exp in cv.get("experience", []):
        date_range = exp.get("date_range", "")
        # Split "Oct 2024 – Present" into from/to
        if " – " in date_range:
            from_date, to_date = date_range.split(" – ", 1)
        elif " - " in date_range:
            from_date, to_date = date_range.split(" - ", 1)
        else:
            from_date, to_date = date_range, ""

        experience.append({
            "company": exp.get("organization", ""),
            "location": "",
            "from": from_date.strip(),
            "to": to_date.strip(),
            "assignments": [{
                "role": exp.get("role_title", ""),
                "period": date_range,
                "bullets": exp.get("bullets", []),
                "tools": "",
            }],
        })

    # Education: first entry → main education block; extras → courses
    education_list = cv.get("education", [])
    main_edu: dict = {}
    extra_courses: list[str] = []
    if education_list:
        edu = education_list[0]
        years = edu.get("years", "")
        if "–" in years:
            y_from, y_to = years.split("–", 1)
        elif "-" in years:
            y_from, y_to = years.split("-", 1)
        else:
            y_from, y_to = years, ""
        main_edu = {
            "degree": edu.get("qualification", ""),
            "institution": edu.get("institution", ""),
            "from": y_from.strip(),
            "to": y_to.strip(),
            "description": "",
        }
        for edu in education_list[1:]:
            name = edu.get("qualification", "")
            if name:
                extra_courses.append(name)

    # Courses from cv-content + any extra education entries
    cv_courses = [
        c.get("name", "") if isinstance(c, dict) else str(c)
        for c in cv.get("courses", [])
    ]
    courses = [c for c in cv_courses + extra_courses if c]

    # Languages
    languages = [
        {"name": lang.get("language", ""), "level": lang.get("proficiency", "")}
        for lang in cv.get("languages", [])
    ]

    # Competencies → skills.main (the tailored skill list for this position)
    competencies = cv.get("competencies", [])

    return {
        "iName": header.get("full_name", ""),
        "iRole": header.get("job_title", ""),
        "iEmail": contact.get("email", ""),
        "iPhone": contact.get("phone", ""),
        "iAvail": "",
        "iSummary": cv.get("summary", ""),
        "skills": {
            "main": competencies,
            "other": [],
            "tool": [],
        },
        "experience": experience,
        "education": main_edu,
        "languages": languages,
        "courses": courses,
    }
