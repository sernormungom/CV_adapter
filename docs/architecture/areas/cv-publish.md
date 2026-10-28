# CV publishing — subsystem contract

**Owns:** `backend/cv_publish/`, `backend/templates/cv-viewer.html`,
`html/cv-viewer.js`, table `cv_publications`, `data/cv_publications/photos/`.
**Decisions:** ADR-009 (the capability-link decision itself), D-013 / P-010.
**Read alongside:** [cv-artifacts.md](cv-artifacts.md) (the tailored CV this
snapshots), [applications.md](applications.md) (lock order, terminal
transitions), [ADR-009](../adr/ADR-009-cv-publish-capability-links.md), and
[dpia.md](../../legal/dpia.md) (the five controls required before external
use).

What it is: a shareable, capability-linked, **read-only** web version of an
**Application-owned tailored CV**, for a hiring manager. A publication is a
server-derived snapshot. There is no publication-edit mode — to change what is
published, edit the tailored CV and Publish again.

---

## Contract

### Modules

| File | Responsibility |
|---|---|
| `service.py` | Every write. Publish/extend/revoke/purge, view recording, photo snapshot. |
| `routes.py` | `api_router` (authenticated management) + the four anonymous capability routes + the viewer shell. Owns `_PUBLIC_HEADERS`. |
| `sanitize.py` | `sanitize_cv_state()` — runs on the **write** path, so reads never sanitize. |
| `throttle.py` | One module-level fixed-window request cap for the anonymous surface. |

### Routes

| Route | Auth | Notes |
|---|---|---|
| `POST /api/publications` | session; `ensure_application_access` | Body is only `{"application_id": …}`. Returns the raw token **once**. |
| `GET /api/publications` | session | Role-scoped listing. Carries no `url` and no token. |
| `POST /api/publications/{id}/extend` | session | Keyed by the publication's own non-secret `id`. |
| `DELETE /api/publications/{id}` | session | Revoke. |
| `GET /api/publications/meta`, `/content`, `/photo`, `POST /opened` | **anonymous** | No path parameter. Token arrives in the `X-CV-Publication-Token` header. |
| `GET /cv/view` | anonymous | The single token-agnostic viewer shell, served from `backend/templates/`. |

Public link shape: `/cv/view#<raw-token>` — a **fragment**, which a browser
never sends to any server, in a path/query, or in a `Referer` header.

### Storage — `cv_publications` (`backend/db/models/cv_publish.py`)

`token_hash` (unique, sha256 hex) · `application_id` (FK, NOT NULL) ·
`consultant_username` / `job_id` / `title` (denormalized display columns,
derived **server-side** at publish time, never client-supplied) · `cv_state`
JSONB (sanitized snapshot, never rendered HTML) · `photo_path` (named by this
row's `id`) · `created_by_id` · `expires_at` · `view_count` ·
`last_viewed_at` · `revoked`.

Partial unique index `uq_cv_publications_active_application` on
`application_id WHERE NOT revoked`. `to_dict()` never emits `token_hash`, a raw
token, or `cv_state`.

### UI entry point — `html/published-cvs.html` (V3-P-003, S6)

The focused Published CVs dashboard, one row per publication the caller may
reach: `GET /api/publications` (already role-scoped by the backend — a
Consultant sees only their own, a TA only assigned consultants, an Admin
every publication), a status pill (revoked / expired / active, with view
count and last-viewed), an expiry pill, an unconditional **Open CV** button
(the row's own `application_id`/`consultant_username`, always authorized
since the row would not be listed otherwise — see below), and conditional
**Extend**/**Revoke** buttons keyed by the publication's `id`
(`POST /api/publications/{id}/extend`, `DELETE /api/publications/{id}`). No
"Copy link" — see the accepted limitation below. A `?consultant_id=` on the
page's URL (carried by the top-nav links, or a Consultants-page row link) is
resolved through `html/auth-guard.js`'s `resolveConsultantContext()` against
`GET /api/me/consultants` before it is shown, filtered on, or forwarded — an
unauthorized, stale, or unresolvable value is dropped rather than used —
and, once resolved, is forwarded as the endpoint's own `consultant_username`
filter: a server-side narrowing of the same already-authorized result, not a
new capability (S6 correction round 1; `tests/test_published_cvs_client_behaviour.py`).

**Open CV** (S6 correction round 1) opens `html/cv-importer.html`'s existing
editor application mode at `?application=<application_id>&consultant_id=<consultant_username>`
— the same convention `html/applications.html`'s tailored-CV controls already
use, so the base/tailored artifact split (P-009) is unaffected. It is offered
regardless of the publication's own state (active, expired, or revoked): the
underlying Application and its tailored CV outlive the publication's
lifecycle, and an expired link — which Extend refuses — is republished by
opening the same editor first, not by a different destination.

This lived embedded in `html/ta-dashboard.html` (TA/Admin only) through V3
packet S5; S6 gave it its own page and, since `GET /api/publications` already
supported a Consultant caller, extended reach to a plain Consultant session
for the first time on the client side — V3-P-003's Consultant matrix includes
Published CVs.

### Config

`CV_PUBLISH_TTL_DAYS` (28) · `CV_PUBLISH_PHOTO_DIR`
(`data/cv_publications/photos`) · `CV_PUBLISH_PUBLIC_RATE_MAX_REQUESTS` (60) ·
`CV_PUBLISH_PUBLIC_RATE_WINDOW_SECONDS` (60).

---

## Guards

Everything required to change this subsystem safely is in this section. The
History section below is background only — you never need it to implement.

### G1 — The capability token exists only as a hash and only in a header

Never in a URL path, query string, database row, log, or notification payload.
Only `token_hash` is stored (`backend.auth.security.generate_token` /
`hash_token`). Transport is the `X-CV-Publication-Token` header; the link
carries the raw token in a URL fragment.
*Why:* public-token safety is a non-negotiable floor; a path/query token
leaks through logs, proxies and `Referer`.
*Pinned by:* `tests/test_cv_publish.py::test_only_the_hash_is_ever_stored`,
`::test_capability_routes_reject_a_path_or_query_token`,
`::test_view_notification_payload_carries_no_token`,
`::test_list_never_carries_a_url_or_token`.

### G2 — Every publish mints a fresh token and invalidates the previous link

First publish and republish alike. The raw token is returned exactly once, in
that response, and never persisted or logged. An existing publication is
updated **in place** (rotated token, refreshed snapshot, reset expiry), not
superseded by a second row.
*Why:* P-010's "Regenerate invalidates the previous link".
*Pinned by:* `tests/test_cv_publish.py::test_republish_rotates_the_token_and_updates_the_snapshot`.

### G3 — Sanitize on the write path, and replace rather than refuse

`sanitize_cv_state()` runs in `publish_for_application` before storage.
Two techniques: `sanitize_rich_text()` (stdlib `HTMLParser`, fixed tag
vocabulary, **zero attributes on any tag**) for the one raw-HTML field,
`expList[*].assignments[*].descHtml`; and an allowlist for the **twelve**
presentation fields (`{pf,sk,ex,ed}{Font,Color,Weight}`), transcribed from the
editor's own controls — fixed `<select>` values for Font/Weight,
`^#[0-9a-fA-F]{6}$` for Color.
*Why:* `html/cv-render.js`'s `ts(prefix)` builds a `style="…"` attribute from
those twelve with **no escaping at all**, and `bodyHTML` interpolates
`descHtml` as raw HTML. An out-of-allowlist value is replaced with `ts()`'s own
default for a missing field rather than refusing the publish, so a real curated
CV never renders differently.
*Pinned by:* `tests/test_cv_publish.py::test_a_malicious_presentation_value_cannot_survive_publish`,
`::test_a_real_generated_tailored_cv_publishes_its_presentation_unchanged`, and
all of `tests/test_cv_publish_sanitize.py`.

### G4 — Publish takes the Application row lock first and refuses a terminal Application

`publish_for_application` calls `backend.applications.service.lock_application`
and refuses (409) a terminal Application **before reading anything else**.
*Why:* without it, Publish could read a still-live tailored CV, race a
concurrent terminal transition's cleanup, and commit a publication for an
Application that had just been closed. Holding the same lock `transition()`
takes leaves only the two safe orderings.
*Lock order (do not reorder):* Job row → pair row → Application row → tailored
CV row → publication.
*Pinned by:* `tests/integration/test_cv_publish_pg.py::test_terminal_transition_wins_first_and_publish_is_refused`,
`::test_publish_wins_first_and_terminal_cleanup_then_revokes_it`,
`tests/test_cv_publish.py::test_publish_refuses_an_already_terminal_application`.

### G5 — A terminal Application disables the publication in the status commit

When an Application newly reaches `closed` or `withdrawn`,
`backend/applications/service.py::transition` calls `disable_for_application()`
and deletes the `TailoredCV` row **in the same transaction** as the status
commit — one commit publishes both effects or neither.
*Why:* a closed Application must not leave a live public link.
*Implementation note:* `applications/service.py` imports
`disable_for_application` **locally, inside `transition()`** — a module-level
import is a genuine circular import, since `cv_publish` imports
`lock_application` from that module. Applications stays the lower-level module.
*Pinned by:* `tests/test_cv_publish.py::test_terminal_transition_disables_the_publication_and_deletes_the_tailored_cv`,
`::test_a_retried_terminal_transition_does_not_double_delete_or_error`,
`::test_terminal_transition_with_no_publication_is_a_plain_status_change`.

### G6 — `Cache-Control: no-store` on every publicly reachable response

Part of `_PUBLIC_HEADERS`, alongside noindex, `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and the CSP.
*Why:* this is a required control, not hygiene (ADR-009's viewer-isolation
bullet; `docs/legal/dpia.md`). `/content` returns a whole tailored CV to an
anonymous caller and CloudFront sits in front of the origin — without it,
nothing forbids a cache re-serving that CV **after Revoke**, the control
ADR-009 leans on hardest. It was missing until STAB-5-R1.
*Pinned by:* `tests/test_cv_publish.py::test_every_publicly_reachable_response_forbids_caching`
— which asserts across success, photo, shell, 404, 410 and 429 **at once**,
because per-route header assertions are exactly what let it go missing.

### G7 — The viewer shell carries no inline script and no `on*` attribute

Its CSP is `script-src 'self'` with **no** `'unsafe-inline'`, so every control
is bound from the external `html/cv-viewer.js` (`armDownload()` binds Download
PDF's `window.print()`). `style-src` does allow `'unsafe-inline'`, because the
shared render core relies on per-element inline `style="…"`.
*Why:* an inline handler under this CSP is a silently dead button. S5-P2-R1
found exactly that — the CSP and the button were each asserted in separate
tests that never met.
*Pinned by:* `tests/test_static_pages.py::test_the_hardened_viewer_shell_has_no_inline_event_handler_its_csp_would_kill`
(pins the **class** of defect, not one attribute) plus step 8 of
`docs/testing.md`'s manual pilot walkthrough, which clicks the real button —
no static test substitutes for that.

### G8 — One shell, one route

The shell lives in `backend/templates/cv-viewer.html`, deliberately **not**
`html/`, which `backend/main.py` mounts generically at `/html`.
*Why:* a copy under `html/` is a fully working, header-less alias of `/cv/view`
— same content, none of `_PUBLIC_HEADERS`, reachable with the same fragment
token (S5-P2, `S5-P1-R2` finding 1). The static assets `cv-render.css`,
`cv-render.js` and `cv-viewer.js` **do** stay under `/html`: inert without a
shell to load them.
*Pinned by:* `tests/test_static_pages.py::test_the_viewer_shell_has_no_header_less_alias_under_the_html_mount`,
`::test_public_viewer_route_serves_shell_with_noindex`.

### G8a — The viewer's own behavior: opened-once, edit-gated, no page markers

`html/cv-viewer.js` reads the token from `location.hash`, fetches `meta` +
`content`, then:
- fires the `opened` beacon **once**, on first human interaction
  (`beaconSent` guard + `{once:true}` listeners) — never on load, so a
  crawler or prefetch does not count as a view;
- shows **Edit** only when `meta.can_edit` is true, linking to
  `/?consultant_id=<username>` — the ordinary base/Application editor, since
  there is no publication-edit route to link to (a publication has no edit
  mode at all — see the top of this file);
- sets `SHOW_PAGE_MARKERS = false`, so the editor's dashed pagination-preview
  markers never render for a hiring manager.

*Pinned by:* `tests/test_static_pages.py::test_viewer_beacon_fires_once`,
`tests/test_cv_publish.py::test_meta_can_edit_flag`,
`::test_opened_authorized_session_does_not_count`.

### G9 — The rate cap runs before the token is looked up, and is global

Checked in `_require_pub`, the single choke point all four capability routes
call, **before** any token lookup, so an exhausted budget answers 429
identically for a live, revoked, unknown, and absent token. The 429 carries the
full `_PUBLIC_HEADERS` set and deliberately no `Retry-After`.
*Why it is global:* per-token resets for free on every new guess; per-IP is
meaningless here because nothing in `backend/` reads `X-Forwarded-For` and
behind the ALB `request.client.host` is the balancer.
*Do not describe this as brute-force protection.* `generate_token()` is 256
bits — guessing is infeasible at any rate. It bounds automated abuse and
resource use on a surface with no identity check (ADR-009's "modest request
throttling").
*Accepted limits:* in-process and unlocked, so a restart resets the budget
(fail-open, the safe direction) and the count can drift under concurrency; it
throttles per Python process, so the effective cap multiplies by worker count —
theoretical today (no `--workers`, ECS desired count 1). Authenticated
management routes are deliberately not throttled.
*Pinned by:* `tests/test_cv_publish.py::test_the_throttle_leaks_no_signal_about_token_validity`,
`::test_the_capability_budget_is_global_not_per_token`,
`::test_the_capability_budget_is_shared_across_all_four_routes`,
`::test_the_over_budget_response_carries_the_full_public_header_set`,
`::test_the_authenticated_management_routes_are_not_throttled`,
`::test_requests_succeed_again_once_the_window_has_elapsed`,
`::test_the_configured_budget_is_sixty_requests_per_minute`.
*Test-suite dependency:* `tests/conftest.py`'s autouse
`_fresh_public_capability_budget` resets the counter per test — without it the
session shares one 60-request minute and public-route tests pass or fail
depending on their neighbours.

### G10 — Expiry blocks access but deletes nothing; only `revoked` rows are purged

`is_serveable()` refuses an expired row with 410, but `purge_revoked()` (called
from `retention/service.py::run_retention_purges`) targets `revoked = true`,
**not** `expires_at <= now()`. A row becomes `revoked` only through an explicit
Revoke or the terminal-Application hook — never by time passing.
*Pinned by:* `tests/test_cv_publish.py::test_expired_not_serveable_but_not_purged`,
`::test_purge_revoked_deletes_row_and_photo`.

### G11 — Publish answers 404; extend/revoke answer 404 or 403, and the split is deliberate

The two management surfaces are keyed by different identifiers and protect
different secrets. Do not flatten them into one rule.

**`POST /api/publications` → 404, never 403.** The caller supplies an
**Application id**, and an Application's existence is itself content — the same
convention as Jobs and tailored CVs. `authz.ensure_application_access`.

**`POST /{id}/extend` and `DELETE /{id}` → 404 unknown, 403 unauthorized.**
These name a **publication id** (`uuid4`, non-secret, already held by anyone
entitled to manage it). `routes.py` raises 404 when no such row exists, then
calls `authz.ensure_consultant_access`, which raises **403** for a known
publication belonging to a consultant the actor does not reach. What a 403
reveals here is a consultant-username relationship, not an Application's
existence — and a username is not secret in this system, the same reasoning the
`/api/profiles/{username}/…` routes use.

No Site is ever read on either path (invariant 6a).
*Pinned by:* `tests/test_cv_publish.py::test_unrelated_ta_gets_404_not_403`
(publish), `::test_extend_revoke_unknown_is_404` (unknown id → 404),
`::test_unrelated_ta_cannot_extend_or_revoke` (**asserts 403**, and that the
capability token still serves 200 — the public link is unaffected by who is
logged in), `::test_publish_requires_auth`.

### G12 — The photo is a copy named by the row id, never by the token

`_snapshot_photo` copies the consultant's current photo to a file named by the
publication's `id` and rewrites `cv_state.photoSrc` to the bare capability
photo route; that route authorizes by the header token, not by the filename.
*Pinned by:* `tests/test_cv_publish.py::test_photo_copied_on_publish_and_served_via_header`,
`::test_photo_reference_dropped_when_no_file`, `::test_photo_absent_is_404`.

### G13 — GDPR surfaces carry metadata only

`privacy/service.py` export writes `publications.json` from `to_dict()` — never
a token, never `cv_state`. Erasure deletes rows **and** photo files.
*Pinned by:* `tests/test_privacy.py::test_export_contains_all_categories_and_no_password_hash`
(seeds a real publication and asserts on `publications.json`),
`::test_erase_consultant_ordered_hard_delete`.

### Accepted limitations — do not "fix" these

- **No "Copy link" control** in `html/published-cvs.html`. A raw token cannot be
  reconstructed from its hash once shown. P-010's accepted trade-off —
  deferred, not built (see [product.md](../../product.md)).
- **View recording is anonymous and day-deduped**, keyed by the row's `id`.
  Automatic `link_opened` bookkeeping is the explicit exception to invariant 9.
- **Publishing requires a generated tailored CV** — publishing an Application
  with no content is refused with 400; there is nothing for a client to supply.
