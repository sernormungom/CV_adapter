# Data Protection Impact Assessment (Art 35 GDPR)

**Not legal advice** — this is an engineering-oriented compliance artefact, produced for qualified Swedish counsel to review before being relied on as a complete DPIA. In particular, the "acceptable" conclusion in §5 is an engineering judgment about residual technical risk, not a legal sign-off.

**Status:** Session 5 governance artefact of `docs/legal/decisions.md`, describing the system **as remediated by Sessions 1–4** (merged to `main`). Structured against IMY's expected DPIA shape (Art 35(7)): (a) systematic description, (b) necessity/proportionality, (c) risk identification, (d) mitigations, plus a conclusion on residual risk.

> **Planned V2 amendment—not yet a deployed mitigation:** D-013 and amended ADR-009
> retain the 28-day capability-link experience but move the raw secret to a browser
> fragment/body-or-header exchange, store only its hash, minimize and sanitize the
> anonymous response, forbid third-party viewer assets, apply strict
> CSP/no-store/no-referrer/no-frame/nosniff/noindex controls, retain no server-side
> PDF, and stop expiry from deleting content without human approval. D-014 also
> makes hardened local authentication mandatory and defers SSO/Cognito/MFA. This
> DPIA must be revalidated against the implemented controls before V2 external use.

---

## 1. Is a DPIA required here?

Yes, on the facts already established in `docs/legal/decisions.md` §2.1: IMY's Art 35(4) list includes "systematic evaluation of employees using innovative technology," and the matching/scoring pipeline is exactly that — an AI-assisted (LLM-generated matching config, deterministic scorer) system that evaluates job/consultant fit. (V1/V2 planning additionally described a learning loop feeding employee verdict patterns back into future scoring; that mechanism is deleted — V3-P-008 decision 5 — and does not need to hold for this threshold conclusion, since the LLM-generated config and deterministic scorer alone already meet it.) This DPIA treats that conclusion as settled and proceeds to the substantive assessment rather than re-litigating the threshold question. See `docs/legal/ai-act-assessment.md` (sibling artefact) for the separate AI Act high-risk classification question, which is related but not identical.

## 2. Systematic description of the processing

Two independent pipelines (`CLAUDE.md` "Architecture in two sentences") process consultant personal data:

1. **CV Prep Pipeline** — imports a consultant's CV, extracts structured facts via an LLM call, stores them as `profile.md`, and generates tailored CVs for specific client assignments (also LLM-driven).
2. **Opportunity Matching Pipeline** — collects job postings (manual `+ Job` entry in the cloud UI; the TA Cockpit's local browser scraping was retired by ADR-010 in V2 Step 1), scores each against a consultant's profile and `matching_config.yaml` (a deterministic scoring function, `shared/scoring.py`) computed on demand, and lets a Consultant or TA see ordered opportunities and record Apply/Reject. The matching config itself is LLM-generated from `profile.md` alone by `config_bootstrapper.py`, re-run only on an explicit human "Update match configurator" action — the verdict-fed learning loop this description used to also cite (a Learning Module proposing config refinements from accumulated verdicts) never had a live writer and was deleted, V3-P-008 decision 5.

A later addition (`docs/architecture/adr/ADR-009-cv-publish-capability-links.md`) extends the CV Prep Pipeline with **CV publishing**: a consultant/TA can deliberately share a tailored CV with an external hiring manager via an unguessable, expiring, revocable capability link. This is the system's only path that discloses consultant personal data to a party *outside* the Company, and is assessed as Risk 9 below (and recorded as its own processing activity in `docs/legal/ropa.md` §6).

Processing is on-demand and per-consultant: nothing is a batch/population-level automated decision. See `docs/legal/ropa.md` for the full Art 30 breakdown of purposes, legal bases, and data flows this DPIA assumes.

## 3. Necessity and proportionality

- **CV/profile processing** is necessary to perform the core service the Company provides to a consultant (staffing on assignments) — Art 6(1)(b). Extraction is schema-first (`project_context/consultant-profile.schema.md`): the LLM is instructed to produce structured facts, not an open-ended dump, which bounds what ends up in long-term storage. **V2 Slice 4** restored CV import as a durable async workflow and, in doing so, tightened this further: the uploaded document is now deleted by the import handler immediately after its last required processing stage, on every path including failure — it is never retained for a 30-day verification window the way V1's synchronous importer's archived copy was. `UPLOAD_RETENTION_DAYS`/`purge_old_uploads` remain as a purge for the pre-V2 `uploads/` archive entries that already exist on disk (a historical backstop, `project_context/consultant-profile.schema.md` §0.2), but no new import creates one.
- **Matching/scoring** is proportionate because it is deterministic and inspectable (a human-authored `matching_config.yaml`, not an opaque model) — see `docs/legal/lia.md` for the full necessity argument specific to Art 6(1)(f) processing.
- **The verdict-fed learning loop this bullet used to describe no longer exists.** `delta_guard.py` and the config-update proposal it bounded were deleted (V3-P-008 decision 5, V3 packet S2) rather than built out — they never had a live writer. The proportionality control left is schema validation (`shared/matching_config.py::validate_config`) on a human-triggered, `profile.md`-only config generation.
- **Less intrusive alternatives** considered and rejected as impractical at this scale: fully manual TA-only matching (defeats the tool's purpose and doesn't scale, while human curation is *already* retained as the gating step — see §5's human-in-the-loop mitigation); a static config with no owner-visible input (would drift from what actually works for a consultant and require an admin to hand-edit YAML; profile-driven regeneration achieves the same currency without reading anything beyond what the consultant already wrote into `profile.md`).

## 4. Risks identified and their mitigations

Each risk below existed in some form in the pre-fix system described in `docs/legal/decisions.md` §1.1/§2.4; the "Mitigation" column states what Sessions 1–4 actually shipped, with the exact file/endpoint.

### Risk 1 — LLM disclosure of CV/profile data to a third-party processor

CV text, full profile YAML, and job descriptions are sent to Anthropic across several call sites (`backend/llm.py` callers: `experience_extractor.py`, `cv_content_generator.py`, `chat_engine.py`, `profile_patcher.py`, `config_bootstrapper.py`). This is an inherent disclosure to a processor outside the Company's direct infrastructure. (Verdict history was never sent here in the deployed system — `config_updater.py` and `config_update_prompt.py`, the two call sites that would have sent it, had no live caller and were deleted along with the rest of the verdict-fed learning path, V3 packet S2.)

**Mitigation:** Anthropic is DPF-certified with SCCs Module 2 in place, does not train on API inputs/outputs, and retains API logs only 7 days (`docs/legal/processors.md`). This does not eliminate the disclosure but bounds its consequence (no model training, short-lived logs, contractually governed processor). Data-minimisation controls reduce *what* is disclosed and logged around the disclosure: the extractor works against a facts-only schema (bounds what profile data even exists to send); `DEBUG_LLM_LOGGING` (default off, `backend/config.py`) gates the ~400-char raw-response previews that used to always log server-side (Session 3); `backend.llm.opaque_label()` replaces raw usernames with a non-reversible hash in LLM-call log labels (Session 3). **Residual risk:** the disclosure to Anthropic is structural to the product (LLM-based extraction/generation/config-tuning is the product) and cannot be eliminated without an architecture change (e.g., an EU-hosted inference alternative) — `docs/legal/decisions.md` §3 decision #4 explicitly defers this, treating the current DPF+SCC+no-training posture as sufficient and revisiting only if counsel demands EU-only inference.

### Risk 2 — TA-laptop loss exposing `configs_cache.json` — RETIRED (V2 Step 1, ADR-010)

**Status: closed by design, not by mitigation.** The TA Cockpit — and with it the local `configs_cache.json` cleartext cache and the local SQLite triage pool — was removed in V2 Step 1 per ADR-010 (browser scraping and local triage retired; jobs are created/managed in the cloud UI, with a lean email forwarder as a possible future conditional slice). No consultant profile or matching-config data is cached on a TA laptop in V2. This eliminates the laptop-loss exposure surface described below rather than mitigating it; the original risk description is retained for historical record.

*Original risk (V1, resolved):* The TA Cockpit cached every actively-looking consultant's full profile and matching config in cleartext at `data/ta_cockpit/configs_cache.json` (`local_tools/app/config_sync.py`) so it could score jobs offline. A lost or stolen laptop would have exposed this file (and the local SQLite triage pool, `data/ta_cockpit/pool.db`, which held job records and triage state but never scores — ADR-008 §1). The V1 mitigation required full-disk encryption (BitLocker) as a laptop-provisioning prerequisite and restricted ACLs on the cache directory; the residual gap was that the cache had no absolute TTL. None of this applies now that the Cockpit and its cache no longer exist.

### Risk 3 — Log leakage (INFO logs, CloudWatch)

The pre-fix system logged ~400-char previews of extracted profiles / generated CV content at multiple call sites, and used raw usernames (`chat:{username}`) as LLM-call log labels, all flowing to CloudWatch on ECS.

**Mitigation:** `DEBUG_LLM_LOGGING` (default `false`, `backend/config.py`) now gates every one of those content-preview log lines, including the CV-import flow's (`cv_content_generator.py`, `config_bootstrapper.py`, and `experience_extractor.py`/`profile_merger.py`) — off by default in any environment that doesn't explicitly opt in for local debugging. The CV-import flow's own separate `DEBUG_CV_IMPORT` flag was removed as dead code in STAB-7; `DEBUG_LLM_LOGGING` is now the sole guard for `experience_extractor.py`/`profile_merger.py`'s preview logging, same as every other call site. `backend.llm.opaque_label()` (Session 3) replaces the raw username in LLM-call labels with a deterministic, non-reversible truncated SHA-256 hash, so a log file remains grep-able per user without exposing the username itself. **Residual risk:** this only covers the specific previously-identified log call sites; a future call site that logs response content without going through the same flag would reintroduce the issue — this is a code-review discipline item, not something a config flag can guarantee forever.

### Risk 4 — Third-party contact PII in job ads / lead emails

Job postings routinely carry a named hiring-manager/recruiter's contact details in the free-text description, and the original email cleaner did not strip the leading mail-header block (`From:/To:/Subject:` etc.) where such details typically appear.

**Mitigation (historical):** `shared/collection.py::_clean_email_noise` (Session 3) used to strip a leading `From:/To:/Cc:/Sent:/Date:/Subject:` header block, in addition to quoted-reply/signature/disclaimer stripping. **⚠️ Reduced in V2 Slice 1, then the code itself removed in STAB-7 — this risk is now partly unmitigated and is an open item.** Three things changed:

- **The short-retention limb is gone.** The job store's `expired` state and 7-day `PURGE_GRACE_DAYS` hard delete were deleted with the JSON Job Store. A relational Job is closed by a person and then kept, with no automatic purge (an open product/legal item, see [`docs/product.md`](../product.md)). Art 14(5)(b) in `docs/legal/ropa.md` §4 leaned on that window and no longer can.
- **The cleaner never ran on the manual path.** It fit an email boundary; V2 Slice 1 deliberately did not apply it to a hand-typed description, because its quote/signature truncation would silently delete the tail of legitimate text.
- **The code itself no longer exists.** `shared/collection.py` had zero remaining callers once Slice 6 (the email-forwarder path it would have served) was discarded (P-018, 2026-09-04), so STAB-7 removed it as dead code. If email ingestion is ever reconsidered, equivalent stripping would need to be rebuilt, not merely reconnected.

Partly offsetting: postings are now entered by hand, one at a time, rather than scraped in bulk — far fewer records, each seen by a person who can remove a contact line before saving. **Residual risk: moderate and open.** The named individual's details may now persist indefinitely in a Job description. **Actions:** decide a Job retention rule (or a description-level minimisation pass) and re-argue or re-base Art 14(5)(b) before production. The data is still never scored, matched, or used to build a profile on that individual.

### Risk 5 — Unbounded retention (pre-Session-3)

Original CV uploads, verdict rows, and the old (dark, never-shipped) `cv_share` rendered-HTML blobs were kept indefinitely.

**Mitigation:** Session 3 added code-enforced purges: uploads after `UPLOAD_RETENTION_DAYS` (30, default) and — until the `verdicts` table was dropped with no writer left (V3 packet S2, V3-P-008 decision 5) — verdicts after `VERDICT_RETENTION_MONTHS` (24, default). The `cv_share` module was later replaced by `cv_publish` (ADR-009): a published CV's snapshot **and** its photo copy are hard-deleted once past `CV_PUBLISH_TTL_DAYS` (28, renewable) or on revoke (`backend/cv_publish/service.py::purge_expired`). The remaining purges run on demand via `POST /api/admin/retention/run` (`backend/retention/routes.py`, admin-only) **and now also run automatically**: cv-publish Session 4 added a daily lifespan background task in `backend/main.py` (`_retention_purge_loop`) that fires `run_retention_purges` at startup and every 24h. **Residual risk:** low — the previously-flagged "purges only run when triggered" gap is now closed by the daily task; the remaining dependency is that the app process actually stays up long enough between restarts for the 24h cycle to fire, which for a long-running ECS service it does.

### Risk 6 — No data-subject rights mechanism

Pre-fix, there was no export endpoint and no true delete (only a TA↔consultant association removal).

**Mitigation:** Session 2's `backend/privacy/` module: `GET /api/privacy/consultants/{username}/export` (ZIP: user row minus `password_hash`, full profile tree, CV-publication metadata, TA associations, the consultant's Job associations and their own review markers, their Applications with the stage each reached (V2 Slice 2), and each Application's tailored CV **including its content and the frozen inputs it was generated from** (`tailored_cvs.json`, V2 Slice 3 — disclosed in full rather than as metadata precisely because erasure deletes it in full) — excluding, in every case, another consultant's link, review, Application, or tailored CV on the same shared Job) and `DELETE /api/privacy/consultants/{username}` (ordered hard delete: publications, photo files first → sessions/invites/API tokens → TA associations → notifications → tailored CVs → Applications → Job links and review markers → profile directory → `users` row; the shared Job itself is business content and survives, with its authorship reference nulled), admin-only, restricted specifically to accounts with `role == consultant` (a merge-review fix closed a gap where the lookup could otherwise target a TA/admin account, including the caller's own). (The export/erasure ordering used to lead with verdicts; that step is gone with the `verdicts` table, V3 packet S2.) An `AuditLog` row records that an erasure happened, by whom, and when (hashed username, never the erased content) — `backend/db/models/audit.py`. **Residual risk:** rights are admin-executed, not consultant self-serve (this is a deliberate locked decision, not a gap — see `docs/legal/ropa.md` §6 and `docs/legal/decisions.md` §3 decision #2), so response time depends on an admin acting on a request; `html/privacy.html` §7 currently tells the consultant to "contact your HR representative or system administrator," which is accurate to how the endpoints work today.

### Risk 7 — Transport/session security gaps

Pre-fix: `CORS allow_origins=["*"]`, no HSTS, non-expiring API bearer tokens.

**Mitigation:** Session 4: `backend.config.CORS_ALLOWED_ORIGINS` (an explicit allowlist, defaulting to just the app's own base URL) replaces the wildcard in `backend/main.py`; an HSTS middleware (`_add_hsts_header`) adds `Strict-Transport-Security` whenever `SESSION_COOKIE_SECURE` is true. The bearer API-token mechanism this paragraph once described (a 365-day `API_TOKEN_TTL_DAYS` expiry with a `POST /api/auth/tokens/{id}/rotate` endpoint) was retired along with V2 Slice 6 (STAB-6, P-018); there is no bearer-token path left to secure. **Repository records describe TLS through the existing CloudFront entry point**, including secure-cookie deployment settings; this repository does not contain the authoritative infrastructure and therefore cannot prove the live CloudFront→ALB configuration or the status of an earlier custom-domain plan. **Residual risk:** verify HTTPS redirect, certificate, origin encryption/timeout, direct-ALB reachability, and `SESSION_COOKIE_SECURE=true` against the deployed infrastructure before launch. Local plain HTTP remains development-only. A dedicated company hostname may improve presentation but is not itself a security boundary or a V2 prerequisite.

### Risk 8 — Employee-evaluation / profiling risk (Art 22 exposure)

The matching/scoring loop evaluates a consultant's fit for assignments against their own profile and matching config — the kind of process that, if fully automated end-to-end, would raise Art 22 (solely-automated decisions with legal/significant effect).

**V1 mitigation:** the deployed design used TA batch curation, a Consultant verdict, and human pending-config approval. That is historical evidence, not the current control description.

**Superseded "Planned V2 treatment" (V3 correction, 2026-09-10):** this paragraph used to describe V2 storing advisory Consultant–Job projections and *automatically* applying an LLM-proposed matching config after schema/delta-guard validation. That automatic-proposal design was never built — it had no live writer — and V3-P-008 decision 5 now orders it deleted rather than built: there is no accept/reject verdict history left to adapt from, no delta guard, and no auto-apply path. **Current V3 control:** a config is generated only from `profile.md`, only when a human presses "Update match configurator" (decision 4), and a failed or invalid generation leaves the previous valid config in place (schema validation, no delta guard needed with nothing left to bound). Every otherwise eligible Job remains available regardless of score, and a human still chooses Apply/Reject and every outward CV/Application action (invariant 9). **Residual risk:** lower than the withdrawn automatic design — there is no behavioural-adaptation mechanism to raise an automation-bias concern about — but the profile-driven config is still an LLM-authored artefact shaping ranking; see the V3 note in `ai-act-assessment.md` §2.4.

### Risk 9 — External CV exposure via a capability link (cv-publish, ADR-009)

The `cv-publish` feature is the first path by which a consultant's personal data (a full tailored CV + photo) is deliberately disclosed to a party **outside** the Company — an external hiring manager — via an unguessable link that grants read access with no recipient authentication. The obvious risks: link leakage/forwarding beyond the intended recipient, indefinite exposure, and inflated/identifying view tracking of the recipient.

**Mitigation (ADR-009):** the disclosure is *intended and consultant-initiated* — publishing is a deliberate human click with a confirm step (CLAUDE.md invariant #9), tied to a specific `(consultant, job)` placement, on the Art 6(1)(b) basis that putting the consultant forward is core to the engagement. The capability-URL model is bounded by compensating controls: a 128-bit+ unguessable token (brute-force enumeration infeasible — rate limiting is therefore explicitly out of scope, ADR-009), `X-Robots-Tag: noindex` on every public response (kept out of search indexes), a fixed renewable lifetime (`CV_PUBLISH_TTL_DAYS`, 28) after which content is hard-deleted, and a one-click revoke. Data minimisation runs both ways: **no viewer IP/UA is stored** (only a count + timestamps), the "opened" signal is a human-interaction beacon (not a load-time hit, so prefetchers don't trip it), and anonymous viewers receive **no cookies**. The published artefact is a point-in-time snapshot, so revoking/expiring genuinely removes access and a later profile edit can't retroactively change what was shared. **Residual risk:** a recipient can still forward the link within its lifetime — inherent to any no-auth share link, accepted as the deliberate product trade-off (the alternative, per-recipient auth/email gates, was rejected in ADR-009 as friction the hiring-manager flow can't bear). Revoke + short TTL keep the exposure window bounded; the consultant/TA controls both.

**Planned V2 treatment:** The current mitigation paragraph remains evidence about
V1. Before V2 external use, D-013/ADR-009 require fragment-only link transport,
hash-only token storage, a throttled anonymous exchange, sanitized/minimized output,
strict browser controls, and no secret in paths, queries, logs, metadata, events, or
referrers. Expiry blocks access but retains the CV for Extend or a human terminal
decision; only confirmed Hired/Rejected/Closed automatically deletes the tailored
snapshot/photo. Forwarding/browser-history exposure remains the accepted residual
risk of a no-recipient-auth capability link.

**V2 implementation status of those five controls (2026-09-04, corrected after
`STAB-5-R1`):** four are implemented and the fifth — "strict browser controls" —
is implemented **except for one named item**, recorded below rather than
claimed complete. Fragment-only transport, hash-only token storage, and
sanitized/minimized output landed with V2 Slice 5 and `S5-P2`
(`backend/cv_publish/`, `backend/templates/cv-viewer.html`). The throttled
anonymous exchange landed with `STAB-5`
(`backend/cv_publish/throttle.py`): one **global** in-process cap of 60 requests
per minute shared by all four unauthenticated capability routes, applied before
any token lookup so an exhausted budget answers identically for a live token, a
revoked one, an unknown one, and no token at all. Consistent with ADR-009's own
wording, this is "modest request throttling … even though token enumeration is
infeasible" — it bounds automated abuse and resource use on a surface open to
anyone with no identity check, and is **not** claimed as protection against
guessing a 256-bit token. Two limitations are recorded rather than implied: the
counter is per Python process (theoretical today — one uvicorn worker, ECS
desired count 1 — but the effective cap multiplies if either rises), and the cap
is global rather than per source, because no trusted client-address header
contract exists yet at this layer (the same gap `STAB-2` recorded for per-IP
login throttling; it should be closed once, for both). See
`docs/architecture/areas/cv-publish.md`.

**Strict browser controls — what is in place, and the one item that is not.**
Implemented on every publicly reachable response (the four capability routes
and the viewer shell, one header set in `backend/cv_publish/routes.py`):
`Content-Security-Policy` with `default-src 'none'` and `script-src 'self'`
(no inline-script exception), `frame-ancestors 'none'`, `X-Frame-Options:
DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Robots-Tag: noindex`, and — added by `STAB-5`'s correction round, having
been absent since Slice 5 — **`Cache-Control: no-store`**, so neither a
browser nor an intermediary cache (CloudFront fronts this origin) is permitted
to retain `/content`'s tailored-CV body or re-serve it after Revoke or expiry.
Pinned across all six public surfaces by
`tests/test_cv_publish.py::test_every_publicly_reachable_response_forbids_caching`.

**Not yet met: "forbid third-party viewer assets."** The viewer shell still
loads its webfonts from `fonts.googleapis.com`/`fonts.gstatic.com`, which its
CSP correspondingly allows (`backend/templates/cv-viewer.html`). This is an
**open item for the pre-external-use gate, deliberately not closed inside
`STAB-5`**: the five font families are the ones the CV render core and the
editor's own presentation allowlist use, so dropping them silently re-renders
every already-approved published CV in a fallback face, and self-hosting them
properly (committing WOFF2 binaries, serving them first-party, re-verifying
the render in a real browser) is its own bounded piece of work rather than a
rate-limiter correction. The exposure is narrow and does **not** disclose
Consultant data: with `Referrer-Policy: no-referrer` already in force the font
request carries no referrer, no capability token, no consultant identity, and
no CV content — Google learns only that some IP address fetched a public
webfont. Accepted as a remote residual risk for this fabricated-data pilot;
**close it before real Consultant data or genuine external sharing**, at which
point ADR-009's first-party-only requirement is met literally rather than
substantially.

Because expiry deliberately retains the CV/Application until a person acts, V2 also
introduces a storage-limitation risk: paused Jobs could leave personal CV snapshots
indefinitely. Persistent, non-dismissible TA action at the top of the Application
dashboard and a prominent oldest-first Admin queue reduce neglect but do not create
an automatic retention limit. Age must be visible, unresolved items included in
periodic operational review, and qualified privacy review must accept or revise this
human-only deletion posture before launch.

## 5. Conclusion on residual risk

Taken together, Sessions 1–4 materially reduced the pre-fix risks identified in
`docs/legal/decisions.md` §1.1, and the human-in-the-loop design (Risk 8)
structurally keeps the system out of GDPR Art 22 territory. The remaining residual
items and V2 launch conditions are:

1. Risk 2 (`configs_cache.json` TTL) is closed: V2 Step 1 removed the Cockpit
   code and its local caches/pool entirely (verified at V2 Step 1; see
   `docs/architecture/areas/shared-and-legacy.md` G5), not just the sync path.
2. The current daily in-process purge exists, but its restart/delivery semantics are
   weak. ADR-011 replaces fragile background work with the durable Postgres-backed
   pattern without adding another AWS service.
3. Repository records describe CloudFront TLS, but the authoritative infrastructure
   must verify the live certificate/redirect/origin/direct-ALB posture (Risk 7).
4. Anthropic disclosure is structural to the product and cannot be fully eliminated
   without an architecture change (Risk 1) — accepted per locked decision #4.
5. Capability links can be forwarded or remain in browser history within their
   lifetime (Risk 9) — an accepted product trade-off bounded by expiry, immediate
   revocation, and the D-013 fragment/hash/viewer controls.
6. D-013 viewer hardening and D-014 local-auth hardening are planned, not deployed.
   External V2 production sharing is blocked until they are implemented, negatively
   tested, and this DPIA/ROPA/privacy notice are revalidated against the result.
7. V2 automatic config application and stored ranking projections require a revised
   AI Act classification, automation-bias/ordering tests, and qualified review; the
   July V1 classification cannot approve V2 (Risk 8).
8. Expired CV/Application data has no automatic deletion deadline. Persistent
   oldest-first action queues and periodic operational review are required, and the
   residual storage-limitation risk needs privacy approval (Risk 9).

**Engineering assessment:** the currently deployed pilot has bounded residual risk,
but that assessment does not approve external V2 launch. Launch remains conditional
on item 6, the operational reviews above, and qualified counsel confirming the
legal conclusion. Revisit the assessment if the Anthropic processing relationship
changes, the human-in-the-loop invariant weakens, or counsel reads the balancing
test in `docs/legal/lia.md` differently.
