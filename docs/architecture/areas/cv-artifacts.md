# CV artifacts — base CV, tailored CV, and generation

**Owns:** `backend/cv_state/`, `backend/tailored_cv/`, `backend/cv_pipeline/`,
`html/cv-importer.html` (the editor), `data/profiles/<u>/cv_state.json`, table
`tailored_cvs`.
**Decisions:** P-009 (the two-artifact split), P-006 with ADR-011 (durable
generation).
**Read alongside:** [workflows.md](workflows.md) (the generation handler's
contract), [applications.md](applications.md) (lock order, terminal
transitions), [cv-publish.md](cv-publish.md) (what a publication snapshots).

A Consultant's **base CV** and each Application's **tailored CV** are separate
artifacts with separate storage, separate revisions, and separate editor modes.

---

## Contract

### Base CV — `backend/cv_state/`

`GET`/`PUT /api/cv/base/{username}`. Stored at
`data/profiles/<username>/cv_state.json`, one of the four canonical seed
artifacts. Carries its own `revision` +
`content_hash` under a reserved `_cv_identity` key **inside the same file** —
one store, nothing to drift.

Replaces V1's unversioned `GET/POST /api/profiles/{username}/cv-state`, which
lived in `backend/main.py` and is retired.

### Tailored CV — `backend/tailored_cv/`

| File | Responsibility |
|---|---|
| `service.py` | Every write. `request_generation` freezes inputs; `commit_generated_state_once` is the handler's atomic effect; `save_state` owns the three refusals. |
| `keys.py` | Leaf module. `GenerationKey` = `(tailored_cv_id, input_hash, generation_attempt)` → the workflow idempotency key. |
| `handler.py` | The runner's first real handler, registered from `backend/workflows/__init__.py`. |
| `state_builder.py` | Generated content + the base CV's presentation → the editor's `cv_state`. The browser used to do this; a background generation has no browser. |
| `routes.py` | `POST …/cv/generate` (**202**, or **200** when an identical generation is in flight), `GET …/cv`, `PUT …/cv`, `GET …/cv/status`. |

**Table `tailored_cvs`:** `application_id` is **unique** — one tailored CV per
Application, decided by the database rather than a prior SELECT. `status`
(`generating`/`ready`/`failed`) describes the *last generation*, not whether
content exists — `revision > 0` is that. **No column pointing at a workflow
run** (G5).

### Generation machinery — `backend/cv_pipeline/`, unchanged and reused

`context_assembler.py` (profile + style + job description → LLM context) →
`cv_content_generator.py` (the LLM call) → `form_data.py`'s
`cv_content_to_form_data()` (**the live render path**). The methodology is
`cv_content_prompt.md`, loaded as the system prompt — **edit that file to
change generation behaviour**, it is the single source of truth. Read
`project_context/cv-content.schema.md` and
`project_context/consultant-style.design.md` first.

`cv_renderer.py` + `backend/templates/cv.html.jinja2` is a server-side Jinja2
renderer **not wired into any route** — kept schema-consistent as a candidate
for a future export path. Do not assume it is live.

**`POST /api/generate-cv` is retired.** It generated synchronously into
whatever CV the editor held — the base overwrite P-009 forbids — over a
connection `docs/evidence/m1-timeout-inventory.md` records as routinely
exceeding the budget on a full CV.

### The consultant/TA-facing entry point — `html/applications.html`

Per Application, offers **Tailor CV** (first generation) / **Regenerate** (or
"Retry generation" after a `failed` status) / **Open CV** / **Download PDF**,
and polls the durable status the same way the editor does. Download PDF opens
the CV editor with `&print=1` — straight into the browser's print dialog, the
same path the hiring-manager viewer's own Download PDF button uses (see
[cv-publish.md](cv-publish.md)). This workflow lived embedded in
`html/jobs.html` through V3 packet S5; S6 (V3-P-003) moved it to its own
focused Applications dashboard — same API calls, same editor mode, new page.

---

## Guards

### G1 — Tailoring never writes base state (invariant 7b, P-009)

`backend/cv_state/service.py` is the **only** writer of
`data/profiles/<u>/cv_state.json`; `backend/tailored_cv/service.py` is the only
writer of `tailored_cvs`. The generation handler reads its frozen
`input_snapshot` and opens no file at all, so it has nothing to write back
with. This is a correctness boundary, not UI polish.
*Pinned by:* `tests/test_tailored_cv.py::test_generating_and_editing_a_tailored_cv_never_moves_the_base_cv`
(must stay in the **fast** gate),
`::test_the_tailored_cv_takes_presentation_from_the_base_without_writing_it`.

### G2 — The base CV's identity lives inside the file and cannot be seen or forged

`_cv_identity` is stripped from every read, so a client can neither observe nor
supply it. A file written before this module existed reads as revision 0 and
gets an identity on its first save. A file edited outside the app reports its
actual content hash. The hash ignores key order but not content.
*Pinned by:* `tests/test_cv_state.py::test_the_identity_never_leaks_into_the_state_a_client_sees`,
`::test_a_client_cannot_forge_an_identity_by_sending_one_as_content`,
`::test_a_file_written_before_this_module_existed_reads_at_revision_zero`,
`::test_a_file_edited_outside_the_app_reports_its_actual_content_hash`,
`::test_the_hash_ignores_key_order_but_not_content`,
`::test_a_consultant_with_no_saved_cv_reads_as_an_empty_revision_zero`,
`::test_saving_bumps_the_revision_and_records_the_content_hash`.

### G3 — The revision check and the write are ONE operation, serialized per consultant

`_exclusive_base_write` — `flock` on a `.cv_state.lock` file beside the state
file, plus a per-process layer.

*Why this is not belt-and-braces:* the atomic rename makes a **torn file**
impossible and nothing more. Two writers that both loaded revision 3 both
passed the check and both wrote revision 4, and **both were told they had
saved** while one CV was destroyed (S3-P1-R1 finding 2). The profile volume is
EFS, so the contenders are different ECS tasks and the lock must live on that
shared volume; the per-process layer exists because Linux implements `flock()`
over NFS as a per-process POSIX lock. The lock file sits **beside** the state
file rather than being it, because a lock on a file then replaced by rename is
a lock on an unlinked inode.
*Pinned by:* `tests/test_cv_state.py::test_two_writers_at_the_same_revision_cannot_both_win`,
`::test_a_write_based_on_a_stale_revision_is_refused`,
`::test_a_partial_write_cannot_destroy_the_stored_cv`,
`::test_a_stale_write_over_http_is_409_and_keeps_the_winner`,
`::test_a_write_must_state_a_revision`,
`::test_unreadable_stored_state_is_a_409_not_a_500`.

### G4 — A failed regeneration keeps the previous document

`status` describes the last generation; content existence is `revision > 0`. A
failed regeneration therefore keeps the previous `cv_state`, `revision`, and
`content_hash` — a transient LLM error must not cost the consultant a working
document. Only the error *kind* is recorded, never model text.
*Pinned by:* `tests/test_tailored_cv.py::test_a_failed_generation_keeps_the_previous_cv_and_says_what_failed`,
`::test_a_rejected_generation_records_only_the_error_kind`,
`tests/integration/test_tailored_cv_pg.py::test_one_tailored_cv_per_application_is_enforced_by_the_migration`,
`::test_a_raw_second_artifact_for_one_application_is_rejected`,
`::test_the_status_vocabulary_is_closed_in_the_migrated_database`.

### G5 — No stored workflow-run reference — this is load-bearing

The run is found by looking up the idempotency key. An earlier version stored a
`generation_run_id`, which could only be written **after** `enqueue` committed;
a second concurrent request that took the artifact's row lock inside that
window read a null reference, concluded nothing was in flight, and **started a
duplicate generation**. **Do not re-add the column.**
*Pinned by:* `tests/integration/test_tailored_cv_pg.py::test_two_concurrent_generate_requests_yield_one_artifact`
(the test that caught it), `::test_the_artifact_stores_no_reference_to_its_workflow_run`,
`tests/test_tailored_cv.py::test_the_artifact_stores_no_reference_to_its_workflow_run`.

### G6 — The handler reads the input snapshot and nothing else

`input_snapshot` holds the consultant's profile YAML, style rules, base CV
state (presentation + photo), and the job posting as they stood when a human
asked. No file, no Job row. Editing `profile.md` mid-flight cannot change an
approved generation, and a re-queued invocation regenerates from the same
inputs. The snapshot deliberately carries **no timestamp**: it is hashed, and a
clock reading would make every double-click a different generation.
*Pinned by:* `tests/test_tailored_cv.py::test_generation_uses_the_snapshot_not_whatever_the_disk_holds_later`,
`::test_deleting_the_profile_after_enqueue_does_not_break_the_generation`,
`::test_the_snapshot_carries_no_timestamp`,
`::test_the_generation_key_round_trips_and_rejects_anything_else`.

Building the snapshot refuses, with a 409 and nothing enqueued, a consultant
with no `profile.md` (`NO_PROFILE_MESSAGE`, "Import a CV first") and —
separately — one whose base CV state has no stored `photoSrc`
(`NO_PHOTO_MESSAGE`, "Upload a photo first"; an inline `data:` URL does not
count, V3-P-023). `state_builder`'s blank-photo fallback stays only as a
defensive default. The client shows either message through
`generateTailoredCv`'s existing error path.
*Pinned by:* `::test_a_consultant_with_no_profile_gets_an_honest_409`,
`::test_a_consultant_with_no_photo_gets_an_honest_409`,
`::test_an_inline_data_url_photo_is_not_a_photo`.

### G7 — Retry means two different things

Re-requesting a generation **still in flight** returns the existing run and
starts nothing (the double-click, and the POST retried after the 60-second
edge). Re-requesting one whose run has **finished** is a deliberate new
attempt: `generation_attempt` increments, so the key differs and a real run is
created. Without that counter the second case would collide with the first key
and silently never execute again.
*Pinned by:* `tests/test_tailored_cv.py::test_a_repeated_request_joins_the_run_in_flight_and_starts_nothing`,
`::test_a_retry_after_a_finished_run_is_a_real_new_attempt`,
`::test_a_re_queued_run_does_not_produce_a_second_document`,
`::test_a_superseded_attempt_cannot_overwrite_the_current_one`,
`::test_generation_is_accepted_as_a_row_and_a_run_not_a_held_connection`,
`tests/integration/test_tailored_cv_pg.py::test_two_concurrent_invocations_commit_the_effect_exactly_once`,
`::test_a_superseded_attempt_changes_nothing_and_reports_it`.

**Not deduplicated:** the LLM call itself (P-006 defers the leases that would
allow it). A lock loss can cost a second model call; the artifact, its revision
and its hash still move exactly once.

### G8 — `save_state` owns exactly three refusals

Stale revision · cross-artifact (an edit addressed at another Application's CV)
· mid-generation (refused rather than silently lost). A save that passes all
three but carries content identical to what is stored is a no-op (G15).
*Pinned by:* `tests/test_tailored_cv.py::test_editing_bumps_the_revision_and_a_stale_edit_is_refused`,
`::test_an_edit_addressed_at_another_applications_cv_is_refused`,
`::test_an_edit_during_a_generation_is_refused_rather_than_silently_lost`.

### G9 — Lock order: Job row → pair row → Application row → tailored CV row

Same order as `backend/jobs/` and `backend/applications/`, so nothing here can
deadlock against them. `cv_publish` extends the same chain.
A terminal Application cannot start a generation, and reaching a terminal
status deletes the tailored CV.
*Pinned by:* `tests/integration/test_tailored_cv_pg.py::test_a_generation_serializes_with_the_applications_own_row_lock`,
`::test_a_close_cannot_land_between_the_artifact_and_the_enqueue`,
`::test_deleting_an_application_takes_its_tailored_cv_with_it`,
`tests/test_tailored_cv.py::test_a_terminal_application_cannot_start_a_generation_and_deletes_its_cv`,
`::test_a_crash_before_the_enqueue_does_not_strand_the_application`.

### G10 — Two opposite authorization conventions live in this area. Do not align them

- **Tailored CV → 404, not 403** (`policy.ensure_application_access`): an
  Application's existence is content, and so is the fact that a CV was tailored
  for it.
- **Base CV → 403** (`require_username_access`): a username is not secret the
  way an Application's existence is. Same convention as CV import.

No Site is read on either path (invariant 6a).
*Pinned by:* `tests/test_tailored_cv.py::test_an_unrelated_ta_gets_404_and_learns_nothing`,
`::test_a_consultant_reaches_no_other_consultants_tailored_cv`,
`::test_the_related_ta_and_the_admin_reach_every_route`,
`::test_the_tailored_cv_path_reads_no_site`,
`::test_a_missing_tailored_cv_is_404_with_an_actionable_reason`,
`tests/test_cv_state.py::test_an_unrelated_ta_reaches_no_base_cv` (**asserts
403**), `::test_a_related_ta_may_read_and_write_a_consultants_base_cv`,
`::test_an_admin_reaches_any_base_cv`.

### G11 — Status and metadata never carry the document

*Pinned by:* `tests/test_tailored_cv.py::test_the_metadata_projection_never_carries_cv_content`,
`::test_the_status_route_never_ships_the_document`,
`::test_the_generation_run_row_holds_references_not_content`,
`::test_every_tailored_cv_route_maps_a_service_error_rather_than_500`.

### G12 — In the editor, the mode is claimed before the first `await`, and one writer switches on it

`html/cv-importer.html` decides the mode once from the URL — `?application=<id>`
→ tailored, `?pub=<token>` → a published snapshot, neither → base — and
**claims it synchronously before either loader's first `await`**. Deciding once
the artifact had loaded left a two-round-trip window in which the page was
visibly an Application editor while `_cvMode` still said `base` (S3-P1-R1
finding 1). Leaving a mode is correspondingly explicit: each load failure
resets it before falling back to `loadProfile()`. Every CV-state save goes
through the single `_persistCvState()` writer.
*Why:* a per-call-site URL choice is one forgotten branch away from restoring
the V1 behaviour where generating for a position overwrote the consultant's own
CV.
*Pinned by:* `tests/test_cv_editor_behaviour.py::test_the_mode_is_claimed_before_the_first_await`,
`::test_a_cv_state_save_goes_to_the_artifact_its_mode_names`,
`::test_the_probe_cannot_set_the_editors_mode_directly`.

### G13 — Writes to base *profile* material go through one named gate

`_baseProfileWritesAllowed()` gates the photo upload
(`/api/profiles/{u}/photo`), the chat profile patch
(`/api/chat/profile-patch`), and the async CV import (`handleAsyncImportFile`).
These write material **no** Application or publication owns a copy of, so they
are not CV-state saves and the one-writer switch never saw them. Each was
checking the mode for itself and two of three answers were wrong — the photo
refused publication mode only, the profile patch refused nothing. Editing one
Application could therefore replace the photo and profile facts every other CV
renders from, while every artifact's revision and hash stayed put.
**Any new action that writes base profile material must call that gate rather
than re-deriving the rule.**
*Pinned by:* `tests/test_cv_editor_behaviour.py::test_the_base_profile_write_gate_exists_as_one_named_function`,
`::test_base_mode_really_does_write_base_profile_material`,
`::test_application_mode_writes_no_base_profile_material`,
`::test_the_durable_async_importer_is_wired_up`.
*Test technique:* these execute the real page under a stub DOM and assert on
the URLs it actually requested, including during the pre-await window, with a
base-mode control so the absences are evidence rather than a broken stub
(`::test_the_stub_element_has_no_per_element_behaviour`,
`::test_the_stub_stays_small_enough_to_read`).

### G14 — Body-text presentation is fixed and shared, not per-consultant or per-section (S10)

The editor no longer offers per-section text-style controls (the four
`.style-box` panels for Profile/Skills/Experience/Education, and the "Generate
CV" header control/modal that only served picking an Application to tailor —
both removed entirely). `html/cv-render.js`'s `ts()` returns one fixed style
constant (`'Source Sans 3',sans-serif`, 10px, `#444444`, weight 400) used for
all four sections' body text, uniformly across every consultant. An existing
`cv_state` document that still carries the old per-section style keys
(`pfFont`/`skFont`/`exFont`/`edFont` and their `Size`/`Color`/`Weight`
equivalents) simply has them ignored on read — no migration purges them, they
are inert leftovers. This is a genuine, approved reset of any previously-saved
per-section customization, not a preserved value with a hidden control.
Structural CV typography (name/role labels, bold assignment/role lines,
section headers, bullets) is separate, fixed CSS in `html/cv-render.css` and
was already unaffected by these controls. Per-entry inline rich-text
formatting (the `rte-btn` toolbar, `Ctrl+B`/`Ctrl+I`) is unaffected — it
remains the only mechanism for emphasis within an entry.
*Pinned by:* `tests/test_cv_editor_behaviour.py`.

### G15 — A content-identical save is a no-op, decided after the refusals (S22)

The editor autosaves every 30 seconds whether or not anything changed, so the
revision used to climb with the time a tab stayed open (V3-P-022). Now
`save_base` and `save_state` compare the incoming content's `content_hash`
against the stored one and, when equal, **write nothing** and hand back the
stored artifact unchanged: same revision, hash and `updated_at`. For a
tailored CV, `content_changed_at` and `updated_by_id` don't move either.
The rules (V3-P-033):

- **After the refusals, inside the same critical section** (the base CV's
  `_exclusive_base_write`, the tailored CV's row lock). Order: wrong-artifact →
  mid-generation → stale revision → no-op. A stale or mid-generation save of
  identical content is still refused.
- **Compared against:** the base CV's hash as `load_base` recomputes it from
  content (never missing); the tailored row's stored `content_hash`, where a
  missing one counts as changed and writes as before.
- **The tailored no-op still ends its transaction** (`db.rollback()`; nothing
  was assigned), so the `FOR UPDATE` lock doesn't outlive the decision.
- **The editor must send stable content.** `captureFormState()` saves `photoSrc`
  without its query string. The only query the editor ever puts there is the
  `?t=` cache-buster, minted fresh on every page load for the `<img>`, and
  saving it made the first autosave after each load look like an edit. (A
  photo reference whose query is meaningful, such as a signed URL, is not
  supported: `restoreFormState` already drops the query on load, and has
  since before S22. No code path produces one.)
- **The no-op reply is the snapshot taken under the lock.** `save_state`
  detaches the row (`db.expunge`) before rolling back, so the route's reply
  never re-reads it after the lock is released. A re-read would turn a
  concurrent delete into a 500. Worse, it would hand the client a concurrent
  writer's revision, and the client's next autosave of its older content
  would then pass the stale check.
- A seeded base CV with no stored identity stays without one on an identical
  first save (it reads as revision 0). No special code.

The first editor save after a generation may still bump once: the editor's
captured shape (defaults filled, keys it does not edit dropped) is not the
generator's. That's expected. After it, identical autosaves are no-ops.
*Pinned by:* `tests/test_cv_state.py::test_an_identical_autosave_writes_nothing_and_a_real_edit_still_does`,
`::test_an_identical_save_with_a_stale_revision_is_still_refused`,
`::test_an_identical_save_over_http_returns_the_stored_artifact`,
`::test_identical_content_with_a_forged_identity_is_a_no_op`,
`tests/test_tailored_cv.py::test_an_identical_autosave_moves_nothing_and_a_real_edit_still_does`,
`::test_an_identical_save_with_a_stale_revision_is_still_refused`,
`::test_an_identical_save_keeps_the_earlier_refusals`,
`::test_a_missing_stored_hash_counts_as_changed`,
`::test_a_no_op_save_ends_its_transaction`,
`::test_a_no_op_reply_carries_the_values_read_under_the_lock`,
`::test_a_no_op_reply_survives_the_row_being_deleted_meanwhile`,
`tests/integration/test_tailored_cv_pg.py::test_an_identical_save_writes_nothing_and_releases_the_row_lock`,
`tests/test_cv_editor_behaviour.py::test_a_saved_photo_reference_carries_no_per_load_cache_buster`.

### Accepted limitations — do not "fix" these

- `cv_renderer.py` is intentionally unwired. Do not route to it without a
  decision about the export path.
- A consultant with no profile gets an honest **409**, not an empty generation
  (`tests/test_tailored_cv.py::test_a_consultant_with_no_profile_gets_an_honest_409`).
- `&print=1` opens the editor straight into the browser's print dialog — that
  *is* the PDF path, the same one the hiring-manager viewer uses. There is no
  server-side PDF renderer.

### Known defect — open, not an accepted limitation

- **Known defect, deliberately left for later (owner decision 2026-10-01):**
  the tailored-CV save *write* path ends `db.commit(); db.refresh(tailored)`
  (`backend/tailored_cv/service.py`, `save_state`). The commit releases the
  `FOR UPDATE` lock and the refresh re-reads the row, so an edit committed by
  another tab in that gap is returned as this client's revision; that client's
  next autosave of older content then passes the stale check and overwrites the
  edit (lost update). A delete in the gap raises a 500. The no-op path already
  replies with the snapshot taken under the lock (G15); the write path needs
  the same — build the reply before committing. Prove it the way
  `tests/test_tailored_cv.py::test_a_no_op_reply_carries_the_values_read_under_the_lock`
  does.
