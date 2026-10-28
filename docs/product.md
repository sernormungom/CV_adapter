# CV Adapter — product handover

**As of:** 2026-10-01. A short page for whoever owns the product now: what state
it is in, what it deliberately does not do, and what is still open. Rules for
each subsystem live in its Contract and Guards
([docs/architecture/areas/](architecture/areas/README.md)). Starting, testing
and deploying are covered in [running.md](running.md), [testing.md](testing.md)
and [DEPLOY.md](../DEPLOY.md). End-user and admin guides are
[GUIDE.md](../GUIDE.md) and [ADMIN.md](../ADMIN.md).

## What it is

A FastAPI modular monolith on Postgres, plus a per-consultant profile directory
(`data/profiles/<username>/`). It has two independent pipelines:

- **CV pipeline:** import a consultant's CV into their base profile, generate a
  CV tailored to an Application, and publish a read-only link for a hiring
  manager.
- **Matching pipeline:** record Jobs by hand, give each consultant an advisory
  score for each Job, and track Apply/Reject decisions.

The only artefact the two pipelines share is the (consultant, Job) pair.
Long-running work (import, generation, config generation) runs on a small durable
in-process workflow runner, with no separate queue or worker service. The Claude
API is the only LLM.

## Operating posture

This is a **pre-production pilot on fabricated, disposable data**, and downtime
is acceptable. Nobody is owed a data migration, a compatibility layer, restore
testing or a zero-downtime rollout. When the data model changes, recreating the
database is an acceptable answer.

The following are **not** relaxed by the pilot posture:

- authentication is enforced, and startup refuses to disable it when
  `APP_ENV=production`;
- authorization goes through one policy module (`backend/authz/policy.py`);
- secrets and capability tokens are kept safe (hash-only link tokens, no secrets
  in images or logs);
- uploaded source files are deleted after import;
- durable work is idempotent;
- CI gates every image that is built.

Any destructive AWS action needs explicit approval that names the exact
resources.

The seed (`make seed-pilot`) creates four Sites (GBG, MAL, STH, UPS) and a
fabricated three-person cast, all on GBG: one TA, one Admin+Consultant and one
Consultant.

## Rules that are easy to break by accident

Each rule has a home in an area Guard. They are listed here because they cut
across areas.

- **A human decides every outward or employment-process action.** No score or
  LLM output may submit a consultant, choose Apply or Reject, publish or send a
  CV, or delete or anonymize data. The one exception is the anonymous
  `link_opened` view count on a published CV. Showing a Job to every eligible
  consultant at its Site lists it; it decides nothing. This is a GDPR Art 22 /
  automation-bias control. The launch gate is
  [ai-act-assessment.md](legal/ai-act-assessment.md).
- **Scores are advisory and cached per (Job, consultant) pair, never on the
  Job.** The cache is lazily filled and unversioned. A missing or failed score is
  `scored: false` with a null score, and it never hides or rejects a Job. Cached
  rows are deleted when a Job closes, when a consultant stops looking, and on
  erasure. See [jobs.md](architecture/areas/jobs.md) G2, G3 and G9.
- **Site governs Jobs and matching only.** Profiles, CVs, imports, publications
  and privacy actions are decided by role and assignment: Admin reaches
  everything, a TA reaches assigned consultants, a consultant reaches
  themselves. See [authz.md](architecture/areas/authz.md) G3.
- **A matching config is generated only when a person clicks "Update match
  configurator", and only from `profile.md`.** A failed or invalid generation
  leaves the previous config in place. The old verdict-fed learning loop is
  removed; do not rebuild it without a product decision.
- **Tailoring never writes the base CV.** Every base-profile write goes through
  one gate. See [cv-artifacts.md](architecture/areas/cv-artifacts.md) G1, G12 and
  G13.
- **Ending an Application revokes its public link and deletes its tailored CV in
  the same commit.** See [applications.md](architecture/areas/applications.md) G5.

## Deliberate limitations

These are accepted trade-offs, not bugs. Each area's "Accepted limitations" list
has the details.

- Jobs are created by hand (`+ Job`). Automatic ingestion and browser scraping
  were discarded, so do not add a scraping or bearer-token push path.
- Scores have no freshness tracking. A Job edit, a profile change or a config
  change does not refresh a cached score.
- Near-duplicate Job detection is a deterministic best-effort precheck. Two
  racing near-duplicate submissions can both land.
- There is no cross-Site Job sharing, TA assignment or Site transfer. Admin
  covers the rare cross-Site need.
- A consultant cannot opt out of matching in the app. Only their assigned TA,
  or an Admin, can set "actively looking"
  (`POST /api/ta/consultants/{username}/looking`).
- Public-link throttling is a global cap, not per IP.
- The importer's scratch-directory sweep was removed on purpose. A stray temp
  file after a rare double failure costs disk space and nothing else.

## Before real consultant data or genuine external sharing

None of these is done. Each one blocks using real CVs or sending links outside
the team. Check the code before relying on this list; it describes the code as
of the date above.

1. **Lawful basis and participant authorization** for every real CV are
   recorded, and counsel has reviewed the final processing description.
2. **Legal and privacy documents match the system.** Known mismatches:
   - `html/privacy.html`, `docs/legal/ropa.md` and `docs/legal/dpia.md` say
     expired links are hard-deleted (by a `purge_expired` that does not exist).
     In the code, expiry only blocks access, and the purge deletes revoked links
     only (`cv_publish/service.py::purge_revoked`).
   - `ropa.md` says tailored CVs are kept after an Application ends. They are
     deleted at the terminal transition (`applications/service.py`).
   - `ropa.md` says scores are never persisted. They are cached in
     `job_consultant_matches`.
   - `ai-act-assessment.md`, `lia.md` and `legal/decisions.md` describe scores
     as versioned, with visible staleness. The cache has no version.
   - `ai-act-assessment.md`, `dpia.md` and `lia.md` still rely on an automatic
     config-learning loop with a delta guard. That loop was removed.
   - `dpia.md` and `ropa.md` list API tokens in the erasure order (they are
     retired). Neither document lists the cached scores and CV imports that
     erasure deletes, or the `matching_scores.json` file that the export
     produces (`privacy/service.py`).
   - `dpia.md` and `ropa.md` call the public-viewer hardening "planned", although
     most of it is implemented. Only the first-party fonts are still missing.
3. **First-party viewer assets and a narrow CSP.** The public CV viewer and the
   app pages load Google Fonts, and the CSP allows it.
4. **HTTPS end to end.** Set `SESSION_COOKIE_SECURE=true`, which also turns on
   HSTS; it defaults to false. Serve over TLS: the documented dev URL is plain
   `http://`, and TLS lives in the infra repository.
5. **A retention lifecycle for every cloud object.** Missing today:
   - a Job or Application purge;
   - a purge of expired links;
   - an agreed link lifetime (the config default is 28 days, the owner's intent
     was 7);
   - a "Hired" status, with its notification and a profile-deletion clock;
   - retention for `workflow_runs` rows, which are never deleted and which
     erasure skips.

   Open questions: what does the profile clock count from; can a Job be deleted
   while terminal Application history depends on it; and what proves an export
   happened before a cloud deletion?
6. **An Admin retention console** to review, export and delete eligible data.
   Today there is only `POST /api/admin/retention/run`. Design it before
   building it: start with one dashboard with type filters and one-object
   actions, and leave batch deletion until single-object deletion is proven.

## Unfinished product items

| Item | State |
| --- | --- |
| Tailored-CV save reply can hand back another tab's edit | **Known defect, deliberately left for later.** The write path in `backend/tailored_cv/service.py` re-reads the row after the commit has released the lock, which allows a lost update or a 500 on a concurrent delete. The fix is described in [cv-artifacts.md](architecture/areas/cv-artifacts.md). |
| Readable Site names | Not built. Sites are seeded with `name = code`, and the admin dashboard shows the code. |
| Photo-required CV generation: browser check | The code and automated tests are done: generation is refused without a profile photo. The manual browser walkthrough of the refusal has not been run. Chromium needs `libnspr4`/`libasound` under WSL, or use the dev container. |
| `personal_interests` is never used | The field is editable, but no prompt or schema guidance reads it. This is a prompt/schema change, not code. |
| Generated config invents hard blockers | Some hard blockers (citizenship, clearance) do not trace back to a profile fact. Fix it in `matching-config.schema.md` and the config generator, then re-check with the phrasings in the [notes experiment](evidence/v3-preferences-notes-experiment-2026-09-24.md). |
| Explain score components on the Jobs board | Not built. Add a one-line explanation for each component (`role_fit` … `risk_deduction`), checked against the scorer and the schema. |
| Jobs header count | Not built. The header should show `(total; actionable)` for the selected consultant instead of `(total; unscored)`. It needs either a client recompute or a server filter. |
| Model refresh | Research only. Evaluate a newer Claude model on extraction, merge, config and generation for cost and latency. |

## Reference IDs in code comments

Comments and tests cite short labels such as `P-009`, `V3-P-008`, `STAB-2` or
`S21`. They name past decisions and work items whose records were retired. The
[glossary](architecture/areas/README.md#reference-ids) gives one line for each
label that still appears often. The rule itself is always in the area Guard
next to the code.
