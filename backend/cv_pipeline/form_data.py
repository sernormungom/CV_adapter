"""cv-content YAML → the editor's form fields. **The live render path.**

Moved here from `handoff_receiver.py` in V2 Slice 3, unchanged, when the
synchronous `POST /api/generate-cv` route it lived beside was retired: the
mapping is the valuable half and is now called by the durable generation
handler (`backend/tailored_cv/handler.py`) instead of by a request that has to
stay open while an LLM works.

The output shape is the editor's `populateFromImport()` argument. Turning that
into a full `cv_state` — the shape the editor loads and saves — is
`backend/tailored_cv/state_builder.py`, which the browser used to do itself.
"""

from __future__ import annotations


def cv_content_to_form_data(cv: dict) -> dict:
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
                # relevant_items (4–8 role tags) → the editor's per-assignment "Tools & Skills" line
                "tools": ", ".join(exp.get("relevant_items") or []),
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

    # Competencies (3 columns) → the editor's 3 skill buckets.
    # hard_skills→main, soft_skills→other, tools→tool. Tolerate a legacy flat list.
    comp = cv.get("competencies", {})
    if isinstance(comp, dict):
        skills_main = comp.get("hard_skills") or []
        skills_other = comp.get("soft_skills") or []
        skills_tool = comp.get("tools") or []
    else:
        skills_main, skills_other, skills_tool = list(comp or []), [], []

    return {
        "iName": header.get("full_name", ""),
        "iRole": header.get("job_title", ""),
        "iEmail": contact.get("email", ""),
        "iPhone": contact.get("phone", ""),
        "iAvail": "",
        "iSummary": cv.get("summary", ""),
        "skills": {
            "main": skills_main,
            "other": skills_other,
            "tool": skills_tool,
        },
        "experience": experience,
        "education": main_edu,
        "languages": languages,
        "courses": courses,
    }
