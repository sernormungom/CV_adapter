# ADR-009: Publish a CV as a Capability-Link Web Page (the `cv_publish` module)

**Status:** Accepted V1 rationale; amended for V2 Pilot by P-010
**Date:** 2026-07-10
**Related:** ADR-001 (pipeline separation), ADR-011 (durable background work),
V2 Slice 5, P-010, `docs/architecture/areas/cv-publish.md`

---

## V2 Pilot amendment

The capability-link, Application ownership, fragment transport, hash-only storage,
sanitized viewer, non-destructive expiry, and terminal-deletion principles remain.
V2 Pilot does not require stable link derivation, signing-key versions, or automated
rotation: a raw link is shown once, and Regenerate mints a new token and invalidates
the prior link. Where this amendment conflicts with later detail below, the amendment
and P-010 govern.

---

## Context

When a consultant (or their TA) finishes editing a tailored CV in
`html/cv-importer.html`, they need to hand it to a hiring manager (HM). Today
there is no supported path: the dark `backend/cv_share/` module (Phase 3d, from
the era when *emailing* a rendered CV was the plan) was the only artefact, and no
UI ever called it. This ADR fixes the decision for the shareable-link feature the
product owner approved on 2026-07-10.

The requirements that force the design:

- **The HM has no account and never will.** A recruiter opening a CV from their
  inbox will not sign up. Access must be grantable by handing over a link.
- **The link is the firm's handshake.** It must render the CV pixel-identical to
  what the consultant approved, print/save-to-PDF cleanly, look right on a phone,
  and carry no "app" chrome — the HM is handed a document, not a tool.
- **The feature needs server logic, not static hosting.** Token validation, view
  tracking, expiry/revocation, and republish-in-place are dynamic behaviours.
- **A publication is an Application artefact.** In V2 it belongs to exactly one
  active Application and is never a standalone or base-template CV.
- **GDPR.** A CV is personal data being disclosed to an external party. Transport
  security, data minimisation, retention limits, and export/erasure coverage all
  apply.

---

## Decision

**A published CV is served as a hardened capability-linked web page from the
existing FastAPI service. Possession of an unguessable link grants read-only view
access; V2 has exactly one active publication per Application, and republishing or
Extend keeps the same link. The raw capability token is browser-fragment data and
is never an HTTP-path, query-string, database, log, or metadata value.**

Concretely:

- The share URL is `/cv/view#<raw-token>`. Browsers do not send the fragment when
  requesting the fixed `/cv/view` shell, so normal server, ALB, proxy, analytics,
  and referrer handling never receives the secret in the URL.
- The first-party viewer reads the fragment and submits it to a narrowly scoped
  anonymous token-exchange endpoint in the request body or a dedicated
  authorization header. The endpoint hashes the supplied token for lookup and
  returns the authorized CV snapshot plus, when present, one bounded photo encoded
  inline in that same response (for example, a validated base64 data URL). There is
  no second anonymous photo URL and therefore no raw-token path/query transport.
  The exchange returns the same friendly unavailable result for unknown, expired,
  and revoked publications.
- Each publication stores a random, non-business `public_id`, a signing-key version,
  and a unique hash of the high-entropy capability token. The raw token is
  deterministically derived from `public_id` with a versioned server-held HMAC key,
  so an authorized **Copy link** command can reproduce the same URL without storing
  the token. Creation/Copy may return it after object authorization; it is omitted
  from list, metadata, notification, audit, and error responses.
- A publication stores the editor's `captureFormState()` JSON (`cv_state`, a
  JSONB column) plus a copy of the consultant's photo — **not** rendered HTML —
  so the same CV can be re-edited and re-published, and so the viewer renders
  through the identical client-side engine as the editor (render-parity, Session
  2 of the plan).
- The snapshot uses a validated versioned schema. All displayed text/limited rich
  text is escaped or allowlist-sanitized before rendering. The anonymous response
  exposes no username, internal Job/Application IDs, storage paths, role data, or
  management metadata beyond the approved CV itself.
- Publication creation validates and normalizes the photo to an allowlisted image
  type, pixel dimensions, and byte limit before storing the publication-owned copy.
  The exchange repeats the response-size/type guard. Viewer CSP permits only
  first-party assets and the narrowly required inline image source (for example,
  `img-src 'self' data:`); scripts and all other resources remain first-party.
- **One active publication per Application** is enforced by the V2 relational
  model. `publish()`/republish replaces only the Application-owned tailored state;
  the Consultant's base template is never overwritten.
- **Lifetime/review deadline:** default 28 days (`CV_PUBLISH_TTL_DAYS`), reset on
  republish or Extend. At expiry, anonymous access stops and authorized users
  receive an action-required item, but CV/Application data is retained. Extend
  reactivates the same link. Revoke requires `Hired`, `Rejected`, or `Closed`, then
  ends the Application/link together and permanently deletes the CV content as an
  automatic part of that one confirmed action.
- **View honesty:** an open is recorded from a human-interaction *beacon* fired
  by the viewer page — not on every GET (bots and prefetchers don't count) and
  never for an authorized session (the consultant/TA viewing their own work). The
  status label the TA sees is **"Viewed by hiring manager"**, but the internal
  status name stays `link_opened`: an anonymous open of a link is *evidence*, not
  *proof*, that the HM read the CV. The raw token is not stored in the event or
  notification payload.
- **Viewer isolation:** the shell uses only first-party static resources, carries
  no app navigation or management affordances, and receives strict Content Security
  Policy, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, no-framing,
  no-content-sniffing, and noindex controls. Modest request throttling protects the
  anonymous exchange from abuse even though token enumeration is infeasible.
- **No stored PDF:** the service stores structured tailored state and its approved
  photo. Print/Download PDF is generated on demand in the browser and no PDF copy
  accumulates in application, EFS, or object storage.

### Why capability URLs, and not the alternatives

**Fragment capability URL, no HM auth (chosen).** The link *is* the credential.
Mitigations carry the security weight: at least 128 bits of random lookup
unpredictability plus a 256-bit HMAC, fragment-only transport until the viewer
exchange, hash-only secret storage, strict viewer headers and sanitization, a fixed
renewable expiry, throttling, and an authorized Revoke action. Unknown, expired, and
revoked tokens are indistinguishable to anonymous callers.

- **Rejected — passcode / email gate.** A second secret (or an email round-trip)
  in front of the CV adds friction the VIP recipient will resent, for marginal
  security over an already-unguessable 256-bit token delivered over TLS. The
  link's secrecy is the control; doubling it up is theatre.
- **Rejected for V2 — S3, another CloudFront distribution, or a dedicated public
  service.** These can
  isolate delivery, but add infrastructure/signing-key operations and complicate
  the stable 28-day link, Extend, immediate per-Application revocation, view
  tracking, and editable snapshot flow. A direct S3 presigned URL minted with
  temporary ECS role credentials can also expire with those credentials rather
  than at the requested seven days. The fragment-exchange design closes the known
  leakage and viewer risks without a second delivery stack. Reconsider isolation
  only if a security review or usage evidence justifies the operational cost.
- **Rejected — PDF-text round-trip.** Storing rendered output (HTML/PDF text) and
  reconstructing editable fields from it is lossy and fragile. The snapshot is
  the editor's `captureFormState()` JSON — the same structure `restoreFormState()`
  round-trips — so "Edit publication" is exact, never a re-parse.
- **Rejected — server-side PDF generation.** Rendering the CV to PDF in the cloud
  (headless Chrome, or a second engine like WeasyPrint) would either break the
  backend's no-browser invariant or diverge from the editor's output. The
  parity-safe path is client print-to-PDF from the *same* render engine the
  editor uses (the shared render core, Session 2). Revisit only if HMs demand a
  one-click server-generated file.

### Replace `cv_share` cleanly rather than patch it

`cv_share` proved out the right *patterns* (token mint, public view route,
daily-deduped TA notification, TTL expiry, Revoke, retention handling, GDPR
export/erasure hooks, `to_dict`-never-leaks-content) but its auth model (TA-only
mint), content model (rendered HTML blob), and lifecycle (new token per mint) are
all wrong for this feature. Patching would leave a module whose name, docstrings,
and tests describe a flow that no longer exists. So `cv_publish` is built fresh,
cannibalising the proven code, and `cv_share` is deleted in the same change
(module, model, table, tests). The pre-flight `SELECT count(*) FROM
cv_share_tokens` returned 0 on dev — the feature was never reachable from any UI,
so dropping the table loses no data. The **live** email machinery
(`backend/notifications/channels.py`, SES) is *not* residue — it serves auth
invites and TA notifications today and is untouched.

---

## Consequences

**Positive:**

- One low-friction link works for any anonymous recipient; no account, no gate.
- The backend stays the single source of view state, expiry, and revocation.
- The secret is absent from normal HTTP request URLs, access logs, referrers,
  database rows, and metadata/list responses.
- Authorized users can retrieve the same stable link again without a new link or a
  stored raw token.
- Snapshot-as-form-state keeps publish → edit → republish exact and lossless, and
  lets the viewer reuse the editor's render engine (zero rendering divergence).
- Data minimisation: the view record stores a count + timestamps only — **no
  viewer IP, no user-agent** (a deliberate reduction from what `cv_share`
  stored). V2 retains content through the expired/action-required state and
  hard-deletes it only after a confirmed Hired, Rejected, or Closed terminal
  action; lightweight Application history then survives.
- Human-in-the-loop (invariant 9) is preserved: publishing is always an explicit
  human click with a confirm step. The `published → link_opened` flip is *inward*
  bookkeeping (a status update), not an outward action, so it is allowed to be
  automatic.
- Scores never enter this feature (invariant 7): a publication stores CV content
  and view state, never a match score.
- The design adds no S3, additional CloudFront distribution, dedicated
  worker/service, or stored-PDF cleanup burden for the pilot; it reuses the
  application's existing HTTPS entry point.

**Negative / tradeoffs:**

- A leaked link grants access until it expires or is revoked — the accepted cost
  of a no-auth capability URL. The fragment still exists in recipient browser
  history and can be forwarded or copied; no technical design can prevent a
  recipient from saving the displayed CV.
- Storing `cv_state` (and a photo copy) per publication duplicates content that
  also lives in the consultant's working profile — a small, bounded storage cost,
  and the point: a publication is an immutable-until-republished snapshot, not a
  live view of the working CV.
- The render-parity requirement forces the editor's inline render pipeline to be
  extracted into shared static assets (Session 2) — the riskiest refactor in the
  plan.
- The publication signing key becomes durable application configuration. Normal key
  rotation retains prior versions while their Applications remain live; an emergency
  rotation after suspected compromise may invalidate affected links and create
  action-required work. That security exception is allowed to override link
  stability.

**Neutral:**

- V2 changes the public shape from `/cv/view/{token}` to `/cv/view#<token>` and the
  database lookup from raw token to hash. D-012 deletes all legacy/test
  publications before V2, so no live external-link migration is required.

---

## Launch gates and hostname

HTTPS is a hard launch gate for sending CVs to external parties. Repository
deployment records describe an existing CloudFront HTTPS entry point; this must be
verified against the authoritative deployed infrastructure before launch. V2 keeps
the transport requirement and does not add another distribution or delivery
service.

The remaining V2 launch gate is the hardened implementation in this ADR: the
fragment/exchange boundary, hash-only token storage, minimized response, strict
viewer controls, throttling, and their negative tests must be deployed before
external production sharing resumes. The authenticated publication-management
surface must also satisfy D-014's non-disableable production auth, password,
session, and login-throttling requirements. A dedicated `cv.<firm-domain>` hostname
may improve recipient presentation and route separation, but it is neither a
security boundary nor a V2 prerequisite.

---

## Alternatives Considered

Summarised inline above under "Why capability URLs, and not the alternatives"
(passcode/email gate, S3/CloudFront/dedicated delivery, PDF-text round-trip,
server-side PDF generation) and "Replace `cv_share` cleanly rather than patch it".
