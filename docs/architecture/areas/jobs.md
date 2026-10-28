# Jobs, on-demand scoring, and Reject

**Owns:** `backend/jobs/`, `backend/matching/`, tables `jobs`,
`job_decision_pairs`, `job_reviews`, `job_consultant_matches`,
`html/jobs.html`, `data/profiles/<u>/matching_config.yaml`.
**Decisions:** P-007, P-008; V3-P-005, V3-P-006, V3-P-007, V3-P-008,
V3-P-015, V3-P-016, V3-P-021, V3-P-032; invariants 7a, 7
(superseded — see G2); P-018 (Slice 6 discarded).
**Read alongside:** [applications.md](applications.md) — the two share a lock
order and the one-live-decision rule; [authz.md](authz.md) G3 for the reversed
Site-predicate rule. Always read `project_context/matching-config.schema.md`
with any scoring change. Design rationale:
[ADR-007](../adr/ADR-007-per-consultant-scoring.md) (per-consultant scoring),
[ADR-012](../adr/ADR-012-versioned-stored-advisory-matches.md) (deferred
*versioned* projections — distinct from S1's plain cache),
[ADR-010](../adr/ADR-010-email-only-automatic-job-ingestion.md) (why ingestion
is not scraping).

**Postgres is the only authoritative Job store.** V1's one-JSON-file-per-job
tree, review-batch matcher, and Maybe/verdict dashboard are all retired.

---

## Contract

| File | Responsibility |
|---|---|
| `jobs/service.py` | Job and review writes. Normalizes through `shared.job_record.clean_text`, hashes the folded `(title, company, description)` triple, owns the lock helpers. `materialize_decision_pair` (formerly `link_consultant`) is called lazily by `lock_pair`, never at creation. `find_near_duplicate` runs the two-tier Jaccard precheck (G12) before every insert attempt. |
| `jobs/routes.py` | Thin — every access decision is a named predicate from `backend/authz/policy.py`. `list_jobs` builds its own Site/score-aware query directly (G10); the old `jobs/service.py::list_jobs` helper it used to call is gone (no caller left). |
| `matching/service.py` | `score_job_for_consultant(job, consultant)` — pure, writes nothing. Called only from `matching/cache.py`, never directly from a route. |
| `matching/cache.py` | `read_score(db, job, consultant)` — the write-through cache (G2). The only thing that ever writes `job_consultant_matches`. |
| `matching/config_reader.py` | Loads/backs-up/writes `matching_config.yaml` via `_atomic_write` (temp file + `os.replace`) — no longer a plain `write_text`. |
| `html/jobs.html` | The authenticated client. There is no "Link consultant" picker any more: an open Job reaches every actively-looking Consultant at its Site automatically (G10). Owns Create/Edit/Score/Apply/Reject/Close only — the Application list/detail, its status transitions, and the tailored-CV workflow moved to their own dashboard, `html/applications.html` (V3-P-003, S6); Apply here still creates the Application row, it just no longer lists them. Hides the acting Consultant's own decided Jobs from its list (S21 — see "UI entry point" below). |

**Routes** (`/api/jobs`): create · list · view · edit · close ·
`GET /{id}/score` · `POST /{id}/reject` · `DELETE /{id}/reject`. There is no
`POST /{id}/consultants` — RCB-D2's manual link was retired by S1; a request
to it now 404s like any other unmounted route
(`tests/test_jobs.py::test_manual_link_endpoint_is_retired_for_every_actor`).

Create authenticates by **session cookie only** — the bearer API-token path
kept for a conditional email-ingestion forwarder was retired with that slice
(STAB-6, P-018).

**`job_reviews.decision` is check-constrained to `('reject')`** (G4). **A score
is now stored** — a lazily-filled cache row, not a column on `jobs` — see G2.

---

## Guards

### G1 — The duplicate contract is a database constraint, not a prior SELECT

`content_hash` is unique over the folded `(title, company, description)`
triple **within one Site while a Job is open** — the partial unique index
`uq_jobs_open_site_content` on `(site_id, content_hash) WHERE status = 'open'`
(S1; previously a plain global-unique index). A closed Job's hash is free to
repeat, and the same posting may recur at a different Site. `create_job`
treats an `IntegrityError` on insert as the **expected** duplicate path: it
re-reads the winner and reports `duplicate`. **It does not materialize a
decision pair** for the duplicate either — creation never does, since S1 (G10).
*Why:* two concurrent identical submissions both pass a check-then-insert.
Same-Site is not the same boundary as `can_access_job` (G12's note applies
here too): the route projects `duplicate`'s matched Job through
`policy.can_access_job` before attaching it, the same as `near_duplicate`.
*Pinned by:* `tests/integration/test_jobs_pg.py::test_two_concurrent_identical_creates_yield_exactly_one_job`,
`::test_the_unique_content_hash_is_enforced_by_the_migrated_database`,
`::test_a_raw_duplicate_insert_is_rejected`,
`tests/test_jobs.py::test_identical_submission_creates_one_job`,
`::test_duplicate_detection_ignores_case_and_whitespace`,
`::test_a_genuinely_different_posting_is_not_a_duplicate`,
`::test_duplicate_does_not_materialize_a_decision_pair`,
`::test_content_hash_separates_fields`.

### G2 — A score belongs to the Consultant–Job pair, and is now a lazily-filled cache (V3-P-006/P-008 supersede invariant 7)

`matching/service.py::score_job_for_consultant` still computes purely and
writes nothing; the reversal is one layer up. `matching/cache.py::read_score`
is the **only** thing that ever writes a score: it reads
`job_consultant_matches` first and, on a miss for an eligible pair, computes
and upserts one row keyed `(job_id, consultant_id)` — the unique constraint
makes a concurrent or repeated fill idempotent. There is no background
fan-out, no refresh on Job edit/chat patch/config change, and no version
column: a slightly old indicator is accepted (V3-P-008 decision 1). A
missing/unparseable profile or missing config still returns `scored: false`
with a `reason` slug and a **null** `match_score` — not a zero, not a
500 — and is never cached (only `scored: true` results are upserted). **A
low, missing, or failed score must never hide, reject, discard, or make a Job
ineligible:** it stays listed, fetchable, editable and rejectable. That is an
**API** guarantee; the Jobs page hides a Job only after the acting
Consultant's own human decision, never for a score (see "UI entry point").
*Why stored now:* sort-and-filter-by-score over SQL needs the value in the
database; storage is for querying, not for speed (~0.19 ms per posting,
[evidence](../../evidence/v3-scorer-timing-2026-09-10.md)).
*Pinned by:* `tests/test_jobs.py::test_score_is_cached_separately_from_the_job`,
`::test_missing_profile_is_an_honest_failure_not_a_zero_or_a_500`,
`::test_missing_config_is_reported_separately`,
`::test_unscorable_job_is_still_listed_and_fetchable`,
`::test_scoring_text_carries_the_manual_entry_fields`.

### G3 — An opted-out consultant is refused before their profile is read, and their cached scores are cleared

`actively_looking is False` → `reason: "opted_out"`
(`matching_service.REASON_OPTED_OUT`), checked **before** `load_profile` /
`load_config` run at all, so an opted-out consultant's profile is never opened.
Their open-Job opportunities disappear immediately from `GET /api/jobs` (S1
acceptance 5) even though existing `job_consultant_matches`/decision-pair rows
are not deleted for that reason alone — `tenancy/routes.py::set_consultant_looking`
deletes their cache rows explicitly on opt-out, so a re-list never serves a
stale cached score either.
*Why:* this is the Art 21 opt-out enforcement `docs/legal/lia.md` conditioned
its conclusion on (STAB-1). Opting back in resumes scoring with no persisted
suppression and no backfill (S1 exclusions) — it simply refills lazily on the
next read. An opted-out consultant's Jobs stay viewable and rejectable through
`GET /api/applications` and existing history; they leave the open-Jobs list.
*Pinned by:* `tests/test_jobs.py::test_opted_out_consultant_is_not_scored_and_profile_is_not_read`,
`::test_opting_back_in_resumes_scoring_with_no_persisted_suppression`,
`::test_opt_out_hides_opportunities_but_preserves_application_history`.

### G4 — There is no Maybe, and it is unrepresentable (invariant 7a, P-008)

The `decision` check constraint accepts only `reject`, so the removed value
cannot be stored rather than merely being rejected by a route. **Do not add a
compatibility translation from V1's `yes`/`no`/`maybe` vocabulary.**
*Pinned by:* `tests/test_jobs.py::test_there_is_no_maybe_anywhere_in_the_api`,
`::test_the_schema_itself_cannot_hold_maybe`,
`tests/integration/test_jobs_pg.py::test_the_database_itself_refuses_a_maybe_decision`.

### G5 — A pair holds one live decision: a Reject marker *or* a live Application

`record_review` refuses (409) a pair with a live Application; `applications`
refuses a rejected pair. The two write **different tables**, so no unique index
can enforce it — both take the pair's `job_consultants` row lock first
(`jobs.service.lock_pair`). `DELETE /api/jobs/{id}/reject`
(`withdraw_review`) is the documented way out, kept a separate explicit command
rather than a side effect of Apply, because removing a recorded human decision
is itself one (invariant 9).
*Pinned by:* `tests/integration/test_applications_pg.py::test_a_concurrent_apply_and_reject_cannot_both_land`,
`tests/integration/test_jobs_pg.py::test_concurrent_rejects_leave_exactly_one_review_marker`,
`tests/test_jobs.py::test_reject_is_idempotent`,
`::test_related_ta_and_consultant_share_one_review_marker`.

**Reject collects no reason** (V3-P-020, S20): `job_reviews` holds a decision,
who made it, and a timestamp — nothing free-text. The client submits Reject
immediately with no prompt; `POST /{id}/reject` takes only
`consultant_username` and its `review` response has no `reason` key; the
column was dropped by migration `0005_v3_drop_job_review_reason`. The GDPR
Art 15/20 export (`privacy/service.py::_job_associations`) likewise carries
only the decision and timestamp. Unrelated: `matching/service.py`'s
`REASON_*` slugs (G2) are why a Job is unscorable.
*Pinned by:* `tests/test_jobs.py::test_reject_collects_and_echoes_no_reason`,
`tests/test_privacy.py::test_export_includes_the_subjects_job_links_and_own_review`.

### G6 — Lock order: Job row → pair row → Application row

`lock_job` → `lock_pair` → (Applications' own row lock). Identical in both
services, so they cannot deadlock. **Any write whose decision depends on state
another request can change must re-read that state under its lock.**
*Why:* the first round validated `job.status` off the caller's already-loaded
instance, so an overlapping close could be ignored.
*The deliberate exception:* `withdraw_review` takes neither lock. It removes a
marker rather than deciding anything against contended state, and is allowed on
a closed Job (closing refuses *new* evaluations; withdrawing is the removal of
one) — see its docstring. Do not "fix" it by adding a lock without a reason.
*Pinned by:* `tests/integration/test_applications_pg.py::test_apply_cannot_land_after_a_concurrent_close`,
`::test_reject_cannot_land_after_a_concurrent_close`,
`tests/test_jobs.py::test_the_service_refuses_a_closed_job_even_bypassing_the_route`.

### G7 — Reaching the Job is 404-secret; the per-Consultant step is 403. Site is now a legitimate predicate here (reverses invariant 6a for this area — see [authz.md](authz.md) G3)

Two predicates, two answers. The distinction is deliberate and each half is
separately tested.

| Step | Predicate | Refusal |
|---|---|---|
| May the caller reach this Job at all? | `ensure_job_access` → `can_access_job` | **404** — a Job's existence is itself content |
| Does the named Consultant belong to this Job's Site? | `ensure_consultant_operates_job` | **403** |

Admin reaches any Job unconditionally. A TA reaches a Job at their own Site; a
Consultant reaches an open Job at their own Site while active and
actively-looking, or a closed Job they hold a decision pair on (history
persists past opt-out/close — S1 acceptance 5). There is no per-Consultant
link check any more: S1 replaced "linked to this Job" with "same Site as this
Job" — `ensure_consultant_operates_job` now calls `same_site(job, consultant)`
instead of reading `job_decision_pairs`. It still exists as a distinct step
because the *actor* reaching the Job (their own Site) and the *named
Consultant* the actor is scoring/rejecting for can differ — a TA must not
score or reject on behalf of a Consultant outside the Job's Site even if the
TA themselves legitimately reaches the Job.
*Pinned by* — the **404** half:
`tests/test_jobs.py::test_unrelated_ta_gets_404_and_learns_no_job_content`,
`::test_unrelated_ta_cannot_edit_close_score_or_reject`,
`::test_unrelated_ta_gets_404_linking_anyone`.
The **403** half: `::test_same_site_consultant_needs_no_decision_pair_to_score`,
`::test_a_second_ta_cannot_reject_for_the_first_pairs_consultant`,
`::test_site_membership_does_not_grant_sibling_consultant_actions`.
Both: `::test_admin_reaches_every_job`,
`::test_job_access_refuses_a_different_site_even_with_a_decision_pair`,
`::test_job_routes_require_authentication`.

### G8 — Link and review projections are relationship-scoped, and list must equal detail

A duplicate response hides the other Consultant and their review. The list's
review map is scoped exactly like the detail's, for every actor.
*Why:* two views of the same scoped data are where a leak hides.
*Pinned by:* `tests/test_jobs.py::test_list_and_detail_report_the_same_reviews_for_every_actor`,
`::test_a_consultant_sees_only_their_own_link_and_review`,
`::test_duplicate_response_hides_the_other_consultant_and_their_review`,
`::test_the_list_review_map_stays_relationship_scoped_for_ta_and_consultant`,
`::test_an_actor_reaching_no_consultant_sees_no_reviews`,
`::test_the_creating_response_is_scoped_too`,
`::test_each_side_still_sees_its_own_review`,
`::test_admin_sees_every_link_and_review`,
`::test_admin_sees_review_markers_in_the_list_not_only_the_detail`,
`::test_the_admin_consultant_sees_their_own_review_in_the_list`.

### G9 — Closing is explicit, retracts nothing, and deletes cached scores

A closed Job cannot be scored or rejected, but stays listed, fetchable and
editable, keeps reviews already made, and deletes/reopens nothing **except**
its `job_consultant_matches` rows (S1 acceptance 4: "Closing a Job deletes its
rows") — `jobs/service.py::close_job` deletes them in the same commit as the
status transition. Idempotent and filterable. "Stays listed" is the API's
`GET /api/jobs`; the Jobs page still hides a closed Job the acting Consultant
has decided, like an open one (see "UI entry point").
*Pinned by:* `tests/test_jobs.py::test_close_is_explicit_idempotent_and_filterable`,
`tests/integration/test_jobs_pg.py::test_close_job_transitions_status_and_clears_cached_scores_on_postgres`,
`::test_a_closed_job_cannot_be_scored_or_rejected`,
`::test_closing_does_not_retract_a_review_already_made`,
`::test_a_closed_job_stays_listed_fetchable_and_editable`,
`::test_closing_a_job_does_not_delete_or_reopen_anything`,
`tests/integration/test_jobs_pg.py::test_job_status_is_check_constrained`.

### G10 — Distribution is Site-wide and automatic; the decision pair materializes lazily at the first decision (S1, reverses RCB-D2's manual link)

RCB-D2's `POST /api/jobs/{job_id}/consultants` and its `link_consultant`
service call are retired (S1 brief acceptance 1, 7) — a request to that route
now 404s like any unmounted route
(`test_manual_link_endpoint_is_retired_for_every_actor`). Every open Job is
immediately visible, with a score, to every active, actively-looking
Consultant at its Site — no user action creates that reachability, `can_access_job`
does (G7). `JobDecisionPair` (table `job_decision_pairs`, formerly
`job_consultants`) no longer means "may see this Job"; it means "a decision
(Reject or Apply) was started on this pair," and `jobs/service.py::lock_pair`
calls `materialize_decision_pair` (formerly `link_consultant`) to create it on
demand, the first time one is needed — never at Job creation, never at a
duplicate response, and never through a Link action. Re-deciding is an
idempotent no-op decided by the unique `job_decision_pairs` constraint
(`uq_job_decision_pair`), not a prior SELECT.
**Cross-Site distribution is explicitly out of scope** (S1 exclusions): a Job
never reaches a Consultant outside its own Site; Admin covers the rare
cross-Site need.
*Pinned by:* `tests/test_jobs.py::test_later_consultant_immediately_reaches_open_site_jobs`,
`::test_site_membership_does_not_grant_sibling_consultant_actions`,
`::test_manual_link_endpoint_is_retired_for_every_actor`,
`::test_unrelated_ta_gets_404_linking_anyone`,
`::test_decision_pair_is_created_only_by_first_decision`,
`::test_duplicate_does_not_materialize_a_decision_pair`.

### G11 — Validation, error mapping, and no stray workflow work

Creating a Job enqueues no workflow run. Every route maps a service error
rather than a 500.
*Pinned by:* `tests/test_jobs.py::test_creating_a_job_enqueues_no_workflow_run`,
`::test_every_job_route_maps_a_service_error_rather_than_500`,
`::test_no_v1_job_routes_remain_mounted`,
`::test_missing_title_or_too_short_description_is_refused`,
`::test_bad_close_date_is_refused`,
`::test_unknown_job_id_shapes_are_404_not_500`,
`::test_unknown_status_filter_is_refused`,
`::test_edit_into_an_existing_job_is_refused`,
`::test_edit_updates_the_shared_job_for_everyone`.

### G12 — Tier 1/2: a lightweight, deterministic two-tier near-duplicate warning on create (V3-P-005/V3-P-006, S9/S11/S13)

`create_job` first looks up whether an **exact** match already exists as an
open Job at this Site (same `content_hash`) and, if so, returns
`"duplicate"` immediately — before Tier 1/2 ever runs. This precedence is
what keeps an exact resubmission's response stable regardless of what else is
open at the Site: without it, a distinct open Job that also happens to be a
genuine Tier 1/2 match to the submission could be scored and returned first,
relabeling the exact resubmission `"near_duplicate"` and pointing it at the
wrong Job — acceptance 1 guarantees an exact match is never relabeled this
way. Only when no such exact match exists does `create_job` run
`find_near_duplicate`, a read-only precheck over this Site's **open** Jobs
only. Both tiers compare the submitted description as a case/whitespace-folded
word set (tokenized on `[a-z0-9]+`, so punctuation such as a hyphen splits a
compound word into two tokens) via Jaccard similarity (`|A ∩ B| / |A ∪ B|`):

- **Tier 1** — description-only, no gate. A score at or above
  `NEAR_DUPLICATE_THRESHOLD` (0.85) matches regardless of title or company:
  high enough confidence that a word-for-word repeat or a superficial
  formatting/reordering change is caught even under a completely different
  title and company.
- **Tier 2** — a moderately reworded body, gated. A score at or above
  `NEAR_DUPLICATE_TIER2_THRESHOLD` (0.55) but below Tier 1 only matches when
  **both** `_title_matches` (an exact folded match, or a
  `difflib.SequenceMatcher` ratio at or above `TITLE_SIMILARITY_THRESHOLD`,
  0.8) **and** `_company_matches` (folded-equal, or vacuously true whenever
  either posting names no company) agree — otherwise two unrelated postings
  that merely share a boilerplate opening would collide.

Either tier returns `status: "near_duplicate"` — **200, not 201, and no new
row** — with the matched existing Job, exactly like an exact `"duplicate"`.
`find_near_duplicate` additionally excludes any candidate whose
`content_hash` already equals the submission's own — an exact match always
scores 1.0 too, so this is a second line of defense for the race window
between `create_job`'s own exact-match lookup above and this scan, not the
primary mechanism that keeps exact precedence (that precedence is decided
before this function ever runs). An *unrelated* open Job that is a genuine
Tier 1/2 match is still a real match and is still returned — precedence only
protects the exact-match case, never masking a distinct posting's own
near-duplicate. Like G1's duplicate path, a near-duplicate match never
materializes a decision pair (G10).

Same-Site is **not** the same access boundary as `can_access_job` (G3): a
Consultant marked `actively_looking: false` still shares a Site with an open
Job they may no longer `GET` (G7's opt-out rule). `find_near_duplicate` itself
stays Site-scoped only — that is the right precheck for "does this collide
with something already posted here" — but the route projects the *response*
through `policy.can_access_job` before ever attaching the matched Job. An
inaccessible match still answers with its `status` and no new row (the
warning and the no-insert effect are both preserved), but `"job"` is `null`:
no id, title, company, or description. The matched Job is a **candidate for
possible disclosure**, decided by the same policy that gates `GET`, not an
access grant by virtue of having collided with the submission.

*Deliberately excluded* (V3-P-006): no LLM/semantic comparison, no
simultaneous-submission lock — this is a best-effort precheck, not a
concurrency-safe constraint, and an occasional pair of racing near-duplicates
is accepted. A Job that only shares a boilerplate *opening* with otherwise
distinct core content scores well under even Tier 2's threshold, and a
moderately-similar body with an unrelated title/company fails Tier 2's gate.
*Pinned by:* `tests/test_jobs.py::test_near_duplicate_formatting_change_is_refused_with_no_new_row`,
`::test_near_duplicate_reordered_content_is_refused`,
`::test_shared_boilerplate_with_different_core_work_is_not_a_near_duplicate`,
`::test_near_duplicate_of_a_closed_job_may_be_created_anew`,
`::test_near_duplicate_is_scoped_to_the_same_site`,
`::test_near_duplicate_does_not_materialize_a_decision_pair`,
`::test_near_duplicate_match_is_not_disclosed_to_an_opted_out_consultant`,
`::test_tier1_match_ignores_title_and_company_even_at_the_threshold_boundary`,
`::test_tier2_match_requires_title_and_company_and_holds_at_the_threshold_boundary`,
`::test_tier2_range_similarity_without_a_matching_title_is_not_a_near_duplicate`,
`::test_an_exact_resubmission_reports_duplicate_not_near_duplicate`,
`::test_exact_resubmission_takes_precedence_over_a_coexisting_near_duplicate`.

### UI entry point — `html/jobs.html` hides the acting Consultant's decided Jobs (V3-P-021/V3-P-032, S21)

Once the Consultant selected in the picker holds a Reject marker or a live
Application on a Job, that row leaves their Open/Closed/All list. It is a
client-side view filter (`visibleJobs`) over the list's own consultant-scoped
`reviews`/`applications` maps (G8, [applications.md](applications.md) G7) —
no query parameter, no endpoint, no Job-level state. Every other Consultant
still sees the Job; with no Consultant selected nothing is hidden. A Job whose
Application ends reappears on its own (the maps carry live Applications only);
that is expected, not designed.

- **Show → Rejected jobs** is a client-only option. The API answers 400 to any
  `status` but `open`/`closed`, so this view sends **no** `status` and filters
  the selected Consultant's rejected Jobs locally — a rejected *closed* Job is
  reachable, and so is **Undo reject**. Applied Jobs have no such view: they
  live under Applications.
- **The confirmation survives the reload.** A decision's row, and its in-row
  message, disappear on the `loadJobs()` that follows, so Reject, Apply and
  Undo reject also write one page-level line (`#decisionMsg`) that
  `loadJobs()` never touches.
- `jobRow` is unchanged: badges, disabled-control tooltips and
  `data-act="unreject"` stay, pinned by `tests/test_static_pages.py`. The header
  `(total; unscored)` is still the server's count and includes hidden Jobs.

*Pinned by:* `tests/test_jobs_client_behaviour.py::test_the_selected_consultants_decided_jobs_are_hidden_by_default`,
`::test_show_rejected_never_sends_a_rejected_status_to_the_server`,
`::test_the_decision_confirmation_survives_the_reload`,
`::test_with_no_consultant_selected_nothing_is_hidden`,
`::test_the_header_counts_still_come_from_the_server`,
`::test_a_fully_decided_list_explains_where_the_jobs_went`.

### Accepted limitations — do not "fix" these

- **Near-duplicate detection is two lightweight deterministic tiers, not a
  confirmation flow** (V3-P-006): no LLM comparison, no manual override, no
  simultaneous-submission lock. Two concurrent near-duplicate submissions can
  both create a row — the same accepted race as the exact constraint's own
  documented one (G1), just without a database constraint behind it.
- **Score freshness tracking, background preparation, and a nightly audit are
  deferred — not built** (ADR-012, S1 exclusions; see
  [product.md](../../product.md)). `job_consultant_matches`
  (G2) is a lazy, unversioned cache — not the versioned/audited projection
  ADR-012 describes; that remains a separate, deferred design.
- **Automatic Learning/config updates are removed, not deferred** — see
  [shared-and-legacy.md](shared-and-legacy.md). S1's "Update match configurator"
  button (acceptance 6) is human-triggered generation, not the automatic
  feedback loop that stays removed.
- A Job is created manually in the cloud UI (`+ Job`). Automatic ingestion was
  discarded (P-018); do not reintroduce scraping or a bearer-token push path.
- **Cross-Site TA assignment, Job sharing, and Site transfer are out of scope**
  (S1 exclusions). Admin covers the rare cross-Site need.
