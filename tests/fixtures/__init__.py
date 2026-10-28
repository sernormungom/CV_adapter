"""Committed test fixtures — the suite must never read the gitignored `data/` tree.

`job_text()` loads a sanitized job posting from `tests/fixtures/jobs/`. See that
directory's `PROVENANCE.md` for how the fixtures were derived from the
pre-cleanup corpus and what was sanitized out of them (V2 M1 / gap G-037).
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
JOBS_DIR = FIXTURE_DIR / "jobs"

# The forwarded-email posting carrying a native "Job Posting ID" — the dominant
# production shape, and the `native:` identity branch.
JOB_WITH_NATIVE_ID = "email_posting_with_native_id"
# A posting with no source-owned identifier — the `content:` hash branch.
JOB_WITHOUT_NATIVE_ID = "plain_posting_no_native_id"


def job_text(name: str = JOB_WITH_NATIVE_ID) -> str:
    """Return one sanitized job-posting fixture as text.

    Read with an explicit encoding and no error swallowing: these fixtures
    contain Swedish characters, and a silently mangled byte would change the
    derived `identity_key`/`job_id` the tests assert on.
    """
    path = JOBS_DIR / f"{name}.txt"
    if not path.is_file():
        available = sorted(p.stem for p in JOBS_DIR.glob("*.txt"))
        raise FileNotFoundError(
            f"No job fixture named {name!r}. Available: {available}"
        )
    return path.read_text(encoding="utf-8")
