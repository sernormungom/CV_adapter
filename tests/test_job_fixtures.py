"""The committed job-posting fixtures are a contract; these tests guard it.

They are the only job corpus that survives the M0 cleanup, so they must stay
non-personal.

This module used to be `tests/test_ingest.py`, testing
`POST /api/opportunities/ingest` into the JSON Job Store. That endpoint and
that store were retired in Slice 1; relational Job creation is covered by
`tests/test_jobs.py`. The fixture guards below outlived them.

STAB-7 removed the identity-derivation chain this module used to also cover
(`build_identity_key`/`build_job_item`/`extract_source_native_id` — the
`native:`/`content:` branches) along with `shared/collection.py`, its last
caller. `clean_text` is still live (`backend/jobs/service.py`'s `build_input`,
reached from `POST /api/jobs`), so it keeps direct coverage here.
"""
from __future__ import annotations

import re

from shared.job_record import clean_text
from tests.fixtures import JOB_WITH_NATIVE_ID, JOB_WITHOUT_NATIVE_ID, job_text


# ── clean_text ────────────────────────────────────────────────────────────


def test_clean_text_normalizes_a_raw_fixture():
    text = clean_text(job_text(JOB_WITH_NATIVE_ID))
    assert text == text.strip()
    assert "\r" not in text
    assert "\n\n\n" not in text


# ── Sanitization ────────────────────────────────────────────────────────────


def test_fixtures_carry_no_real_identifiers():
    """Guard the sanitization contract in PROVENANCE.md: the committed fixtures
    must not reacquire a real client/broker identifier or a routable domain.

    This is the test that keeps the M0 cleanup honest — the fixtures are the
    only job corpus that survives it, so they must stay non-personal."""
    banned = ("volvo", "levigo", "mpyascitech", "ymlptr", "volgjp", "volvcajp")
    for name in (JOB_WITH_NATIVE_ID, JOB_WITHOUT_NATIVE_ID):
        text = job_text(name)
        lowered = text.lower()
        for needle in banned:
            assert needle not in lowered, f"{name} contains {needle!r}"

        # Every email address and URL must sit under an RFC 2606 reserved TLD,
        # so nothing in a fixture can resolve or be contacted.
        for address in re.findall(r"[\w.+-]+@[\w.-]+", text):
            assert address.endswith(".invalid"), f"{name}: {address}"
        for host in re.findall(r"https?://([\w.-]+)", text):
            assert host.endswith(".invalid"), f"{name}: {host}"
