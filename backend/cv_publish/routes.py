"""CV publication endpoints (ADR-009, D-013, P-010).

`api_router` (/api/publications) mixes authenticated management routes
(publish, list, extend, revoke — all keyed by the publication's non-secret
`id`) with four intentionally-unauthenticated capability routes the
hiring-manager viewer calls: `meta`, `content`, `photo`, `opened`. None of
them carry the raw capability token in a path or query string — the viewer
sends it in the `X-CV-Publication-Token` header instead, so it is absent from
URLs, browser history, `Referer` headers, and server access logs.
`public_router` carries the public viewer shell at `GET /cv/view` — a single
token-agnostic route; the raw token lives only in the URL *fragment*
(`/cv/view#<token>`), which browsers never transmit to any server.

Every unauthenticated response carries the full `_PUBLIC_HEADERS` set —
a restrictive CSP plus `Cache-Control: no-store`, so a revoked link's content
cannot be re-served from a browser or intermediary cache (`docs/plans/v2-pilot.md`
§11, "before an external public link": viewer sanitization and security-header
tests). Unknown/missing tokens 404 (no enumeration signal); expired/revoked
publications 410.

The four capability routes also share **one global request-rate cap**
(`backend/cv_publish/throttle.py`, STAB-5), applied in `_require_pub` before
any token lookup so an exhausted budget cannot distinguish a live token from a
bogus one. The authenticated management routes are deliberately **not**
throttled by it — they sit behind a session and `STAB-2`'s per-account login
lockout, a different boundary; the DPIA's "anonymous exchange" wording is
specifically about the unauthenticated surface.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.auth.dependencies import get_current_user, get_optional_user
from backend.cv_publish import service, throttle
from backend.cv_publish.service import SERVICE_ERRORS
from backend.db.models import CVPublication, User
from backend.db.session import get_db
from backend.authz import policy as authz

api_router = APIRouter(prefix="/api/publications", tags=["cv-publish"])
# Public viewer shell served at /cv/view (Session 2; token-agnostic since D-013 —
# see module docstring). Kept on a separate router so its registration and the
# noindex/CSP posture are already in place.
public_router = APIRouter(tags=["cv-publish"])

# `backend/templates/`, not `html/` — S5-P2 (`S5-P1-R2` finding 1): `html/` is
# generically mounted at /html (backend/main.py), so a copy of the shell there
# was a fully working, header-less alias of this route that carried none of
# `_PUBLIC_HEADERS`. Moving the file is what makes `GET /cv/view` the only way
# to reach it, rather than relying on two places serving identical content to
# stay in sync. `cv-render.js`/`cv-render.css`/`cv-viewer.js` stay mounted at
# /html — they are static assets with no page of their own, inert without this
# shell to load them into.
_VIEWER_FILE = (
    Path(__file__).resolve().parent.parent / "templates" / "cv-viewer.html"
)

#: The capability token travels in a header, never a path or query string —
#: the forbidden-carrier list this whole module exists to keep empty
#: (`docs/plans/v2-pilot.md` §1's safety floor and §11's promotion gate).
TOKEN_HEADER = "X-CV-Publication-Token"

#: Strict headers for every publicly reachable response (the four capability
#: routes and the viewer shell). The CSP allows `'unsafe-inline'` for
#: `style-src` only — the shared render core (`html/cv-render.js`) draws the CV
#: through per-element inline `style="..."` attributes, which is a genuine
#: rendering need, not a script-execution path. `script-src 'self'` has no
#: matching relaxation: the viewer's own logic lives in `html/cv-viewer.js`
#: (an external file, loaded by src=), never an inline `<script>` block, so no
#: exception is needed there.
_PUBLIC_HEADERS = {
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # ADR-009's viewer-isolation bullet and `docs/legal/dpia.md`'s planned-V2
    # amendment both require this explicitly, and it was missing until
    # `STAB-5-R1` found it. It matters most on `/content`, whose body is a
    # whole tailored CV served to an anonymous caller: without it, nothing
    # forbids the browser or an intermediary (CloudFront sits in front of this
    # origin) from retaining a copy and re-serving it after the publication is
    # revoked or expires — which would defeat Revoke, the control ADR-009
    # leans on hardest. `no-store` rather than `no-cache`: the latter permits
    # storing and merely requires revalidation.
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'none'"
    ),
}


def _publish_url(raw_token: str) -> str:
    """A capability link the token never sent to a server can leak from a
    path, a query string, an access log, or a `Referer` header (§1 safety
    floor) — the fragment is client-side only."""
    return f"{config.APP_BASE_URL.rstrip('/')}/cv/view#{raw_token}"


def _view_status(pub: CVPublication) -> str:
    """Publication view state as the status-machine name."""
    return "link_opened" if (pub.view_count or 0) > 0 else "published"


def _can_edit(db: Session, user: User | None, consultant_username: str) -> bool:
    """True when `user` is a logged-in owner/TA/admin for this consultant.

    Doubles as the "authorized session" test for the opened beacon: an
    authorized viewer never counts as a hiring-manager view.
    """
    if user is None:
        return False
    advisee = authz.get_advisee_by_username(db, consultant_username)
    if advisee is None:
        return False
    return authz.can_access_consultant(db, user, advisee)


def _apply_public_headers(response: Response) -> None:
    for key, value in _PUBLIC_HEADERS.items():
        response.headers[key] = value


def _public_http(status_code: int, detail: str) -> HTTPException:
    """An HTTPException that still carries the full public header set.

    A plain `raise HTTPException` discards the injected Response's headers —
    Starlette builds a fresh error response from `exc.headers` — so the
    404/410 paths on the public capability routes must set headers on the
    exception itself, not on the injected `response`.
    """
    return HTTPException(status_code=status_code, detail=detail, headers=_PUBLIC_HEADERS)


def _require_pub(db: Session, raw_token: str | None) -> CVPublication:
    """Resolve a capability-route request's publication, or raise 429/404/410.

    A missing header answers exactly like an unknown token — no signal that
    distinguishes "you forgot the header" from "that token does not exist".

    The rate check is deliberately **first**, before the token is looked up at
    all (STAB-5). Two reasons: an exhausted budget must answer identically for
    a valid, live token and a bogus one — checking validity first would leak
    404-vs-429 through the throttle — and a refused request should not cost a
    token-hash lookup. Every one of the four capability routes calls this
    helper before touching the database, which is why the throttle lives here
    and not in four places. The 429 carries the full `_PUBLIC_HEADERS` set the
    same way 404/410 do, and no `Retry-After`: the caller learns it is over
    budget and nothing about the window's internal timing.
    """
    if not throttle.public_capability_throttle.allow():
        raise _public_http(429, "Too many requests")
    pub = service.get_by_token(db, raw_token) if raw_token else None
    if pub is None:
        raise _public_http(404, "Publication not found")
    if not service.is_serveable(pub):
        raise _public_http(410, "This CV is no longer available")
    return pub


# ── Authenticated management (keyed by the publication's non-secret id) ────


class PublishIn(BaseModel):
    application_id: str


@api_router.post("", status_code=201)
def create_publication(
    body: PublishIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Publish (or republish) the Application's tailored CV.

    Authz is `ensure_application_access` (404, not 403 — an Application's
    existence is itself content, same as `backend/tailored_cv/routes.py`). The
    snapshot, title, and consultant/job identity are all derived server-side
    from the Application's own tailored CV; the caller supplies nothing but
    which Application to publish.
    """
    application = authz.ensure_application_access(db, actor, body.application_id)
    try:
        pub, raw_token = service.publish_for_application(
            db, application=application, actor=actor
        )
    except SERVICE_ERRORS as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    return {**pub.to_dict(), "url": _publish_url(raw_token)}


@api_router.get("")
def list_publications(
    consultant_username: str | None = Query(default=None),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """No `url`/token in a listing — a raw token can never be reconstructed
    from its hash, so only a *newly minted* publish/republish response ever
    carries one (P-010's accepted trade-off; see `docs/DEFERRED.md`)."""
    if authz.is_admin(actor):
        pubs = (
            service.list_for_usernames(db, {consultant_username})
            if consultant_username
            else service.list_all(db)
        )
    elif authz.is_ta(actor):
        usernames = {c.username for c in authz.consultants_for_ta(db, actor.id)}
        if consultant_username:
            usernames &= {consultant_username}
        pubs = service.list_for_usernames(db, usernames)
    else:  # consultant → only their own
        pubs = service.list_for_usernames(db, {actor.username})
    # Resolve display names once (admins can't reach GET /api/ta/consultants
    # without a ta_id, so the dashboard can't otherwise show names for them).
    usernames = {p.consultant_username for p in pubs}
    names = {
        u.username: (u.full_name or u.username)
        for u in db.scalars(select(User).where(User.username.in_(usernames)))
    } if usernames else {}
    return {
        "publications": [
            {
                **p.to_dict(),
                "status": _view_status(p),
                "consultant_full_name": names.get(
                    p.consultant_username, p.consultant_username
                ),
            }
            for p in pubs
        ],
        "total": len(pubs),
    }


@api_router.post("/{pub_id}/extend")
def extend_publication(
    pub_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    pub = service.get_by_id(db, pub_id)
    if pub is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    authz.ensure_consultant_access(db, actor, pub.consultant_username)
    if not service.is_serveable(pub):
        # An expired or revoked publication is renewed by republishing, not
        # extending — keep 410 consistent with the public routes.
        raise HTTPException(status_code=410, detail="Publication no longer active")
    service.extend(db, pub)
    return pub.to_dict()


@api_router.delete("/{pub_id}")
def revoke_publication(
    pub_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    pub = service.get_by_id(db, pub_id)
    if pub is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    authz.ensure_consultant_access(db, actor, pub.consultant_username)
    service.revoke(db, pub)
    return {"revoked": str(pub.id)}


# ── Public (capability-token-via-header) ───────────────────────────────────


@api_router.get("/meta")
def publication_meta(
    response: Response,
    x_cv_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict:
    _apply_public_headers(response)
    pub = _require_pub(db, x_cv_token)
    return {
        "title": pub.title,
        "consultant_username": pub.consultant_username,
        "can_edit": _can_edit(db, user, pub.consultant_username),
        "expires_at": pub.expires_at.isoformat() if pub.expires_at else None,
        "status": _view_status(pub),
        "view_count": pub.view_count,
    }


@api_router.get("/content")
def publication_content(
    response: Response,
    x_cv_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
) -> dict:
    _apply_public_headers(response)
    pub = _require_pub(db, x_cv_token)
    return {"cv_state": pub.cv_state}


@api_router.get("/photo")
def publication_photo(
    x_cv_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
) -> FileResponse:
    """The publication-owned photo copy (data minimisation: header-token only,
    never a path/query parameter). Same 404/410 surface as the other
    capability routes. 404 (not 410) when the publication has no photo on
    disk — an absent photo is not an expired link."""
    pub = _require_pub(db, x_cv_token)
    if not pub.photo_path or not os.path.exists(pub.photo_path):
        raise _public_http(404, "No photo")
    return FileResponse(pub.photo_path, headers=_PUBLIC_HEADERS)


@api_router.post("/opened")
def publication_opened(
    response: Response,
    x_cv_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict:
    """Human-interaction beacon from the viewer. No-op for authorized sessions
    (the consultant/TA viewing their own work never counts as a HM view)."""
    _apply_public_headers(response)
    pub = _require_pub(db, x_cv_token)
    if _can_edit(db, user, pub.consultant_username):
        return {"recorded": False}
    service.record_view(db, pub)
    return {"recorded": True}


# ── Public viewer shell ───────────────────────────────────────────────────


@public_router.get("/cv/view")
def serve_viewer() -> FileResponse:
    """Serve the lean hiring-manager viewer shell — token-agnostic.

    The token lives only in the URL fragment (`/cv/view#<token>`), which the
    browser never sends to any server, so this route takes no path parameter
    at all. The shell's own script reads `location.hash` and sends the token
    to the capability routes via the `X-CV-Publication-Token` header,
    rendering the friendly not-found (404) / expired (410) states client-side.
    """
    return FileResponse(
        _VIEWER_FILE,
        media_type="text/html",
        headers=_PUBLIC_HEADERS,
    )
