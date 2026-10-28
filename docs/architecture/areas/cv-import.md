# CV import — subsystem contract

**Owns:** `backend/cv_importer/`, table `cv_imports`, the import half of
`html/cv-importer.html`, `data/profiles/<username>/.cv_import_tmp/`.
**Decisions:** P-006 with ADR-011 (why durable background work exists at all),
P-009.
**Read alongside:** [workflows.md](workflows.md) (the runner contract this
handler obeys) and [cv-artifacts.md](cv-artifacts.md) (the base/tailored
artifact split this import must never cross).

Durable import of a consultant CV (PDF/DOCX/Markdown) into their **base**
`profile.md`.
Accepting an upload is **202 + a run to poll**, never a held connection: the
predecessor `POST /api/import-cv` ran extraction plus an LLM call on the open
request and is a known 504 path (`docs/evidence/m1-timeout-inventory.md`). It
is retired permanently and must not return.

---

## Contract

### Modules

| File | Responsibility |
|---|---|
| `keys.py` | Leaf module. `ImportKey` = `(cv_import_id, input_hash, attempt)`, formatted as the workflow idempotency key. No imports from the rest of the package, so the handler can register without import-order trouble. |
| `service.py` | **Every** write to `cv_imports`. Upload save/delete, request admission, reconciliation, terminal commits. |
| `handler.py` | The runner's second real handler (`handle_cv_import`), registered from `backend/workflows/__init__.py`. |
| `routes.py` | The three HTTP routes and the `_envelope` projection. |
| `preview.py` | `read_profile_document` (raw, for the marker check) and `load_preview` (form fields, for the editor). |

**Reused unchanged** — the extraction/merge chain, which predates V2:
`cv_parser.py` (text extraction) → `experience_extractor.py` (LLM call 1,
structured extraction) → `profile_merger.py` (LLM call 2, **conditional** —
only when a profile already exists) → `experience_writer.py::write_profile()`.
Read `project_context/consultant-profile.schema.md` first; it is the contract
the LLM must satisfy.

### Routes

| Route | Response |
|---|---|
| `POST /api/profiles/{username}/cv-import` | **202** + a run to poll; **200** when joining an identical in-flight import (`started: false`); **409** for a different upload while one is in flight |
| `GET  /api/profiles/{username}/cv-import/status` | durable progress, no profile content |
| `GET  /api/profiles/{username}/cv-import/preview` | the current `profile.md`, read fresh from disk, as editor form fields |

### Terminal failure display

The editor maps only the terminal import response's exact
`error_message: "unparseable"` to: “Import failed: We couldn’t read the text
in this file. Try exporting your CV again from the original source (Word,
Google Docs, LinkedIn, etc.).” Other reasons retain the existing bounded
`error_message`, then `error_code`, then `unknown reason` fallback. This is
display-only: the backend retains `error_code: "extraction_failed"` and
`error_message: "unparseable"`, without exposing raw extraction context.

### Storage — `cv_imports` (`backend/db/models/cv_import.py`)

Unique `consultant_username` — **one row per consultant**, because an import is
base-profile work, exactly like the base CV. Holds a filename, a content type,
`source_ref` (a path to the transient upload, cleared once deleted), and
non-content counts (`roles_count`, `skills_count`, `open_gaps`,
`blocking_gaps` — the stats `write_profile()` already returns). **No profile
content, and no workflow-run reference** (see G1).

---

## Guards

Everything required to change this subsystem safely is here. History below is
background only.

### G1 — No stored workflow-run reference

`(input_hash, attempt)` is the generation's identity; the run is found by
looking up the idempotency key the row already determines.
*Why:* the same reason `tailored_cvs` has none — a reference can only be
written *after* `enqueue` commits, so a concurrent request that takes the row
lock inside that window reads null, concludes nothing is in flight, and starts
a duplicate. **Do not add the column.**
*Pinned by:* `tests/test_cv_import.py::test_the_cv_import_row_stores_no_reference_to_its_workflow_run`,
`tests/integration/test_cv_import_pg.py::test_one_import_row_per_consultant_is_enforced_by_the_migration`,
`::test_the_status_vocabulary_is_closed_by_the_migration`.

### G2 — Three distinct admission outcomes

Identical upload while one is in flight → **join** it (200, `started: false`).
Different upload while one is in flight → **refuse** (409). Any upload after a
finished attempt → a genuine new attempt (`attempt` increments, so the
idempotency key differs and a real run is created).
*Why:* without the counter the second case collides with the first key and
silently never executes again.
*Pinned by:* `tests/test_cv_import.py::test_a_duplicate_upload_joins_the_run_in_flight_rather_than_duplicating_it`,
`::test_a_different_upload_while_one_is_in_flight_is_refused`,
`::test_retrying_after_a_finished_import_is_a_genuine_new_attempt`,
`tests/integration/test_cv_import_pg.py::test_two_concurrent_identical_first_imports_yield_one_row_and_one_run`,
`::test_two_concurrent_different_uploads_for_one_consultant_refuse_the_second`.

### G3 — The merge is guarded three ways, because it is not naturally idempotent

`write_profile()` **merges** a new extraction into whatever `profile.md` holds.
Two invocations of one attempt (the runner's at-least-once contract) would each
merge the same extraction into an already-merged file. Tailored-CV generation
cannot have this failure mode because it *overwrites* `cv_state` wholesale.

1. **Live sibling** — the write and its success commit happen inside
   `cv_state_service.exclusive_profile_write` (the base CV's compare-and-write
   lock, generalized, under a distinct lock filename so an import and a base-CV
   save never contend needlessly), with the row re-read **under that lock**
   immediately before the write and again before the success commit.
2. **Dead sibling** — `flock` is released the instant its holder's process
   dies, whether or not its `finally` ran, so a crash between the disk write
   and the row commit leaves the file merged, the row `processing`, and nothing
   in the database saying so. `write_profile()` therefore embeds the attempt
   key into the document itself, atomically with the merge
   (`metadata._last_import_key`, temp-file-plus-rename), and the handler reads
   it (`preview.read_profile_document`) **before** calling `write_profile` at
   all. A match means only the row needs finalizing, with stats recomputed by
   `experience_writer.compute_profile_stats` rather than by merging again.
3. **Live process whose finalize commit fails** — `write_profile()` succeeded
   and the marker landed, but `commit_import_success_once` raised. Recording
   that through `_record_failure` would be actively wrong: the content is
   correct, only the bookkeeping is unfinished. That one failure is never
   passed to `_record_failure`; the row is left `processing`, the honest state,
   and that is what makes the case recognizable later.

*Pinned by:* `tests/integration/test_cv_import_pg.py::test_two_concurrent_handler_invocations_write_the_profile_exactly_once`
(real `flock`, two real threads),
`tests/test_cv_import.py::test_a_crash_between_the_profile_write_and_the_success_commit_does_not_duplicate_the_merge`
(performs the crashed process's write directly — a real crash never runs a
`finally`), `::test_a_failed_finalize_commit_does_not_cause_a_later_attempt_to_merge_again`
(through the real runner, so the run genuinely reaches `failed`).
*Test technique that must be preserved:* these assert on a **call counter**
over `write_profile`, not on profile content. An LLM merger instructed to
update an existing role group rather than duplicate it
(`profile_merger.py`'s MERGE RULES) could re-merge without visibly duplicating
anything; relying on that semantic judgement is the weak guarantee these fixes
exist to replace.

### G4 — The marker comparison stays exact, and reconciliation lives one layer up

The handler compares the full `(cv_import_id, input_hash, attempt)`.
**Relaxing it to `(cv_import_id, input_hash)` was tried and reverted**
(S4-P1-R3): content-hash-only matching cannot distinguish "this stuck attempt's
own write landed" from "these bytes match some unrelated, already-**finished**
import", so a legitimate re-upload of the same CV after a **successful** import
reported success while silently skipping the merge into a profile that had
changed since.

Instead `service.request_import` / `_try_reconcile_stuck_attempt` detects the
case a new attempt never can — a `processing` row with **no active run**,
re-requested with the **same** content — and finalizes that attempt under its
own unchanged key: no new attempt, no new run, no merge, stats recomputed from
the document. If the marker does not match, or the document cannot be read,
reconciliation is skipped and a genuine new attempt merges fresh.
*Pinned by:* `tests/test_cv_import.py::test_a_legitimate_reupload_after_success_still_merges_into_the_changed_profile`,
`tests/integration/test_cv_import_pg.py::test_a_reupload_of_the_same_bytes_after_success_is_a_genuine_new_attempt`,
`::test_a_stuck_finalize_commit_is_reconciled_by_the_next_identical_upload`,
`tests/test_cv_import.py::test_an_unreadable_profile_lets_a_stuck_attempt_fall_through_to_a_new_one`.

### G5 — Reconciliation finalizes the row and its run in ONE commit — do not split them

`routes._envelope` looks the run up by the row's own key, so a run left
terminally `failed` — honest when the handler raised, a lie once the marker
proves the effect — would ship beside a `succeeded` import on the POST response
and every later poll. Upgrading a stale `failed` run is explicitly allowed (a
committed effect outranks an earlier wrong guess), but it must not be a
*second* commit: the two records are one polling contract, and a process death
between two commits leaves them durably disagreeing.

So the run transition is **staged** (`workflows_service.stage_succeeded`, the
write without the commit) *before* the row's guarded update, and
`commit_import_success_once`'s single commit publishes both — or its rollback
discards both — with the request's `FOR UPDATE` lock held across the pair.
*Pinned by:* `tests/integration/test_cv_import_pg.py::test_reconciliation_finalizes_the_row_and_its_run_in_one_transaction`,
`::test_two_concurrent_retries_of_one_stuck_attempt_reconcile_it_once`,
`tests/test_cv_import.py::test_a_death_at_the_reconciliation_boundary_cannot_split_the_row_from_its_run`,
`::test_a_reconciled_stuck_attempt_reports_a_consistent_run_and_import_status`.

### G6 — The transient upload is deleted after the handler's last required stage, on every path

Success, failure, and a superseded/already-landed no-op alike. It is **never**
archived into the permanent `uploads/` backstop the way the retired synchronous
importer's uploads were: the handler passes `source_tmp_path=None` to
`write_profile()` so its archiving branch never triggers, and deletes the file
itself in a `finally`.
*Why:* transient source deletion is a non-negotiable floor, tightened past the old
30-day `UPLOAD_RETENTION_DAYS` window.
*Pinned by:* `tests/test_cv_import.py::test_the_uploaded_file_is_deleted_after_a_successful_import`,
`::test_the_uploaded_file_is_deleted_when_extraction_fails`,
`::test_a_redundant_duplicate_uploads_own_file_is_deleted_immediately`,
`::test_the_uploaded_file_is_deleted_even_when_accepting_the_request_fails_unexpectedly`.

### G7 — `source_ref` is cleared only once the delete is confirmed, never in the terminal-status write

`service.finish_upload_cleanup` is the single place that orders this correctly,
used by both the handler's `finally` and stuck-attempt reconciliation;
`delete_upload` **returns whether the file is confirmed absent** rather than
swallowing the failure.
*Why:* the first version cleared the reference in the terminal commit and
deleted afterwards, so a process death in between — or a real `OSError`, as
opposed to "already gone" — left a file with no reference anywhere, forever.
*Pinned by:* `tests/test_cv_import.py::test_a_failed_reference_clear_commit_does_not_strand_the_session_or_the_run`,
`::test_an_unlink_failure_leaves_the_reference_discoverable_and_is_retried_on_next_touch`,
`::test_a_crash_after_the_terminal_commit_leaves_a_recoverable_reference`,
`::test_reconciling_a_stuck_attempt_also_retries_its_own_stranded_upload`.

### G8 — A superseded attempt can neither complete nor fail the current one

Terminal writes are guarded by the attempt key, not merely by row identity.
*Pinned by:* `tests/test_cv_import.py::test_a_superseded_attempt_can_neither_complete_nor_fail_the_current_one`.

### G9 — Authorization is 403, not 404 — the opposite of Jobs and Applications

`require_username_access`: self, the consultant's assigned TA, or an Admin.
*Why the difference:* a **username is not secret** the way an Application's
existence is, so these routes use the same 403 convention as the base CV rather
than the Job/Application 404-secrecy one. Do not "align" them.
Site is never read (invariant 6a).
*Pinned by:* `tests/test_cv_import.py::test_the_related_ta_may_import_for_their_consultant`,
`::test_an_unrelated_ta_is_refused` (**asserts 403**),
`::test_admin_may_import_for_any_consultant`,
`::test_site_is_never_read_on_the_import_path`.

### G10 — Type and size are refused before anything is written to disk

PDF/DOC/DOCX admission is keyed on browser-reported content-type
(`ALLOWED_CONTENT_TYPES`). `.md`/`.markdown` (extension match,
case-insensitive) instead admit by decoding the bytes as UTF-8 — browsers
report markdown content-type inconsistently (`text/markdown`, `text/plain`,
`application/octet-stream`, or empty), so content-type is not a reliable gate
for those two extensions (F-025, V3-P-005). A
decode failure reaches the same `CVImportServiceError` refusal as any other
unsupported type, before `dest.write_bytes` runs. `cv_parser.extract_text`
mirrors this with its own extension-first branch (`file_path.suffix.lower()`),
since mime type is equally unreliable there; the accepted text is read
verbatim and flows through the existing `experience_extractor`/
`profile_merger`/`experience_writer` chain unchanged — no markdown-to-plaintext
conversion, rendering, or execution.
*Pinned by:* `tests/test_cv_import.py::test_an_unsupported_file_type_is_refused_before_anything_is_saved`,
`::test_an_oversized_file_is_refused`,
`::test_a_markdown_upload_is_accepted_regardless_of_content_type`,
`::test_a_markdown_extension_upload_is_accepted`,
`::test_a_non_utf8_file_renamed_to_md_is_refused_before_anything_is_saved`.

### G11 — `status` carries no profile content; `preview` is the content read

The frequent poll must not ship the whole profile every tick — the same split
`tailored_cv/routes.py` makes between `/cv/status` and `/cv`. `preview` reads
`profile.md` fresh from disk, so profile content is never duplicated into a
workflow-adjacent table.
*Pinned by:* `tests/test_cv_import.py::test_the_status_route_never_carries_a_workflow_stage_before_polling`,
`::test_the_preview_route_serves_current_profile_content_after_success`,
`::test_the_preview_route_404s_before_any_import_exists`.

### G12 — The editor entry point is gated as a base-profile write

`↑ Import CV` (`handleAsyncImportFile`) writes base profile material, so it
calls `_baseProfileWritesAllowed()` and must never run from Application or
publication mode (S3-P1-R1 finding 1). The deliberately **new** function name
(not `handleImportFile`) keeps the old-name regressions valid rather than
needing to change.
*Pinned by:* `tests/test_auth_enforcement.py::test_the_synchronous_cv_importer_is_disabled_for_release_cut_a`,
`tests/test_cv_editor_behaviour.py::test_the_editor_has_no_synchronous_import_path_left`.

### G13 — Both LLM calls must pass the workflow timeout override

`cv_extract` and `profile_merge` pass `timeout=WORKFLOW_TIMEOUT_SECONDS` (170s)
to `create_message()`. `backend/llm.py`'s module default (30s) exists for the
synchronous chat routes and would break these.
*Why:* a `profile_merge` against a full existing profile (`max_tokens=8000`)
was measured at ~85s and was cut off by the 30s default, failing the workflow
with `APITimeoutError` with nothing actually wrong. Do not lower
`WORKFLOW_TIMEOUT_SECONDS`, and do not add a workflow LLM call site without the
override.
*Pinned by:* `tests/test_llm.py::test_cv_extraction_uses_the_workflow_timeout_not_the_gateway_default`,
`::test_profile_merge_uses_the_workflow_timeout_not_the_gateway_default`,
`::test_workflow_timeout_is_generously_above_the_gateway_budget`.

### Accepted limitations — do not "fix" these

- **Two windows can leave a file with no reference:** a crash between saving
  the upload and the row's first commit, and a confirmed-failed delete for a
  consultant who never imports again. A scheduled sweep **was built, reviewed,
  and deliberately removed** (S4-P1-R7/R8) as a product decision: the residual
  file costs disk and nothing else, it is cleared unconditionally on that
  consultant's next import or their offboarding erasure, and the sweep's own
  "which reference is still active" logic was where two review rounds of
  findings lived. Full reasoning is in `backend/cv_importer/service.py`'s
  module docstring.
- **There is no dedicated Save control in the editor.** Staged import values
  persist through the existing generic 30-second autosave
  (`saveCurrentState()` → `_persistCvState()`). A reload inside that window
  loses the staged import exactly as it would lose any other unsaved edit.
- **`POST /api/import-cv` is retired permanently** and is not to be
  reintroduced at any address. Slice 4 restored the feature, not the route.

---

## Watch items

**Not reproducible, not blocking. Recorded so a recurrence is recognized
rather than investigated from scratch.**

- **2026-09-02, Release Cut B rehearsal.** One import run produced a single
  hallucinated role/organization in a consultant's `profile.md` that matched no
  real CV content. Suspected cause: a stale job-name variable from an unrelated
  Apply action leaking into a later import's LLM call. A second attempt (new
  job, new upload) did not reproduce it. There is not enough signal to act on.
