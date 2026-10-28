"""
Unit tests for cv_content_to_form_data: the mapping from generated cv-content
into the editor's form fields (the live render path). Pure function, no LLM.

V2 Slice 3 moved the function to `backend/cv_pipeline/form_data.py`, unchanged,
when the synchronous `POST /api/generate-cv` route it lived beside was retired.
The durable generation handler calls it now; these tests are unchanged because
the mapping is.
"""
from __future__ import annotations

from backend.cv_pipeline.form_data import (
    cv_content_to_form_data as _cv_content_to_form_data,
)


def _doc() -> dict:
    return {
        "header": {"full_name": "N", "job_title": "Dev", "contact": {"email": "e@x.com"}},
        "summary": "s",
        "competencies": {
            "hard_skills": ["C++", "ISO 26262"],
            "soft_skills": ["Coaching"],
            "tools": ["Git"],
        },
        "experience": [{
            "organization": "Volvo", "role_title": "Dev", "date_range": "Oct 2024 – Present",
            "relevant_items": ["AUTOSAR", "ISO 26262"], "bullets": ["a", "b", "c"],
        }],
        "education": [{"institution": "ITESM", "qualification": "MSc", "years": "2005"}],
        "languages": [{"language": "English", "proficiency": "fluent"}],
    }


def test_competencies_map_to_three_buckets():
    fd = _cv_content_to_form_data(_doc())
    assert fd["skills"]["main"] == ["C++", "ISO 26262"]
    assert fd["skills"]["other"] == ["Coaching"]
    assert fd["skills"]["tool"] == ["Git"]


def test_relevant_items_join_into_assignment_tools():
    fd = _cv_content_to_form_data(_doc())
    assert fd["experience"][0]["assignments"][0]["tools"] == "AUTOSAR, ISO 26262"


def test_legacy_flat_competencies_fall_back_to_main():
    doc = _doc()
    doc["competencies"] = ["C++", "AUTOSAR"]
    fd = _cv_content_to_form_data(doc)
    assert fd["skills"]["main"] == ["C++", "AUTOSAR"]
    assert fd["skills"]["other"] == []
    assert fd["skills"]["tool"] == []
