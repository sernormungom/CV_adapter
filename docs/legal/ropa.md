# Record of Processing Activities (Art 30 GDPR)

**Not legal advice** — this is an engineering-oriented compliance artefact, produced for qualified Swedish counsel to review before being relied on as a complete Art 30 record.

**Status:** Session 5 governance artefact of `docs/legal/decisions.md`. Describes the system **as remediated by Sessions 1–4** of that plan (all merged to `main`), not the pre-fix inventory in that plan's §1.1 (dated 2026-07-07). Where this document cites a Session's fix, the citation is the primary source of truth — re-check the cited file if the code has moved on since this was written. **§6 (CV publishing to hiring managers)** was added later, when the `cv-publish` feature (ADR-009) introduced a genuinely new processing activity — a disclosure of a consultant's CV to a recipient outside the Company (2026-07-10).

> **V2 planning note:** This remains a current/deployed-state record until V2
> cutover. D-013/ADR-009 plan fragment/exchange capability links, hash-only token
> storage, sanitized/minimized anonymous responses, strict viewer controls, no
> stored PDF, and human-reviewed expiry. D-014 plans hardened local auth. Update the
> operative rows and revalidate this ROPA when those controls are actually deployed.

**Controller:** the Company (the employer; name withheld in this portfolio copy). **Joint controllers:** none identified. **DPO:** not currently appointed — `docs/legal/decisions.md` §5 flags the Art 37 necessity decision as outstanding (HR/legal decision, not a code question).

---

## 1. CV import, profile management & CV generation

| Field | Detail |
| --- | --- |
| **Purpose** | Build and maintain a consultant's structured career profile from an uploaded CV, and generate tailored CVs for specific assignments/clients. |
| **Legal basis** | Art 6(1)(b) — necessary to perform the consultant engagement (staffing consultants on assignments is core to the relationship). **Never consent** (locked decision #1 — employer/employee power imbalance invalidates it per IMY/EDPB guidance, `docs/legal/decisions.md` §1.2). |
| **Data subjects** | Consultants (employees/contractors placed by the Company). |
| **Data categories** | Account data (email, username, full name, role, city, bcrypt password hash — `backend/db/models/user.py`); structured profile (identity, education, career history) in `data/profiles/<username>/profile.md` + `style.md`; uploaded CV files in `data/profiles/<username>/uploads/`; profile photo; the **base CV** editor state (`data/profiles/<username>/cv_state.json`, which since V2 Slice 3 also carries a `_cv_identity` revision/hash block); and each **Application-tailored CV** in Postgres `tailored_cvs` — its `cv_state` (a CV document of this person) plus `input_snapshot`, a frozen copy of the profile YAML, style rules, base CV state and job posting the generation was approved to use (`backend/db/models/tailored_cv.py`). The tailored artifact is a separate storage identity from the base CV by design (P-009); the snapshot duplicates profile data deliberately, so a generated document can be explained by the inputs it actually used. |
| **Recipients** | Anthropic (Claude API) for CV-text extraction, merging, and generation (`backend/cv_importer/experience_extractor.py`, `backend/cv_importer/profile_merger.py`, `backend/cv_pipeline/cv_content_generator.py`, `backend/cv_assistant/chat_engine.py`, `backend/cv_assistant/profile_patcher.py` — all via `backend/llm.py`). AWS (hosting). |
| **International transfers** | Anthropic PBC, USA. EU-US Data Privacy Framework certification (active) + Standard Contractual Clauses Module 2 (controller→processor); DPA auto-incorporated into Anthropic's Commercial Terms (version effective 2026-01-01); no training on API inputs/outputs; 7-day API log retention. See `docs/legal/processors.md`. |
| **Retention** | **V2 Slice 4's durable import** (`backend/cv_importer/`) deletes the uploaded file after the handler's last required stage, on every path including failure — ordinarily within seconds, tighter than the figure below. A rare process crash or storage-level delete failure can leave that file on disk with no reference clearing it until this consultant's next import overwrites the reference, or they are offboarded, whichever comes first (`backend/privacy/service.py::erase_consultant` removes it unconditionally). Accepted for this disposable-data pilot: the residual file is small, uploader-only, and holds nothing `profile.md` will not shortly hold anyway. The `UPLOAD_RETENTION_DAYS` (default 30 days) purge (`backend/config.py`, `backend/cv_importer/experience_writer.py::purge_old_uploads`, Session 3) remains for the pre-V2 synchronous importer's archived `uploads/` copies already on disk; a V2 import creates no new entry there. Only `profile.md` facts (+ any pre-existing `metadata.source_archives` filename list) survive past either path. Profile facts themselves persist for the duration of the engagement (no independent TTL — they are live operational data, deleted only on erasure, see §7). |
| **Security measures** | bcrypt password hashing; server-side sessions (hash-only token storage, httponly+samesite cookie, `SESSION_COOKIE_SECURE` gate); CORS allowlist (`backend.config.CORS_ALLOWED_ORIGINS`, replacing the prior `allow_origins=["*"]`, `backend/main.py`); HSTS response header when served over HTTPS (`backend/main.py::_add_hsts_header`, Session 4); LLM-call content previews gated behind `DEBUG_LLM_LOGGING` (default off, Session 3) instead of always logging ~400 chars of extracted profile/CV text; LLM-call log labels use `backend.llm.opaque_label()` (a truncated, non-reversible SHA-256 hash) instead of the raw username. |

## 2. Opportunity matching & scoring

| Field | Detail |
| --- | --- |
| **Purpose** | Score Jobs against a consultant's profile and matching config, and let a Consultant or TA see ordered opportunities. |
| **Legal basis** | Art 6(1)(f) — legitimate interest, since this is not the core contractual act of employment but an optimisation on top of it. A full three-part Legitimate Interest Assessment is at `docs/legal/lia.md`. **Never consent.** |
| **Data subjects** | Consultants. |
| **Data categories** | Matching config (`data/profiles/<username>/matching_config.yaml`, generated from `profile.md` alone by `config_bootstrapper.py`); profile facts (read, not duplicated); job records (intrinsic facts only, no scores — the Postgres `jobs` table since V2 Slice 1, which retired the `data/job_store/jobs/` file store and the curated review batch). Which consultants may see a job is the `job_consultants` link. **Scores themselves are never persisted** — they are a pure function of `(consultant profile, matching config, job)` computed on demand by `backend/matching/service.py` / `shared/scoring.py` (ADR-007; CLAUDE.md invariant #7), and a recorded Reject deliberately stores no score alongside it. |
| **Recipients** | Anthropic, for `config_bootstrapper.py`'s config-generation call. AWS (hosting). |
| **International transfers** | Same as §1 — Anthropic, DPF + SCCs Module 2. |
| **Retention** | Matching config is live operational state, retained for the duration of the engagement; no independent purge (it holds no free-standing PII beyond what's already covered under profile retention). Job records: see §3's third-party-data retention (the same job store holds all jobs regardless of whether contact PII is present). |
| **Security measures** | Same LLM-logging minimisation as §1 (`DEBUG_LLM_LOGGING`, `opaque_label()`). Schema validation (`shared/matching_config.py::validate_config`) rejects a malformed generation and leaves the previous valid config in place — the only safeguard left once the verdict-fed learning path (and the delta guard it used to also enforce) was deleted, V3-P-008 decision 5. **Human-in-the-loop** (CLAUDE.md invariant #9): config generation only ever runs from an explicit human "Update match configurator" action, never automatically. |

**V3 note (2026-09-10):** this activity used to also describe a Learning
Module that proposed configuration refinements from a consultant's accept/reject
verdict history, and a "Planned V2 change" paragraph here described a
`job_consultant_matches` cache whose config side auto-applied through a delta
guard. Both are superseded: V3-P-008 decision 5
deletes the verdict-fed proposal mechanism and its delta guard outright (V3
packet S2, `docs/architecture/adr/ADR-004-learning-module.md`'s V3
supersession); decision 4 replaces the automatic first-config trigger with the
human-pressed button above. The stored-score cache itself is a separate,
still-open V3 Slice 1 item — see `docs/architecture/adr/ADR-012-versioned-stored-advisory-matches.md`'s
V3 amendment — and carries no learning/config-proposal behaviour of its own.

## 3. Verdict / evaluation record-keeping — RETIRED (V3 packet S2)

**This processing activity no longer exists.** It recorded a consultant's
accept/reject/maybe decision on a proposed opportunity, with an optional
free-text reason, as input to the Learning Module (§2) and as an
application-tracking record. The Postgres `verdicts` table it depended on
(`backend/db/models/verdict.py`) had no writer since V2 Slice 3 — Slice 2's
`applications` already replaced its application-tracking function (P-008) — and V3-P-008 decision 5
orders the table itself dropped (`alembic/versions/0003_v3_drop_verdicts.py`)
along with its last two readers, the GDPR export and the retention purge (both
below). Retained here, struck through in substance, as a historical record of
what this ROPA previously listed — the same convention §5 uses for the
retired TA Cockpit.

*Original entry (V1/V2, resolved):* Legal basis was Art 6(1)(b) for the
application-tracking function and Art 6(1)(f) for the learning-loop use of the
same data (this is employee-evaluation data in substance — treated as such per
`docs/legal/decisions.md` §2.2). Data categories were the verdict
(yes/no/maybe), free-text reason, score snapshot at submission time,
timestamps, and application status. Retention was `VERDICT_RETENTION_MONTHS`
(default 24 months). None of this applies now: Apply is evidenced by a live
Application/history record; a minimal Consultant–Job review marker only
prevents ordinary resurfacing; and there is no learning-loop use of Reject
text left to describe, since §2's config generation reads only `profile.md`.

## 4. Third-party personal data in job advertisements

| Field | Detail |
| --- | --- |
| **Purpose** | Collected job postings and forwarded job-lead emails frequently contain a named hiring-manager or recruiter contact (name, email, phone) inside the free-text `raw_text` field. This is incidental to the job-collection purpose (finding assignments for consultants), not a purpose in its own right. |
| **Legal basis** | Art 14(5)(b) — informing each contact individually would involve disproportionate effort relative to the low sensitivity of professional-context contact details, and no profile is ever built on these individuals (`docs/legal/decisions.md` §2.1). ⚠️ **This basis is weaker than it was and is an open item (V2 Slice 1).** It rested on two limbs: no profiling, and *short retention*. The no-profiling limb still holds (read-only pass-through, never scored or matched against the contact). The short-retention limb **no longer holds** — the 7-day job purge was deleted with the JSON Job Store and no automatic Job retention exists (see Retention). One fact partly offsets it: V2 Jobs are entered by hand, one at a time, so the volume and the chance of incidental contact PII are far lower than under automatic collection. The email cleaner that used to strip header/signature blocks was removed as dead code in STAB-7 (see the minimisation row below) and no automatic minimisation replaces it, so that mitigation no longer applies. What remains is thinner than either original limb. **Decide a Job retention rule, or re-argue Art 14(5)(b) without the retention limb, before production.** |
| **Data subjects** | Third parties named in scraped job ads or forwarded lead emails (typically a hiring manager, recruiter, or generic company contact) — not the Company's consultants or employees. |
| **Data categories** | Name, email, phone number, appearing inline in a Job's free-text `description` (Postgres `jobs`, entered by hand since V2 Slice 1; formerly `raw_text` in the JSON store). |
| **Recipients** | None beyond internal TA/consultant matching users — this data is not sent to Anthropic as part of any prompt that specifically targets the contact (it may incidentally be present within a job description passed to the matcher/generator, same as any other job-ad text). AWS (hosting; the Postgres `jobs` table since V2 Slice 1). |
| **International transfers** | Job text may pass through the same Anthropic call sites as any other job description content used in matching/CV generation — same transfer basis as §1/§2. |
| **Retention** | ⚠️ **None — open item (V2 Slice 1).** There is no automatic Job retention or deletion. A Job is a row in Postgres `jobs`; a person closes it when the position is gone and the row is kept. The V1 mechanism this row used to describe — marked `expired`, hard-deleted after `PURGE_GRACE_DAYS = 7` by `backend/opportunity_pipeline/job_store/service.py` — was **deleted** with the JSON Job Store, and nothing replaced it: operational Delete versus hard delete is an open product/legal decision (see [`docs/product.md`](../product.md)). Erasing a consultant removes their link and review markers but not the Job. |
| **Security measures / minimisation** | ⚠️ **This control no longer exists — open item, surfaced by STAB-7 (2026-09-04).** The email cleaner (`_clean_email_noise`) that used to strip a leading `From:/To:/Cc:/Sent:/Date:/Subject:` header block, quoted-reply threads, signature blocks, and legal disclaimers from forwarded job-lead emails lived in `shared/collection.py`, which had zero remaining callers (V2's manual `+ Job` entry never called it) and was removed as dead code in STAB-7. `POST /api/jobs`'s text normalization (`shared.job_record.clean_text`, still live) only collapses whitespace/HTML entities — it does not strip a forwarded-email header block, quotes, signatures, or disclaimers. Whether a Job is entered by hand into `description`, third-party contact PII pasted into that field is no longer passed through any automatic minimisation step; a TA/consultant manually removing a contact line before saving is the only remaining safeguard. **Decide whether to reintroduce equivalent stripping at the manual-entry boundary, or accept manual removal as the control**, alongside the retention decision the row above already flags open. |

## 5. TA Cockpit — local collection & triage (laptop-side processing) — RETIRED (V2 Step 1, ADR-010)

**This processing activity no longer exists.** The TA Cockpit — local browser
collection, local triage, and the laptop-side profile/matching-config cache
described below — was removed in V2 Step 1 per ADR-010. Jobs are created and
managed directly in the cloud UI; no consultant profile or matching-config
data is synced to or cached on a TA laptop. The activity is retained below,
struck through in substance, as a historical record of what this ROPA
previously listed.

*Original entry (V1, resolved):* This was the same controller processing as §2/§3, extended onto a Talent Advisor's laptop (ADR-008) — **not** a separate processor, per `docs/legal/decisions.md` §2.1, but it extended the security perimeter onto laptop hardware. Purpose: collect raw job postings locally (browser scraping could not run in the cloud), score them on-the-fly for instant desktop notification, and let the TA curate which jobs get pushed to the cloud job store. Data categories: each actively-looking consultant's profile and matching config, cached at `data/ta_cockpit/configs_cache.json`, plus a local SQLite triage pool — neither held scores. Retention had a known residual gap (no TTL on a stale cache if sync never ran again). None of this applies now.

## 6. CV publishing — shareable hiring-manager links

Introduced by `docs/architecture/adr/ADR-009-cv-publish-capability-links.md`, after Sessions 1–4 of the legal-compliance plan. When a consultant (or their TA) finishes a tailored CV, they can **publish** it: the system mints an unguessable capability link to a read-only web version that an external hiring manager can open without an account. This is a new disclosure to a recipient *outside* the Company, so it is recorded as its own processing activity.

| Field | Detail |
| --- | --- |
| **Purpose** | Share a specific consultant's tailored CV with an external hiring manager as part of proposing that consultant for a specific assignment — the placement handshake. Always tied to a `(consultant, job)` position (ADR-001 shared artefact); there is no standalone publish. |
| **Legal basis** | Art 6(1)(b) — putting a consultant forward to a prospective client/hiring manager is a core, expected step in performing the consultant staffing engagement. The act is a deliberate human publish click with a confirm step (CLAUDE.md invariant #9), not a background process. **Never consent** (same employer/employee power-imbalance reasoning as §1). |
| **Data subjects** | The consultant whose CV is published. (The hiring manager who opens the link is not profiled — no viewer identifier is stored, see Security/minimisation.) |
| **Data categories** | A point-in-time snapshot of the tailored CV as the consultant approved it: the editor form-state (`cv_state` JSONB — career history, education, skills, languages, courses, and the contact details shown on the CV) and a publication-owned copy of the profile photo, stored in Postgres `cv_publications` + on disk under `CV_PUBLISH_PHOTO_DIR` (`backend/db/models/cv_publish.py`, `backend/cv_publish/service.py`). Plus link lifecycle/telemetry: view count, first/last-viewed timestamps, expiry, revoked flag. **No viewer IP or user-agent is ever stored** (ADR-009 data minimisation). |
| **Recipients** | **Anyone in possession of the link** — a capability URL grants read access with no further authentication. Stated plainly because that is the model: possession of the link is access. The intended recipient is the hiring manager the consultant/TA sends it to. AWS (hosting). **Not** sent to Anthropic. |
| **International transfers** | None introduced by the Company — the published CV is served from the same eu-north-1 hosting. The recipient may open the link from anywhere, but that is the recipient's own access, not a Company-initiated transfer to a processor. |
| **Retention** | `CV_PUBLISH_TTL_DAYS` (default 28 days), resettable by republishing or an explicit extend. On expiry (or revoke) the content — the `cv_state` snapshot **and** the photo file — is **hard-deleted** at the next purge (`backend/cv_publish/service.py::purge_expired`), which now runs automatically via a daily lifespan task in `backend/main.py` as well as on-demand via `POST /api/admin/retention/run`. Only the position's status history in the Application record survives afterwards (metadata, no CV content). |
| **Security measures / minimisation** | Capability-link compensating controls (ADR-009): a high-entropy (128-bit+) unguessable token, `X-Robots-Tag: noindex` on every public response (404 on unknown tokens gives no enumeration signal, 410 on expired/revoked), a fixed renewable lifetime, and one-click revoke (TA dashboard / editor). View tracking is deliberately minimal — count + timestamps only, **no IP/UA** — and the "opened" signal fires from a human-interaction beacon on the viewer page, not on page load, so bots/prefetchers don't trip it. Anonymous viewers are served **no cookies**. The published snapshot is a copy taken at publish time, so a later profile/photo change never silently alters an already-shared CV. Served only over HTTPS once TLS + the real domain are in front (Session 5; GDPR Art 32 transport security). |

**Planned V2 change to this activity:** the publication becomes Application-owned;
the stable raw token appears only after `#` in the recipient URL and is exchanged in
a body/header request against a hash-only database value. The viewer exposes only
sanitized approved CV content/photo, no internal identifiers or third-party assets,
in one bounded exchange response (no anonymous photo URL), and carries
CSP/no-store/no-referrer/no-frame/nosniff/noindex controls. No rendered
PDF is stored. Expiry blocks anonymous access and creates action-required work but
does not delete content; Extend restores the same link; a confirmed
Hired/Rejected/Closed action automatically ends the Application/link and deletes the
tailored snapshot/photo. This paragraph is prospective until verified at cutover.
Expired content can therefore remain beyond 28 days until a human acts. The TA
dashboard pins it and Admin sees a prominent oldest-first queue; age and periodic
review are required, but privacy review must accept the residual lack of an
automatic terminal retention date.

## 7. Data-subject rights execution (cross-cutting)

Export (Art 15/20) and erasure (Art 17) are admin-only, not consultant-self-serve (locked decision #2) — see `backend/privacy/routes.py` / `backend/privacy/service.py` (Session 2). `GET /api/privacy/consultants/{username}/export` returns a ZIP of the `users` row (minus `password_hash`), the full `data/profiles/<username>/` tree, CV-publication metadata (`publications.json` — metadata only, never the `cv_state` content), TA associations, the consultant's relational Job associations and their own review markers (decision and timestamp) (`job_associations.json`, V2 Slice 1), and their Applications — which job they were put forward for, the stage it reached, and when it last moved (`applications.json`, V2 Slice 2). (The export used to also include `verdicts.json`; that stopped once the `verdicts` table was dropped with no writer left, V3 packet S2.) Applications are a separate file rather than a field nested inside `job_associations.json`, because that file is built by joining through the Job link and an Application must not have its disclosure depend on a different row still existing. **V2 Slice 3 adds `tailored_cvs.json`, and it is deliberately not metadata-only**: unlike a publication (whose `cv_state` is a copy of something the subject already holds), erasure hard-deletes the tailored CV itself, so access has to disclose the same thing — the CV document (`cv_state`) and the frozen `input_snapshot` it was generated from. An Art 15 artefact that handed back less than the same module will delete is the inconsistency the right of access exists to prevent. It reaches only the subject's own Applications, so another consultant's tailored CV on the same shared Job is never included. The Job-associations file carries enough of each shared Job to be intelligible (id, title, company, location, status) but **never another consultant's link, review, or Application**: a shared Job's facts are common, its relationships are not, and a subject-access request is not a route to a third party's data. `DELETE /api/privacy/consultants/{username}` performs an ordered hard delete: publications (photo files first, then rows) → sessions/invites/API tokens → TA associations → notifications → tailored CVs → Applications → Job links and review markers → profile directory → `users` row (tailored CVs before the Applications they hang off, so the deletion never depends on a cascade having fired first), and writes a minimal `AuditLog` row (`backend/db/models/audit.py`) recording who erased whom (by a non-reversible username hash) and when — never the erased content itself. This satisfies GDPR Art 5(2) accountability for the erasure action while keeping the audit trail itself free of the personal data it documents.

Backups: see `docs/legal/backup-policy.md` (Session 4) — a stated 7-day rotation policy that an erasure propagates through; independently verified against the live infra-repo backup configuration (recorded in `docs/legal/backup-policy.md`, carried into `docs/legal/dpia.md`).

## 8. Summary retention table

| Data | Retention | Config / enforcement |
| --- | --- | --- |
| Uploaded CV files (V2 durable import, `backend/cv_importer/`) | Deleted after the import handler's last required stage — ordinarily within seconds. A rare crash or storage-level delete failure can leave a small residual file until the next import for that consultant or their offboarding erasure — accepted for this pilot, no scheduled purge | `backend/cv_importer/handler.py`, `backend/cv_importer/service.py` |
| Uploaded CV files (pre-V2 synchronous-importer archive already on disk) | 30 days after import | `UPLOAD_RETENTION_DAYS` (`backend/config.py`) → `experience_writer.py::purge_old_uploads` |
| CV publications (snapshot + photo copy) | Hard-deleted once past `CV_PUBLISH_TTL_DAYS` (28 days, renewable) expiry or on revoke | `backend/cv_publish/service.py::purge_expired` (daily lifespan task in `backend/main.py` + admin endpoint) |
| Jobs (Postgres `jobs`) | ⚠️ **No automatic retention.** Closed by a person, then kept indefinitely | None — open decision (see [`docs/product.md`](../product.md)); the V1 7-day job-store grace purge was deleted in V2 Slice 1 |
| Job links / review markers (`job_consultants`, `job_reviews`) | Duration of engagement, until offboarding erasure | `backend/privacy/service.py::erase_consultant` |
| Applications (Postgres `applications`) | ⚠️ **No automatic retention.** A closed or withdrawn Application is kept as history — deliberately, since the same pair may be applied to again — until offboarding erasure | `backend/privacy/service.py::erase_consultant`; no purge exists, the same open operational-delete decision as Jobs (see [`docs/product.md`](../product.md)) |
| Tailored CVs (Postgres `tailored_cvs`) | ⚠️ **No automatic retention.** One per Application, kept for the Application's lifetime — including after it ends, since it is the record of what was actually sent — until offboarding erasure | `backend/privacy/service.py::erase_consultant` (deleted explicitly, and again by the Application's `ON DELETE CASCADE`); no purge exists. This is the **third** table now carrying the same open operational-delete decision (see [`docs/product.md`](../product.md)), and the one holding the most content per row — worth deciding once, for all three |
| Database backups | ≤7 days, then rolled off | Infra-verified policy — `docs/legal/backup-policy.md` |
| Live profile/config/account data | Duration of engagement, until offboarding erasure | `backend/privacy/service.py::erase_consultant` |

The Cockpit config-cache row above (`local_tools/app/config_sync.py`) is removed: the Cockpit no longer exists (V2 Step 1, ADR-010), so there is no local cache to retain or purge.

For V2, replace the publication row above with: **anonymous access ends at 28 days;
the active Application snapshot/photo remains until Extend or confirmed
Hired/Rejected/Closed**. This human-only deletion boundary is a planned V2 exception
to the current automatic purge and remains a DPIA/privacy launch item.

The code-enforced purges above (uploads, CV publications) can be run on demand via `POST /api/admin/retention/run` (`backend/retention/routes.py`) and **also now run automatically**: `backend/main.py` attaches a daily lifespan background task (`_retention_purge_loop`, added by cv-publish Session 4) that fires `run_retention_purges` once at startup and every 24h thereafter, so an expired CV publication (and any aged-out upload) is purged without an admin remembering to trigger it. This is a backend/Postgres-only scheduler; the TA Cockpit's local scheduler no longer exists (retired, V2 Step 1, ADR-010). (A third purge, verdict rows, ran here until the `verdicts` table was dropped — V3 packet S2.)
