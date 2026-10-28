"""ADR-009/D-013/P-010 tests: CV publications, capability routes, view
tracking, lifecycle — the Application-owned, hash-token contract (S5-P1-R1),
and the publish/terminal-transition concurrency guard plus the presentation-
value injection surface (S5-P2, `S5-P1-R2`).

Covers: publish binds to an Application's tailored CV (never a client-
supplied snapshot), every publish call rotates the token (a hash is stored,
never the raw value), consultant/TA authz, extend/revoke by the publication's
non-secret id, the four header-authorized capability routes (meta/content/
photo/opened) and their strict headers, the opened beacon (skips authorized
sessions), the confirmed-terminal-Application hook (disables the link,
deletes the tailored CV, atomically with the status move) with the lock that
makes it safe against a concurrent Publish, a malicious presentation value
surviving publish, a real generated tailored CV publishing unchanged, the
forbidden-carrier negative tests the promotion gate (`docs/plans/v2-pilot.md`
§11) requires, and the anonymous exchange's global rate cap (STAB-5).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import timedelta

import pytest
import yaml
from sqlalchemy import select

from backend import config
from backend.cv_publish import service
from backend.cv_publish.routes import TOKEN_HEADER
from backend.cv_publish.throttle import public_capability_throttle
from backend.cv_state import service as cv_state_service
from backend.db.models import CVPublication, TailoredCV
from backend.tailored_cv import handler
from backend.workflows import runner as workflow_runner

# ── Fixtures / helpers ──────────────────────────────────────────────────────


_description_nonce = itertools.count()


def _distinct_filler() -> str:
    """Five never-repeating tokens, appended to every generated description so
    two calls can never collide as a Tier 1/2 near-duplicate of each other
    (`backend/jobs/service.py::find_near_duplicate`) regardless of how close
    their titles read to each other — a bare title swap (e.g. "Senior" vs
    "Staff Dev @ ACME") is not enough difference on its own."""
    start = next(_description_nonce) * 5
    return " ".join(f"ref{i}" for i in range(start, start + 5))


def _apply(client, consultant_username: str, *, title: str = "Senior Dev @ ACME") -> dict:
    """Create a Job for `consultant_username` and Apply. Returns the Application."""
    created = client.post(
        "/api/jobs",
        json={
            "consultant_username": consultant_username,
            "title": title,
            "company": "ACME",
            "description": f"We need a senior developer. {title}. {_distinct_filler()}. " * 4,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["id"]
    applied = client.post(
        "/api/applications",
        json={"job_id": job_id, "consultant_username": consultant_username},
    )
    assert applied.status_code == 201, applied.text
    return applied.json()["application"]


#: A cv_state shaped like the editor's captureFormState() output, with one
#: assignment carrying a rich-text field — the sanitizer's one target.
def _cv_state(desc_html: str = "<b>Shipped it.</b>", photo_src=None) -> dict:
    state = {
        "iName": "Alice",
        "iRole": "Senior Dev",
        "expList": [
            {
                "company": "ACME",
                "assignments": [{"role": "Dev", "descHtml": desc_html}],
            }
        ],
    }
    if photo_src is not None:
        state["photoSrc"] = photo_src
    return state


def _give_content(db, application_id, *, desc_html: str = "<b>Shipped it.</b>", photo_src=None) -> TailoredCV:
    """Insert a `ready` tailored CV directly — the real generation pipeline is
    proven elsewhere (`tests/test_tailored_cv.py`); publish only needs a
    tailored CV with content to derive a snapshot from."""
    tailored = TailoredCV(
        application_id=uuid.UUID(str(application_id)),
        status="ready",
        revision=1,
        content_hash="a" * 64,
        cv_state=_cv_state(desc_html, photo_src),
        input_snapshot={},
        input_hash="b" * 64,
    )
    db.add(tailored)
    db.commit()
    db.refresh(tailored)
    return tailored


def _publish(client, app_id: str) -> dict:
    r = client.post("/api/publications", json={"application_id": app_id})
    assert r.status_code == 201, r.text
    return r.json()


def _cap_headers(token: str) -> dict:
    return {TOKEN_HEADER: token}


# ── Real-generation fixtures (copied from tests/test_tailored_cv.py per
# docs/testing.md: "copy the fixture rather than a subset of it") — needed for
# check 3 (a real generated tailored CV must publish unchanged), not the
# hand-built cv_state the rest of this file uses. ──────────────────────────


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.profile_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.matching.config_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.cv_pipeline.context_assembler.DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def profile_for(profiles):
    def _install(username: str, *, headline: str = "Embedded work") -> None:
        root = profiles / username
        root.mkdir(parents=True, exist_ok=True)
        (root / "profile.md").write_text(
            "# Profile\n\n```yaml\n"
            + yaml.dump({"summary": headline, "career_history": {}})
            + "```\n",
            encoding="utf-8",
        )
        (root / "style.md").write_text("## Rules\n- Keep it terse.\n", encoding="utf-8")
        # Generation refuses a consultant with no photo (V3-P-023).
        base = cv_state_service.load_base(username)
        saved = cv_state_service.save_base(
            username,
            {**base.state, "photoSrc": f"/api/profiles/{username}/photo"},
            expected_revision=base.revision,
        )
        assert saved.state.get("photoSrc") == f"/api/profiles/{username}/photo"

    return _install


def _generated(job_title: str = "Embedded Engineer") -> dict:
    return {
        "header": {
            "full_name": "Nora M",
            "job_title": job_title,
            "contact": {"email": "nora@example.invalid"},
        },
        "summary": "Tailored summary.",
        "competencies": {
            "hard_skills": ["C", "ISO 26262"],
            "soft_skills": ["Coaching"],
            "tools": ["CAN"],
        },
        "experience": [
            {
                "organization": "Volvo",
                "role_title": "Engineer",
                "date_range": "2022 – Present",
                "relevant_items": ["AUTOSAR"],
                "bullets": ["Owned an ECU.", "Shipped it.", "Kept it alive."],
            }
        ],
        "education": [
            {"institution": "Chalmers", "qualification": "MSc", "years": "2018-2020"}
        ],
        "languages": [{"language": "English", "proficiency": "fluent"}],
    }


@pytest.fixture
def stub_llm(monkeypatch):
    box = {"document": _generated(), "error": None, "calls": 0}

    async def _generate(context):  # noqa: ARG001
        box["calls"] += 1
        box["last_context"] = context
        if box["error"] is not None:
            raise box["error"]
        if box["document"] is None:
            return None, [{"kind": "validation_error", "context": "secret prompt text"}]
        return box["document"], []

    monkeypatch.setattr(handler, "generate_cv_content", _generate)
    return box


def _drain(db) -> int:
    return workflow_runner.drain(db)


#: A distinctive, still-legitimate presentation choice — none of these are
#: the sanitizer's own defaults, so "unchanged after publish" is a real proof,
#: not a coincidence of everything already being the default.
_CURATED_PRESENTATION = {
    "pfFont": "'Playfair Display',serif|italic",
    "pfColor": "#3355aa",
    "pfWeight": "600",
    "skFont": "Georgia,serif",
    "skColor": "#116622",
    "skWeight": "300",
    "exFont": "'Raleway',sans-serif|italic",
    "exColor": "#aa3311",
    "exWeight": "600",
    "edFont": "'Lato',sans-serif",
    "edColor": "#222222",
    "edWeight": "300",
}


# ── service: publish binds to the tailored CV, never a client snapshot ─────


def test_publish_derives_snapshot_from_the_tailored_cv(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])

    pub = _publish(client, app["id"])
    assert pub["url"].startswith("http")
    assert "#" in pub["url"]  # fragment token, never a path segment
    assert "token" not in pub and "token_hash" not in pub
    assert "cv_state" not in pub  # metadata only


def test_publish_without_a_generated_cv_is_refused(client, actors):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    r = client.post("/api/publications", json={"application_id": app["id"]})
    assert r.status_code == 400


def test_republish_rotates_the_token_and_updates_the_snapshot(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"], desc_html="<b>v1</b>")
    first = _publish(client, app["id"])

    tailored = db.scalar(
        select(TailoredCV).where(TailoredCV.application_id == uuid.UUID(app["id"]))
    )
    tailored.cv_state = _cv_state("<b>v2</b>")
    db.commit()
    second = _publish(client, app["id"])

    assert second["url"] != first["url"]  # a fresh raw token every time
    assert second["id"] == first["id"]  # same row, not a second publication
    # The old link is dead — regenerate invalidates it (P-010).
    old_token = first["url"].rsplit("#", 1)[1]
    r = client.get("/api/publications/content", headers=_cap_headers(old_token))
    assert r.status_code == 404
    new_token = second["url"].rsplit("#", 1)[1]
    r2 = client.get("/api/publications/content", headers=_cap_headers(new_token))
    assert r2.status_code == 200
    assert "v2" in r2.json()["cv_state"]["expList"][0]["assignments"][0]["descHtml"]

    rows = db.scalars(
        select(CVPublication).where(CVPublication.application_id == uuid.UUID(app["id"]))
    ).all()
    assert len(rows) == 1  # updated in place, not a second row


def test_only_the_hash_is_ever_stored(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    raw_token = pub["url"].rsplit("#", 1)[1]

    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    assert raw_token not in row.token_hash
    assert row.token_hash != raw_token
    assert len(row.token_hash) == 64  # sha256 hex digest


# ── API: publish authz (404, not 403 — an Application's existence is content) ──


def test_ta_can_publish_for_assigned_consultant(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    actors.login(client, actors.ta)
    _publish(client, app["id"])


def test_unrelated_ta_gets_404_not_403(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    actors.login(client, actors.other_ta)
    r = client.post("/api/publications", json={"application_id": app["id"]})
    assert r.status_code == 404


def test_publish_requires_auth(client):
    r = client.post("/api/publications", json={"application_id": str(uuid.uuid4())})
    assert r.status_code == 401


# ── API: list scoping — no url/token in a listing ───────────────────────────


def test_list_never_carries_a_url_or_token(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    _publish(client, app["id"])

    listed = client.get("/api/publications").json()
    assert listed["total"] == 1
    row = listed["publications"][0]
    assert "url" not in row and "token" not in row and "token_hash" not in row
    assert row["application_id"] == app["id"]


def test_list_scoped_to_own_for_consultant(client, actors, second_pair, db):
    actors.login(client, actors.consultant)
    app1 = _apply(client, actors.consultant.username)
    _give_content(db, app1["id"])
    _publish(client, app1["id"])

    actors.login(client, second_pair["consultant"])
    app2 = _apply(client, second_pair["consultant"].username, title="Other Role")
    _give_content(db, app2["id"])
    _publish(client, app2["id"])

    actors.login(client, actors.consultant)
    listed = client.get("/api/publications").json()
    assert listed["total"] == 1
    assert listed["publications"][0]["consultant_username"] == actors.consultant.username


def test_admin_list_sees_all_publications(client, actors, second_pair, db):
    actors.login(client, actors.consultant)
    app1 = _apply(client, actors.consultant.username)
    _give_content(db, app1["id"])
    _publish(client, app1["id"])

    actors.login(client, second_pair["consultant"])
    app2 = _apply(client, second_pair["consultant"].username, title="Other Role")
    _give_content(db, app2["id"])
    _publish(client, app2["id"])

    actors.login(client, actors.admin)
    listed = client.get("/api/publications").json()
    assert listed["total"] == 2
    assert all("consultant_full_name" in p for p in listed["publications"])


# ── API: capability routes (meta / content / photo / opened) — header only ──


def test_capability_routes_reject_a_path_or_query_token(client, actors, db):
    """The forbidden-carrier list (§1 safety floor): a token in the path or a
    query string must not work at all — only the header does."""
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    # No route takes a path segment any more.
    assert client.get(f"/api/publications/{token}/meta").status_code == 404
    assert client.get(f"/api/publications/{token}/content").status_code == 404
    # A query string is not read either.
    assert client.get(f"/api/publications/meta?token={token}").status_code == 404
    # The header works.
    assert client.get("/api/publications/meta", headers=_cap_headers(token)).status_code == 200


def test_meta_and_content_missing_or_unknown_token_is_404(client):
    for path in ("meta", "content", "photo"):
        r = client.get(f"/api/publications/{path}")  # no header at all
        assert r.status_code == 404, path
        assert r.headers.get("X-Robots-Tag") == "noindex", path
    r = client.get("/api/publications/meta", headers=_cap_headers("not-a-real-token"))
    assert r.status_code == 404


def test_meta_and_content_revoked_is_410(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    service.revoke(db, row)

    for path in ("meta", "content", "photo"):
        r = client.get(f"/api/publications/{path}", headers=_cap_headers(token))
        assert r.status_code == 410, path
        assert r.headers.get("X-Robots-Tag") == "noindex", path
    r = client.post("/api/publications/opened", headers=_cap_headers(token))
    assert r.status_code == 410


def test_public_capability_responses_carry_strict_headers(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    r = client.get("/api/publications/meta", headers=_cap_headers(token))
    assert r.status_code == 200
    assert r.headers.get("X-Robots-Tag") == "noindex"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    assert r.headers.get("Cache-Control") == "no-store"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]


def test_every_publicly_reachable_response_forbids_caching(
    client, actors, db, tmp_path, monkeypatch
):
    """`Cache-Control: no-store` on every anonymous surface — success, the
    photo, the shell, and each refusal (404 / 410 / 429).

    ADR-009's viewer-isolation bullet and `docs/legal/dpia.md`'s planned-V2
    amendment both require it explicitly; it was absent until `STAB-5-R1`.
    Asserted as a **class** rather than on one route on purpose: the header
    lives in one dict precisely so no surface can drift out of the policy, and
    the way this went unnoticed was that each surface had its own test
    asserting the headers it happened to care about (the same lesson
    `S5-P2-R1` taught about the viewer's inline-handler CSP). `/content` is
    the one that matters most — its body is a whole tailored CV — but a
    per-route assertion is what missed this in the first place.
    """
    monkeypatch.setattr("backend.config.DATA_DIR", tmp_path / "profiles")
    monkeypatch.setattr("backend.config.CV_PUBLISH_PHOTO_DIR", tmp_path / "pub_photos")
    profile_dir = tmp_path / "profiles" / actors.consultant.username
    profile_dir.mkdir(parents=True)
    (profile_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff-alice-photo")

    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"], photo_src="/api/profiles/x/photo?t=1")
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    actors.logout(client)

    def _no_store(r, label: str) -> None:
        assert r.headers.get("Cache-Control") == "no-store", (
            f"{label} → {r.status_code}, Cache-Control="
            f"{r.headers.get('Cache-Control')!r}"
        )

    # Success paths, including the photo file response and the viewer shell.
    _no_store(client.get("/api/publications/meta", headers=_cap_headers(token)), "meta 200")
    _no_store(client.get("/api/publications/content", headers=_cap_headers(token)), "content 200")
    _no_store(client.get("/api/publications/photo", headers=_cap_headers(token)), "photo 200")
    _no_store(client.post("/api/publications/opened", headers=_cap_headers(token)), "opened 200")
    _no_store(client.get("/cv/view"), "shell 200")

    # Refusals: unknown token (404) and, once revoked, 410.
    _no_store(client.get("/api/publications/meta", headers=_cap_headers("nope")), "meta 404")
    service.revoke(db, db.get(CVPublication, uuid.UUID(pub["id"])))
    _no_store(client.get("/api/publications/content", headers=_cap_headers(token)), "content 410")

    # And STAB-5's 429, which reaches the client through `_public_http` too.
    monkeypatch.setattr(config, "CV_PUBLISH_PUBLIC_RATE_MAX_REQUESTS", 0)
    over = client.get("/api/publications/meta", headers=_cap_headers(token))
    assert over.status_code == 429
    _no_store(over, "meta 429")


def test_content_serves_the_sanitized_snapshot(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"], desc_html='<script>alert(1)</script><b>ok</b>')
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    r = client.get("/api/publications/content", headers=_cap_headers(token))
    assert r.status_code == 200
    desc = r.json()["cv_state"]["expList"][0]["assignments"][0]["descHtml"]
    assert "<script" not in desc.lower()
    assert "<b>ok</b>" in desc


def test_meta_can_edit_flag(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    actors.logout(client)  # a hiring manager holds no session at all
    anon = client.get("/api/publications/meta", headers=_cap_headers(token)).json()
    assert anon["can_edit"] is False
    assert anon["status"] == "published"

    actors.login(client, actors.consultant)
    owner = client.get("/api/publications/meta", headers=_cap_headers(token)).json()
    assert owner["can_edit"] is True


# ── API: opened beacon ───────────────────────────────────────────────────


def test_opened_anonymous_records_a_view(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    actors.logout(client)  # a hiring manager holds no session at all

    r = client.post("/api/publications/opened", headers=_cap_headers(token))
    assert r.status_code == 200
    assert r.json()["recorded"] is True

    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    db.refresh(row)
    assert row.view_count == 1 and row.last_viewed_at is not None

    # Second open same day: count increments.
    client.post("/api/publications/opened", headers=_cap_headers(token))
    db.refresh(row)
    assert row.view_count == 2


def test_opened_authorized_session_does_not_count(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    actors.login(client, actors.consultant)  # authorized viewer
    r = client.post("/api/publications/opened", headers=_cap_headers(token))
    assert r.status_code == 200
    assert r.json()["recorded"] is False

    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    assert row.view_count == 0


def test_view_notification_payload_carries_no_token(client, actors, db):
    from backend.notifications import service as notify_service

    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    actors.logout(client)  # a hiring manager holds no session at all

    client.post("/api/publications/opened", headers=_cap_headers(token))
    notes = notify_service.list_for_user(db, actors.ta.user_id)
    assert len(notes) == 1
    assert "token" not in notes[0].payload
    assert notes[0].payload["publication_id"] == pub["id"]


# ── API: extend / revoke by non-secret id ───────────────────────────────


def test_extend_and_revoke_from_owner(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    actors.login(client, actors.ta)
    assert client.post(f"/api/publications/{pub['id']}/extend").status_code == 200
    revoked = client.delete(f"/api/publications/{pub['id']}")
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": pub["id"]}
    assert client.get("/api/publications/content", headers=_cap_headers(token)).status_code == 410


def test_extend_revoke_unknown_is_404(client, actors):
    actors.login(client, actors.ta)
    assert client.post(f"/api/publications/{uuid.uuid4()}/extend").status_code == 404
    assert client.delete(f"/api/publications/{uuid.uuid4()}").status_code == 404


def test_unrelated_ta_cannot_extend_or_revoke(client, actors, second_pair, db):
    actors.login(client, second_pair["consultant"])
    app = _apply(client, second_pair["consultant"].username, title="Other Role")
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    actors.login(client, actors.ta)  # unrelated to second_pair's consultant
    assert client.post(f"/api/publications/{pub['id']}/extend").status_code == 403
    assert client.delete(f"/api/publications/{pub['id']}").status_code == 403
    assert client.get("/api/publications/content", headers=_cap_headers(token)).status_code == 200


# ── Photo copy-on-publish, keyed by the publication's id (never the token) ──


def test_photo_copied_on_publish_and_served_via_header(client, actors, db, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.config.DATA_DIR", tmp_path / "profiles")
    monkeypatch.setattr("backend.config.CV_PUBLISH_PHOTO_DIR", tmp_path / "pub_photos")
    profile_dir = tmp_path / "profiles" / actors.consultant.username
    profile_dir.mkdir(parents=True)
    (profile_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff-alice-photo")

    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"], photo_src="/api/profiles/x/photo?t=1")
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    assert row.photo_path and str(pub["id"]) in row.photo_path
    assert row.cv_state["photoSrc"] == "/api/publications/photo"

    r = client.get("/api/publications/photo", headers=_cap_headers(token))
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff-alice-photo"
    assert r.headers.get("X-Robots-Tag") == "noindex"


def test_photo_reference_dropped_when_no_file(client, actors, db, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.config.DATA_DIR", tmp_path / "profiles")
    monkeypatch.setattr("backend.config.CV_PUBLISH_PHOTO_DIR", tmp_path / "pub_photos")
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"], photo_src="/api/profiles/x/photo")
    pub = _publish(client, app["id"])
    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    assert row.photo_path is None
    assert row.cv_state["photoSrc"] is None


def test_photo_absent_is_404(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])  # no photoSrc
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    assert client.get("/api/publications/photo", headers=_cap_headers(token)).status_code == 404


# ── The confirmed-terminal-Application hook (S5-P1-R1 finding 3) ──────────


def test_terminal_transition_disables_the_publication_and_deletes_the_tailored_cv(
    client, actors, db
):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]

    r = client.post(f"/api/applications/{app['id']}/status", json={"status": "closed"})
    assert r.status_code == 200, r.text

    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    db.refresh(row)
    assert row.revoked is True
    assert db.scalar(
        select(TailoredCV).where(TailoredCV.application_id == uuid.UUID(app["id"]))
    ) is None
    # Atomic with the status move: the link is dead through the public route too.
    assert client.get("/api/publications/content", headers=_cap_headers(token)).status_code == 410


def test_a_retried_terminal_transition_does_not_double_delete_or_error(client, actors, db):
    """The double-click / retried-request case: a second request for the same
    terminal status on an already-terminal Application must not raise, and
    must not attempt a second delete/disable — the ordering rule in
    `transition()` refuses it before the hook ever runs a second time."""
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])

    first = client.post(f"/api/applications/{app['id']}/status", json={"status": "closed"})
    assert first.status_code == 200, first.text
    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    db.refresh(row)
    assert row.revoked is True

    # Idempotent no-op: re-requesting the status already held.
    second = client.post(f"/api/applications/{app['id']}/status", json={"status": "closed"})
    assert second.status_code == 200, second.text
    assert second.json()["changed"] is False

    # A different terminal status from a terminal one is refused outright.
    third = client.post(f"/api/applications/{app['id']}/status", json={"status": "withdrawn"})
    assert third.status_code == 409


def test_terminal_transition_with_no_publication_is_a_plain_status_change(client, actors, db):
    """The hook must not require a publication to exist — most Applications
    never get published at all."""
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    r = client.post(f"/api/applications/{app['id']}/status", json={"status": "withdrawn"})
    assert r.status_code == 200, r.text


def test_service_disable_for_application_is_a_no_op_without_content(db):
    """`transition()` calls this unconditionally on a terminal move; it must
    tolerate an Application that was never published."""
    service.disable_for_application(db, uuid.uuid4())  # no row, no error


# ── Publish refuses a terminal Application (S5-P2, `S5-P1-R2` finding 2) ───
#
# The real concurrent-interleaving proof (the reviewer's reproduction —
# application_status=closed, tailored_cv_count=0, active_publication_count=1
# — must be unreachable) needs two real transactions and a real row lock,
# which SQLite cannot express; that proof is
# tests/integration/test_postgres_semantics.py::test_publish_and_terminal_transition_cannot_both_win.
# These are the sequential-logic half: the ordering rule itself, exercised
# without needing a race to observe it.


def test_publish_refuses_an_already_terminal_application(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])

    closed = client.post(f"/api/applications/{app['id']}/status", json={"status": "closed"})
    assert closed.status_code == 200, closed.text

    r = client.post("/api/publications", json={"application_id": app["id"]})
    assert r.status_code == 409
    assert "ended" in r.json()["detail"]
    assert db.scalar(
        select(CVPublication).where(CVPublication.application_id == uuid.UUID(app["id"]))
    ) is None


# ── Every state-derived value the renderer places in an attribute is
# allowlisted or context-escaped, not only descHtml (S5-P2, `S5-P1-R2`
# finding 1) ─────────────────────────────────────────────────────────────


def test_a_malicious_presentation_value_cannot_survive_publish(client, actors, db):
    """The reviewer's worked case: a `pfFont` value that breaks out of the
    `style="..."` attribute `ts()` builds and injects a live element."""
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    payload = 'x"><img src=x onerror="alert(1)"><span style="'
    tailored = _give_content(db, app["id"])
    tailored.cv_state = {**tailored.cv_state, "pfFont": payload, "skColor": payload}
    db.commit()

    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    r = client.get("/api/publications/content", headers=_cap_headers(token))
    state = r.json()["cv_state"]

    assert state["pfFont"] != payload
    assert "<img" not in state["pfFont"]
    assert "onerror" not in state["pfFont"]
    assert state["skColor"] != payload
    assert "<img" not in state["skColor"]
    # Replaced with the renderer's own already-safe default for a missing
    # field (a publish refusal was the other option — see the module
    # docstring for why a default was chosen).
    assert state["pfFont"] == "'Source Sans 3',sans-serif"
    assert state["skColor"] == "#444"

    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    assert payload not in row.cv_state["pfFont"]


def test_a_real_generated_tailored_cv_publishes_its_presentation_unchanged(
    client, actors, db, profile_for, stub_llm
):
    """Check 3 — the one most likely to fail and the one that would hurt the
    demo: an allowlist narrower than the editor's own offering would silently
    mangle a real, legitimately curated CV. Uses the real generation pipeline
    (`tests/test_tailored_cv.py`'s pattern), not a hand-built fixture."""
    profile_for(actors.consultant.username)
    actors.login(client, actors.consultant)

    base = client.get(f"/api/cv/base/{actors.consultant.username}").json()
    saved = client.put(
        f"/api/cv/base/{actors.consultant.username}",
        json={"state": {**base["state"], **_CURATED_PRESENTATION}, "expected_revision": base["revision"]},
    )
    assert saved.status_code == 200, saved.text

    app = _apply(client, actors.consultant.username)
    r = client.post(f"/api/applications/{app['id']}/cv/generate")
    assert r.status_code == 202, r.text
    _drain(db)
    loaded = client.get(f"/api/applications/{app['id']}/cv").json()
    tailored_state = loaded["artifact"]["state"]
    for key, value in _CURATED_PRESENTATION.items():
        assert tailored_state[key] == value, f"generation did not carry {key} from the base CV"

    pub = _publish(client, app["id"])
    token = pub["url"].rsplit("#", 1)[1]
    published_state = client.get(
        "/api/publications/content", headers=_cap_headers(token)
    ).json()["cv_state"]

    for key, value in _CURATED_PRESENTATION.items():
        assert published_state[key] == value, (
            f"publish changed a legitimate {key} value — the allowlist is "
            "narrower than the editor's own offering"
        )


# ── Expiry blocks access but deletes nothing; only revoked rows are purged ──


def test_expired_not_serveable_but_not_purged(client, actors, db):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    row.expires_at = row.expires_at - timedelta(days=60)
    db.commit()

    assert service.is_serveable(row) is False
    assert service.purge_revoked(db)["purged"] == 0
    assert db.get(CVPublication, row.id) is not None  # still there, just blocked

    service.extend(db, row)
    assert service.is_serveable(row) is True


def test_purge_revoked_deletes_row_and_photo(db, actors, client, tmp_path):
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    row = db.get(CVPublication, uuid.UUID(pub["id"]))
    photo = tmp_path / "pub_photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff-fake-jpeg")
    row.photo_path = str(photo)
    db.commit()
    service.revoke(db, row)

    result = service.purge_revoked(db)
    assert result["purged"] == 1
    assert not photo.exists()
    assert db.get(CVPublication, row.id) is None


# ── The V1 application-status writer and reader stay retired ──────────────


def test_the_v1_application_status_writer_and_reader_are_gone(client):
    """`POST /api/profiles/{u}/accepted-positions/{job}/status` was the last
    writer of `verdicts.application_status`; V2 Slice 3 retired it (application
    state is `applications.status`, P-008) and it must not reappear.

    `GET /api/profiles/{u}/accepted-positions` was Slice 3's deliberately
    retained reader, kept for the editor's CV-publish "Published" group. STAB-7
    removed it too: the survey found zero callers in any shipped page — both
    the editor and the TA dashboard source published-CV state from
    `GET /api/publications` (Slice 5) instead, never from a verdict."""
    from backend import main

    assert not hasattr(main, "_resolve_status_transition")
    assert not hasattr(main, "_STATUS_VALUES")
    r = client.post(
        "/api/profiles/alice/accepted-positions/j1/status",
        json={"status": "applied"},
    )
    assert r.status_code == 404
    assert client.get("/api/profiles/alice/accepted-positions").status_code == 404

    routes = {getattr(r, "path", "") for r in main.app.routes}
    assert "/api/profiles/{username}/accepted-positions" not in routes
    assert (
        "/api/profiles/{username}/accepted-positions/{job_id}/status" not in routes
    )


# ── The anonymous capability exchange's global rate cap (STAB-5) ───────────
# The last of the five controls `docs/legal/dpia.md` requires before external
# CV-publish use. Every test here lowers the budget through `config` rather
# than issuing 60 real requests — the throttle reads its thresholds from the
# module on every call for exactly this reason. `tests/conftest.py`'s autouse
# `_fresh_public_capability_budget` is what keeps one test's spending out of
# the next one's.


def _budget(monkeypatch, max_requests: int, *, window_seconds: int = 60) -> None:
    monkeypatch.setattr(
        config, "CV_PUBLISH_PUBLIC_RATE_MAX_REQUESTS", max_requests
    )
    monkeypatch.setattr(
        config, "CV_PUBLISH_PUBLIC_RATE_WINDOW_SECONDS", window_seconds
    )


def _published_token(client, db, actors, *, title: str) -> str:
    app = _apply(client, actors.consultant.username, title=title)
    _give_content(db, app["id"])
    return _publish(client, app["id"])["url"].rsplit("#", 1)[1]


def test_the_capability_budget_is_global_not_per_token(
    client, actors, db, monkeypatch
):
    """Two live publications share one budget.

    This is the discriminating shape: four requests split two-and-two across
    two different tokens exhaust a budget of four, so the fifth is refused
    even though *that* token has only been used twice. A per-token counter
    would still be admitting it — which is the point, since the abuse worth
    bounding is a script walking many different tokens.
    """
    actors.login(client, actors.consultant)
    token_a = _published_token(client, db, actors, title="Senior Dev @ ACME")
    token_b = _published_token(client, db, actors, title="Staff Dev @ ACME")
    assert token_a != token_b
    _budget(monkeypatch, 4)

    for token in (token_a, token_a, token_b, token_b):
        r = client.get("/api/publications/meta", headers=_cap_headers(token))
        assert r.status_code == 200, r.text

    over = client.get("/api/publications/meta", headers=_cap_headers(token_a))
    assert over.status_code == 429


def test_the_capability_budget_is_shared_across_all_four_routes(
    client, actors, db, monkeypatch
):
    """One limiter at one choke point (`_require_pub`), not four independent
    ones: a full real viewer session — meta, content, photo, opened — spends
    four requests from the same budget."""
    actors.login(client, actors.consultant)
    token = _published_token(client, db, actors, title="Senior Dev @ ACME")
    actors.logout(client)  # an anonymous hiring manager, not the consultant
    _budget(monkeypatch, 4)

    assert client.get("/api/publications/meta", headers=_cap_headers(token)).status_code == 200
    assert client.get("/api/publications/content", headers=_cap_headers(token)).status_code == 200
    # No photo on this publication — 404 is the route's own answer, and it
    # still consumed budget, which is what this test is about.
    assert client.get("/api/publications/photo", headers=_cap_headers(token)).status_code == 404
    assert client.post("/api/publications/opened", headers=_cap_headers(token)).status_code == 200

    for path, call in (
        ("meta", client.get),
        ("content", client.get),
        ("photo", client.get),
    ):
        r = call(f"/api/publications/{path}", headers=_cap_headers(token))
        assert r.status_code == 429, path
    assert client.post(
        "/api/publications/opened", headers=_cap_headers(token)
    ).status_code == 429


def test_the_over_budget_response_carries_the_full_public_header_set(
    client, actors, db, monkeypatch
):
    """A 429 is a publicly reachable response, so it gets the same headers the
    404/410 paths do — `_public_http` attaches them to the exception because a
    raised `HTTPException` discards the injected Response's headers."""
    actors.login(client, actors.consultant)
    token = _published_token(client, db, actors, title="Senior Dev @ ACME")
    _budget(monkeypatch, 1)

    assert client.get("/api/publications/meta", headers=_cap_headers(token)).status_code == 200
    r = client.get("/api/publications/meta", headers=_cap_headers(token))
    assert r.status_code == 429
    assert r.headers.get("X-Robots-Tag") == "noindex"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    assert r.headers.get("Cache-Control") == "no-store"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # No timing detail handed back: the caller learns it is over budget and
    # nothing about the window.
    assert "Retry-After" not in r.headers


def test_the_throttle_leaks_no_signal_about_token_validity(
    client, actors, db, monkeypatch
):
    """Once the budget is gone, a live token, a revoked one, a nonsense one,
    and no header at all are indistinguishable — same status, same body.

    Without the rate check running *before* the token lookup, these would
    answer 200 / 410 / 404 / 404 respectively and the throttle would itself
    become the enumeration oracle the rest of this module exists to avoid.
    """
    actors.login(client, actors.consultant)
    live = _published_token(client, db, actors, title="Senior Dev @ ACME")
    revoked_app = _apply(client, actors.consultant.username, title="Lead Dev @ ACME")
    _give_content(db, revoked_app["id"])
    revoked_pub = _publish(client, revoked_app["id"])
    revoked = revoked_pub["url"].rsplit("#", 1)[1]
    service.revoke(db, db.get(CVPublication, uuid.UUID(revoked_pub["id"])))
    _budget(monkeypatch, 0)  # nothing at all is within budget

    answers = [
        client.get("/api/publications/meta", headers=_cap_headers(live)),
        client.get("/api/publications/meta", headers=_cap_headers(revoked)),
        client.get("/api/publications/meta", headers=_cap_headers("not-a-real-token")),
        client.get("/api/publications/meta"),
    ]
    assert [r.status_code for r in answers] == [429, 429, 429, 429]
    assert len({r.text for r in answers}) == 1, [r.text for r in answers]


def test_requests_succeed_again_once_the_window_has_elapsed(
    client, actors, db, monkeypatch
):
    """Asserted by moving the window's own start time into the past, not by
    sleeping — the codebase's established pattern for time-bounded state
    (`locked_until` in STAB-2)."""
    actors.login(client, actors.consultant)
    token = _published_token(client, db, actors, title="Senior Dev @ ACME")
    _budget(monkeypatch, 1, window_seconds=60)

    assert client.get("/api/publications/meta", headers=_cap_headers(token)).status_code == 200
    assert client.get("/api/publications/meta", headers=_cap_headers(token)).status_code == 429

    public_capability_throttle.window_started_at -= 61
    r = client.get("/api/publications/meta", headers=_cap_headers(token))
    assert r.status_code == 200, r.text
    # A fresh window, holding only the request that just reopened it.
    assert public_capability_throttle.request_count == 1


def test_the_authenticated_management_routes_are_not_throttled(
    client, actors, db, monkeypatch
):
    """Scope is the anonymous surface only. The management routes sit behind a
    session and STAB-2's per-account login lockout — a different boundary —
    so a burst of them never 429s from this mechanism, even with the public
    budget set to zero."""
    actors.login(client, actors.consultant)
    app = _apply(client, actors.consultant.username)
    _give_content(db, app["id"])
    pub = _publish(client, app["id"])
    _budget(monkeypatch, 0)

    for _ in range(10):
        assert client.get("/api/publications").status_code == 200
    assert client.post(f"/api/publications/{pub['id']}/extend").status_code == 200
    assert client.post(
        "/api/publications", json={"application_id": app["id"]}
    ).status_code == 201
    assert client.delete(f"/api/publications/{pub['id']}").status_code == 200


def test_the_configured_budget_is_sixty_requests_per_minute():
    """Pinned so the shipped proportionality decision is a test, not a comment:
    generous enough that a real viewer's four-request session never comes
    close, tight enough to slow a bulk script by orders of magnitude."""
    assert config.CV_PUBLISH_PUBLIC_RATE_MAX_REQUESTS == 60
    assert config.CV_PUBLISH_PUBLIC_RATE_WINDOW_SECONDS == 60


def test_the_throttle_counts_arrivals_and_fails_open_on_a_fresh_process(
    monkeypatch,
):
    """Two properties of the counter itself, away from the routes.

    A refused request is still counted (the count is an honest record of
    arrivals; in a fixed window that changes nothing about when the window
    ends), and a brand-new instance — what a restarted process gets — starts
    with the whole budget available rather than locked.
    """
    from backend.cv_publish.throttle import PublicCapabilityThrottle

    _budget(monkeypatch, 2)
    fresh = PublicCapabilityThrottle()
    assert [fresh.allow() for _ in range(4)] == [True, True, False, False]
    assert fresh.request_count == 4

    restarted = PublicCapabilityThrottle()
    assert restarted.allow() is True
