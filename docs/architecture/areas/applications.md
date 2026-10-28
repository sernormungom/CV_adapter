# Applications

**Owns:** `backend/applications/`, table `applications`, `html/applications.html`.
**Decisions:** P-008 (Apply is a row, not a verdict field); V3-P-003,
V3-P-011, V3-P-032; invariants 7a, 9.
**Read alongside:** [jobs.md](jobs.md) (shared lock order and the
one-live-decision rule), [cv-artifacts.md](cv-artifacts.md) and
[cv-publish.md](cv-publish.md) (both hang off a terminal transition).

Apply is a first-class row with its own lifecycle, replacing V1's
`application_status` field inside a `verdicts` blob.

---

## Contract

**Four statuses, check-constrained:** `applied` → `submitted` →
(`closed` | `withdrawn`).

**One rule governs every transition** — a status may only move to a strictly
higher `APPLICATION_STATUS_ORDER` rank, and the two terminal values **share**
the top rank, so "nothing leaves a terminal state" falls out of the ordering
rather than needing a second rule.

**One live Application per `(Job, Consultant)`**, enforced by the partial
unique index `uq_application_live_pair`
(`WHERE status IN ('applied','submitted')`). Partial, so a pair whose
Application ended may be applied to again while the historical row stays.
Spelled with **both** `postgresql_where` and `sqlite_where` so the fast gate
builds the same index the deployed database does.

**No CV or score column** — Slice 3 owns the tailored artifact (P-009).

| File | Responsibility |
|---|---|
| `service.py` | Every write. `apply()` inserts and lets the index decide; `transition()` owns the ordering rule; `lock_application` is the row lock other areas take. |
| `routes.py` | create · list · detail · status. |

---

## Guards

### G1 — The partial unique index decides, not a prior SELECT

`apply()` inserts and treats the `IntegrityError` as the **expected retry
path**, not an error path.
*Pinned by:* `tests/integration/test_applications_pg.py::test_two_concurrent_applies_yield_exactly_one_live_application`,
`::test_the_live_pair_index_is_unique_and_partial_in_the_migrated_database`,
`::test_a_raw_second_live_row_is_rejected`,
`::test_terminal_rows_are_excluded_from_the_live_uniqueness`,
`tests/test_applications.py::test_repeating_apply_returns_the_same_application`,
`::test_consultant_self_apply_creates_one_application`,
`::test_a_terminal_pair_may_be_applied_to_again_as_a_new_application`.

### G2 — Status only moves forward, and a terminal status is final

Re-requesting the current status is an idempotent no-op. `applied` may skip
straight to a terminal status. A stale transition must not revive a terminal
Application — which would let a pair hold both a live Application and a Reject.
*Pinned by:* `tests/test_applications.py::test_status_only_moves_forward`,
`::test_re_requesting_the_current_status_is_an_idempotent_no_op`,
`::test_a_terminal_application_accepts_no_further_transition`,
`::test_applied_may_skip_straight_to_a_terminal_status`,
`::test_the_status_vocabulary_is_closed_and_ordered`,
`tests/integration/test_applications_pg.py::test_a_stale_transition_cannot_revive_a_terminal_application`,
`::test_the_database_itself_refuses_a_status_outside_the_pilot_set`.

### G3 — A pair holds one live decision: a live Application *or* a Reject marker

`apply()` refuses a rejected pair (409); `jobs.service.record_review` refuses a
pair with a live Application (409). Different tables, so no unique index can
enforce it — both take the pair's `job_decision_pairs` row lock first
(`jobs.service.lock_pair`, which materializes the row on demand if it does not
exist yet — S1 [jobs.md](jobs.md) G10). A terminal Application frees the pair
to be rejected. `DELETE /api/jobs/{id}/reject` is the documented way out.
*Pinned by:* `tests/integration/test_applications_pg.py::test_a_concurrent_apply_and_reject_cannot_both_land`,
`::test_a_stale_transition_cannot_make_a_pair_both_live_and_rejected`,
`tests/test_applications.py::test_apply_is_refused_while_the_pair_holds_a_reject`,
`::test_reject_is_refused_while_the_pair_holds_a_live_application`,
`::test_withdrawing_the_reject_unblocks_apply`,
`::test_withdrawing_a_reject_is_idempotent_and_authorized`,
`::test_a_terminal_application_frees_the_pair_to_be_rejected`,
`::test_the_service_enforces_both_halves_even_bypassing_the_routes`.

### G4 — Lock order: Job row → pair row → Application row

Same order as `backend/jobs/`, so the two cannot deadlock;
[cv-artifacts.md](cv-artifacts.md) and [cv-publish.md](cv-publish.md) extend
the same chain. **Every write whose decision depends on state another request
can change re-reads that state under its lock** — the first round validated
`application.status` off the caller's already-loaded instance.
*Pinned by:* `tests/integration/test_applications_pg.py::test_apply_cannot_land_after_a_concurrent_close`,
`::test_an_application_in_flight_when_the_job_closes_can_still_be_finished`,
`::test_apply_on_a_concurrently_deleted_job_is_404_not_500`,
`::test_a_transition_on_a_concurrently_erased_application_is_404_not_500`,
`tests/test_applications.py::test_apply_is_refused_on_a_closed_job`,
`::test_closing_the_job_afterwards_does_not_strand_a_live_application`.

### G5 — A confirmed terminal transition publishes its side effects in the same commit

`transition()` disables any active publication and deletes the `TailoredCV`
row **in the same transaction** as the status commit — one commit publishes
both effects or neither. Reachable exactly once per Application: the
transition's own rank check already proves the prior status was non-terminal,
so this is not a second guard that could drift from the first.
`disable_for_application` is imported **locally, inside `transition()`** — a
module-level import would be a genuine circular import, since `cv_publish`
imports `lock_application` from here. Applications stays the lower-level module.
*Pinned by:* the terminal-transition tests in
[cv-publish.md](cv-publish.md) G5 and [cv-artifacts.md](cv-artifacts.md) G9.

### G6 — Apply takes the same three-step authorization as Reject, and the three steps answer differently. Site gates *creating* a live pair, never *reading* an existing Application

One shape for "an action taken for a Consultant on a shared Job" — but **not
one status code.** `backend/applications/routes.py`'s own module docstring
spells this out; keep the two in agreement.

| Step | Refusal |
|---|---|
| `ensure_job_access` — may the caller reach this Job (own Site, or Admin)? | **404** |
| `ensure_consultant_access` — may they act for this Consultant? | **403** |
| `ensure_consultant_operates_job` — does that Consultant belong to this Job's Site (S1; formerly "linked to this Job")? | **403** |

Reading an **existing** Application is separately 404-secret
(`ensure_application_access` → `can_access_application`): an Application's
existence is itself content, so an unrelated TA learns nothing. A Consultant
applying for someone else fails at step 2 with 403. **`can_access_application`
does not read Site** — it is a Consultant-owned-record path under
[authz.md](authz.md) G3, same as base CV/import/chat/publications, and must
stay that way: it also gates the tailored-CV routes
(`/api/applications/{id}/cv…`), which must keep working for a Consultant moved
to a different or inactive Site (ADR-001, invariant 1 — the CV pipeline stays
independent of the matching pipeline). Site enforcement for Applications lives
entirely at *creation* (the table above), not at every later read.
*Pinned by* — the **404** half:
`tests/test_applications.py::test_an_unrelated_ta_cannot_create_list_detail_or_mutate`
(the unrelated TA learns nothing).
The **403** half: `::test_a_consultant_cannot_apply_for_a_different_consultant`,
`::test_a_ta_cannot_apply_for_a_consultant_not_linked_to_the_job`.
Both, across every actor:
`::test_related_ta_applies_for_their_consultant_and_both_can_read_it`,
`::test_admin_reaches_every_application`,
`::test_admin_can_apply_for_a_different_consultant`,
`::test_the_admin_consultant_can_apply_for_themselves`,
`::test_every_application_route_is_authorized_for_every_actor`,
`::test_application_access_does_not_read_site`,
`::test_application_routes_require_authentication`,
`::test_unknown_application_id_shapes_are_404_not_500`.

### G7 — Job projections report **live** Applications only, scoped identically in list and detail

`/api/jobs` list and detail both carry an `applications` field scoped by
`policy.operable_consultant_ids`, so the two views cannot disagree. Per-pair
history is `GET /api/applications?job_id=…`.
*Pinned by:* `tests/test_applications.py::test_job_list_and_detail_report_the_same_applications_for_every_actor`,
`::test_the_job_projection_reports_live_applications_only`,
`::test_the_job_projection_is_scoped_to_the_callers_relationship`,
`::test_an_application_list_shows_only_the_actors_own_relationship`,
`::test_one_consultants_application_is_not_readable_by_another_on_the_same_job`,
`::test_an_actor_reaching_no_consultant_sees_no_applications`,
`::test_the_list_can_be_filtered_by_status_and_job`,
`::test_unknown_status_filter_is_refused`.

### G8 — The row carries no Job body, no CV, and no score

Applying enqueues no workflow run.
*Pinned by:* `tests/test_applications.py::test_the_applications_table_stores_no_cv_or_score_column`,
`::test_the_application_response_carries_no_job_body_and_no_cv`,
`::test_applying_enqueues_no_workflow_run`.

### G9 — No V1 vocabulary, at the database

The `applications.status` check constraint rejects V1's `to_apply` /
`cv_ready` / `link_opened` and the `yes`/`no`/`maybe` verdict words. **Do not
add a compatibility translation.**
*Pinned by:* `tests/test_applications.py::test_no_maybe_and_no_v1_verdict_write_reaches_the_application_path`,
`::test_a_consultant_can_move_their_own_application_and_withdraw_own_reject`.

### UI entry point — `html/applications.html` (V3-P-003, S6)

The focused Applications dashboard, reachable from the role-specific top
banner (Consultant and TA matrices both include it). It calls the same
`GET /api/applications` every actor already reached from the Jobs page's old
embedded card — no broadened result, no new filter parameter server-side. A
`?consultant_id=` on its URL (carried forward by the top-nav links, or a
Consultants-page row link) is resolved through `html/auth-guard.js`'s
`resolveConsultantContext()` against `GET /api/me/consultants` before it does
anything — an unauthorized, stale, or unresolvable value is dropped rather
than used, so it only ever narrows what is *displayed*, client-side, from the
caller's own already-authorized list; it grants nothing the API did not
already return (S6 correction round 1 — the first delivery trusted the raw
query value directly, so an invalid one still rendered a scope banner and a
forwarded nav link as if it were real). Status transitions and the
tailored-CV generate/open/print/poll workflow (Slice 3) are unchanged, just
relocated here; opening a tailored CV still uses the editor's
`?application=<id>` mode (`html/cv-importer.html`,
[cv-artifacts.md](cv-artifacts.md)).

**Read-only Job description (V3-P-032, S21).** A row's Job title is a control
that expands, under the row, the posting's description (line breaks kept) and,
only when `source_url` starts with `http://` or `https://`, an "Open original
posting" link (`target="_blank" rel="noopener noreferrer"`). Nothing else: no
Job actions, no company/location/date. The body is fetched from
`GET /api/jobs/{id}` — authorised by `policy.can_access_job`, so an open Job
answers 404 to a Consultant who is no longer looking, and that message is
simply shown inline. The Application projection still carries no Job body
(G8); this is a separate, already-authorised read, not a widening of it. The
description is untrusted posting text and goes through `esc()`, never in as
HTML. A row whose Job is gone keeps its plain "(job removed)" title with no
control; a re-render (status change, poll) may collapse an open view. This is
also where a Job the Jobs page hid after Apply is read
([jobs.md](jobs.md) "UI entry point").
*Pinned by:* `tests/test_applications_client_behaviour.py`.

### Accepted limitations — do not "fix" these

- Deleting a Job removes its Applications; deleting a Consultant removes theirs
  but leaves the Job; deleting the acting TA keeps the Application and nulls
  authorship (`tests/integration/test_applications_pg.py::test_deleting_a_job_removes_its_applications`,
  `::test_deleting_a_consultant_removes_their_applications_not_the_job`,
  `::test_deleting_the_acting_ta_keeps_the_application_and_nulls_authorship`).
- A client picking a `consultant_username` must read `GET /api/me/consultants`,
  **never** `/api/ta/consultants` — see [authz.md](authz.md) G4.
