"""
Unit tests for the CV content generator's prompt assembly and validator.
No LLM calls — these exercise pure functions only.
"""
from __future__ import annotations

from backend.cv_pipeline import cv_content_generator as gen


# ── _strip_frontmatter ───────────────────────────────────────────────────────

def test_strip_frontmatter_removes_block():
    md = "---\nname: x\ndesc: y\n---\n\n# Title\nbody"
    out = gen._strip_frontmatter(md)
    assert out.startswith("# Title")
    assert "name: x" not in out


def test_strip_frontmatter_noop_without_block():
    md = "# Title\nbody"
    assert gen._strip_frontmatter(md) == md


# ── _build_system_prompt ─────────────────────────────────────────────────────

def test_build_system_prompt_composition():
    sp = gen._build_system_prompt()
    assert not sp.lstrip().startswith("---")        # frontmatter stripped
    assert "CV CONTENT SCHEMA" in sp                 # output contract + schema present
    assert "Consultant Profile Schema" not in sp     # profile schema deliberately excluded


# ── _validate_cv_content ─────────────────────────────────────────────────────

def _valid_doc() -> dict:
    return {
        "header": {"full_name": "N", "job_title": "Dev"},
        "summary": "s",
        "competencies": {"hard_skills": ["C++"], "soft_skills": [], "tools": ["Git"]},
        "experience": [{
            "organization": "Volvo", "role_title": "Dev", "date_range": "2024 - Present",
            "relevant_items": ["AUTOSAR"], "bullets": ["a", "b", "c"],
        }],
        "languages": [{"language": "English", "proficiency": "fluent"}],
    }


def test_validate_accepts_new_shape():
    assert gen._validate_cv_content(_valid_doc()) == []


def test_validate_rejects_legacy_flat_competencies():
    doc = _valid_doc()
    doc["competencies"] = ["C++", "AUTOSAR"]
    issues = gen._validate_cv_content(doc)
    assert any("competencies must be a mapping" in i["context"] for i in issues)


def test_validate_requires_hard_skills():
    doc = _valid_doc()
    doc["competencies"] = {"hard_skills": [], "soft_skills": [], "tools": []}
    issues = gen._validate_cv_content(doc)
    assert any("hard_skills" in i["context"] for i in issues)


def test_validate_relevant_items_must_be_list():
    doc = _valid_doc()
    doc["experience"][0]["relevant_items"] = "AUTOSAR"  # string, not a list
    issues = gen._validate_cv_content(doc)
    assert any("relevant_items must be a list" in i["context"] for i in issues)
