# SuperCV and the consultant profile document

**Owns:** `backend/super_cv/`, `html/super-cv.html`, the lossless read and
revision/content-hash-checked save contract for
`data/profiles/<username>/profile.md`, and
`backend/cv_assistant/profile_patcher.py`'s use of that save contract.
**Decision:** V3-P-017 (the SuperCV split and concurrency contract), V3-P-019.
**Read alongside:** [cv-import.md](cv-import.md) for the durable import writer,
[authz.md](authz.md) for Consultant ownership, and
[shared-and-legacy.md](shared-and-legacy.md) G7 for the facts-only boundary.

## Contract

### Lossless profile service — `backend/super_cv/service.py`

`load_profile(username)` parses the fenced YAML through
`backend/cv_assistant/profile_utils.py` and returns the complete profile
document: stable IDs, block-level industries, gaps, preferences, import marker,
verification flags, and all other stored fields. It is intentionally separate
from `backend/profile_reader.py`, whose cleaned projection removes IDs and
suppresses repeated industries for scoring and generation.

The service returns `ProfileArtifact(username, revision, content_hash,
document)`. A reserved `metadata._profile_identity` object stores the revision
and content hash in the same file but is removed from `document` and from the
HTTP profile payload. A profile written before SuperCV has no marker and reads
at revision 0. As with the base CV, the parsed content is authoritative if an
out-of-band writer preserved an older stored hash.

`save_profile(username, document, expected_revision=..., expected_content_hash=None)`
strips any supplied identity, assigns blank IDs with the existing
`assign_new_ids()`, stamps `metadata.updated_at`, increments the revision, and
atomically replaces the Markdown-wrapped YAML. The compare and write happen
under `exclusive_profile_write(username, PROFILE_LOCK_FILENAME)` — the same
lock the CV importer already uses for `profile.md`. `expected_content_hash` is
optional; when a caller supplies it, a mismatch raises the same 409 as a stale
revision (G6). `save_profile` is the only place that sets `updated_at` — every
caller, including the chat patcher, gets it for free rather than stamping it
itself.

### HTTP and client

`GET /api/super-cv/{username}` returns the lossless artifact envelope to the
Consultant, their assigned TA, or an Admin. It uses
`check_consultant_access`; refusal is 403, and Site is not consulted.

`PUT /api/super-cv/{username}` saves the whole document for the same access
set, mapping `ProfileServiceError` through the same `_handle` as the GET. The
body carries the document plus the `revision` and `content_hash` the client
loaded; either mismatch is a 409 (G6), never a merge. `content_hash` is a
required field on this route's request model — an omitted or `null` value is
422, not a save that silently drops the hash side of the comparison (G6).

`GET /html/super-cv.html` renders Identity, Career history, Education,
Miscellaneous education types, Career goals, and import notes. **Identity**
(G5), **career history** (G7), and **education, miscellaneous, and career
goals** (G8) **are all editable** — S18 completes manual editing of every
fact this page renders. Every role group, block and evidence item is fully
editable and reachable, with no bounded `+N more` preview hiding one from an
edit (G7). The page reads
`GET /api/profiles/{username}/cv-import/status` on load and banners a
`processing` import — advisory only, since the content-hash guard (G6) is
what actually protects a save that races one. The page preserves an
authorized `?consultant_id=` context through the shared auth guard. The
Consultant matrix includes a real `SuperCV` destination
(`/html/super-cv.html`); it is never a disabled Base-CV placeholder.

`backend/cv_assistant/profile_patcher.py` loads this artifact and saves the
patched document against the revision *and* content hash it loaded. It no
longer writes `profile.md` directly, and no longer stamps `updated_at` itself.

## Guards

### G1 — SuperCV reads the full document; scoring keeps its cleaned reader

Never replace this service with `backend/profile_reader.py`. Stable IDs and all
unrendered fields are part of the future editor's round-trip contract, while
the cleaned projection is intentionally LLM/scoring signal only. The reserved
profile identity is the sole field removed by this service.

*Pinned by:* `tests/test_super_cv.py::test_lossless_read_keeps_every_stable_id_the_clean_reader_strips`.
The pre-existing cleaned reader remains covered by `tests/test_profile_reader.py`.

### G2 — The marker is stored with the content and cannot be supplied by a caller

`metadata._profile_identity` is removed on every read and stripped again from
every save input. Revision 0 means “no marker yet,” not “empty profile.” The
content hash is computed from the full document without the marker, using the
same canonical JSON function as CV artifacts.

*Pinned by:* `tests/test_super_cv.py::test_a_legacy_profile_without_an_identity_reads_at_revision_zero`,
`::test_a_stale_profile_save_is_refused_and_keeps_the_winner`.

### G3 — Profile compare-and-write uses the importer's lock

The revision check, ID minting, marker update, temporary-file write, and atomic
replace are one operation under `PROFILE_LOCK_FILENAME`. Chat patches use this
save. The durable importer already holds the same lock around its merge, so two
profile writers cannot interleave. Do not introduce another lock filename for a
`profile.md` writer.

*Pinned by:* `tests/integration/test_super_cv_lock.py::test_two_threads_at_the_same_profile_revision_cannot_both_save`,
`tests/integration/test_cv_import_pg.py::test_two_concurrent_handler_invocations_write_the_profile_exactly_once`.

### G4 — Authorization is assignment-based, 403, and Site-free

Self, assigned TA, or Admin is the complete access set. A username is not a
secret resource identifier, so an unrelated actor gets 403 rather than 404.
No Site or score row grants access.

*Pinned by:* `tests/test_auth_enforcement.py::test_self_allowed_others_blocked`,
`tests/test_authz_fixture.py::test_policy_does_not_read_site_for_owned_access`,
`tests/test_super_cv.py::test_an_unrelated_actor_cannot_put_someone_elses_profile`.

### G5 — Identity is editable (P2a), and a save never merges

`full_name`, `email`, `phone`, `location.city`, `location.country`,
`spoken_languages` (add/remove rows, `proficiency` a `<select>` over the
closed `language_proficiency` vocabulary), and `personal_interests` (add/remove
free-text rows reusing the same row shape without `proficiency`/`is_native` —
S18) are the editable identity fields. The
client holds one in-memory document, loaded once and mutated only by the
identity handlers and by gap Dismiss; Save and Discard are explicit controls
— there is no autosave, and Discard reverts the whole document to the last
loaded/saved snapshot, not just the displayed text. A 409 (G6) shows the
server's message and offers reload rather than retrying with the same stale
revision/hash. Year-only dates, overlapping blocks, and holes between
consecutive role groups stay computed warnings, never stored. Open
`data_conflict` gaps show their own question next to the matched Identity
field and can be dismissed, which sets the gap's `status` to **`declined`**
— never `resolved`, since SuperCV cannot verify what it did not collect
(V3-P-017 decision 5); every other open stored gap still appears as one plain
line in collapsed “Notes from import” and is not editable here. Education,
miscellaneous, and career goals are editable too — see G8.

*Pinned by:* `tests/test_super_cv.py::test_untouched_facts_survive_an_identity_only_save`,
`tests/test_super_cv_client_behaviour.py` (identity edit, Discard, and the
409 path), `tests/test_static_pages.py::test_the_nav_matrix_pins_the_consultant_supercv_destination`.

### G6 — A save checks content hash as well as revision, closing the importer-drift hole

`save_profile`'s optional `expected_content_hash`, when a caller supplies it,
is compared alongside `expected_revision` inside the same lock; either
mismatch raises the same 409. This exists because the CV importer writes
`profile.md` under this lock (G3) but never mints or bumps
`_profile_identity` (Accepted limitations, below) — so a completed import is
invisible to the revision check alone, while `load_profile` already returns
the document's *actual* hash regardless of what the stored marker says (G2).
`html/super-cv.html` always sends the hash it loaded; `profile_patcher.py`
passes its loaded artifact's hash through too, so the chat patch gets the
same protection without duplicating the check. `save_profile` keeps
`expected_content_hash` optional so a hypothetical future non-route caller
that only checks revision is not forced to compute a hash, but
`PUT /api/super-cv/{username}`'s own request model requires it: the route is
the only caller with no way to independently know the loaded document is
current, so it cannot be allowed to opt out of this guard the way an
optional field would let it.

*Pinned by:* `tests/test_super_cv.py::test_a_revision_match_with_a_mismatched_content_hash_is_refused`,
`tests/test_super_cv.py::test_the_put_route_refuses_an_omitted_content_hash`,
`tests/test_super_cv.py::test_the_put_route_refuses_a_null_content_hash_over_importer_drift`.

### G7 — Career history (role groups, blocks, evidence) is fully editable (P2b)

The whole tree is one client-side edit against the same `profileDoc`, saved
through the same `PUT` as Identity — no per-operation endpoint, no schema
validator. **Role group:** `organization.name`, `organization.country_code`,
`organization.industries` (chip editor), `display_role_title`,
`started`/`ended` (month + year `<select>`s, a **Present** checkbox on
`ended`), `employment_type` (closed-vocabulary `<select>`), `summary`.
**Block:** `role_title`, `client.name` and `client.department`/
`country_code` **only where the loaded block already has a `client` object**
— no "add a client" affordance — `started`/`ended`, `block_type` and
`seniority` (closed-vocabulary `<select>`s). **Six separate chip editors**
— `domains`, `industries`, `languages`, `tools`, `processes_standards`,
`verification_validation` — each write only their own field; the display-only
`tagsFor()`/bounded-preview helpers P1 used for this section are gone, not
reused, per the accepted-limitations note below. `domains`, `industries` and
`processes_standards` are the schema's *extendable* vocabularies (§3): their
chip input carries a `<datalist>` suggesting the seed vocabulary plus every
value already used anywhere else in the loaded document; the other three
fields have no controlled vocabulary and get no suggestions. Every
`chipEditorHtml` field — these six, organization industries, and career
goals' `target_roles`/`target_industries` — is compact by default (S19):
chips plus a small `+ Add` control, no permanently-visible input. `+ Add`
swaps that control for a focused input (keeping the datalist) plus Accept/
Cancel via `wireChipAdder`; Enter mirrors Accept, Escape mirrors Cancel.
Accept adds the trimmed value — blank or already-present adds nothing — and
either way closes the adder back to compact; Cancel/Escape close it with no
change. An empty field renders no "None added." placeholder, only its label
and `+ Add`. Save and Discard read `profileDoc`, never a chip input's raw DOM
value, so text typed but not accepted is never saved (same rule G5 states for
Identity). **Evidence item:** `type` as the five closed values in sentence-starter form ("I was
responsible for…", …), `text` with a soft ≤60-word counter that never blocks
Save, and `quantification` shown only when `type` is `impact`.

**Structural edits** (add/delete a role group, a block, an evidence item)
mutate `profileDoc` directly and re-render; deleting a role group's last
block deletes the role group with it, and the control that would remove a
block's last evidence item is disabled rather than left to fail at Save.
Deleting a whole role group is the one operation here that asks for
confirmation first (`window.confirm`), since it can carry many blocks and
evidence items. Editing a block's `role_title`, `client`, dates, `block_type`,
`seniority`, any of the six chip fields, or its evidence clears that block's
`verified` flag (schema §2.5.1): the flag records that the consultant
confirmed *that* content, so changed content is unconfirmed again. The
comparison is against the pristine document the page loaded
(`loadedSnapshot`), not against the previous keystroke, so an edit undone
back to the loaded value leaves `verified` alone rather than clearing it for
a change that ended up a no-op.

A role group, block or evidence item is keyed by its schema-stable ID
(`role_group_id`/`block_id`/`evidence_id`) embedded directly in the DOM
element ids under it (e.g. `blkTitle_<block_id>`) — not by array position,
which a structural add/delete anywhere in the tree would invalidate for
everything below it. A not-yet-saved object's ID is blank (schema §2.6:
minted server-side by `assign_new_ids()` on Save); the client mints its own
synthetic per-session key for such an object purely to keep its DOM ids
unique before that save, tracked in two page-local maps
(`objectKeys`/`keyRegistry`) that are never part of `profileDoc` and never
reach the request body. A date, a Present toggle, or a vocabulary `<select>`
re-syncs the whole document and re-renders immediately, because a computed
warning (a year-only date, an overlap within one role group, a hole between
consecutive role groups, all G5's computed-warning rule extended here) or the
quantification block's visibility can change from it; every other field —
free text — is read back into `profileDoc` only at the next such re-render or
at Save, the same idiom G5 established for Identity. `careerHoleWarnings`
sorts role groups by `started` and measures each gap from the **latest end
so far**, not the previous entry in sort order (S19) — a role group nested
inside (or overlapping) an earlier, longer one no longer manufactures a gap
against the role group that follows it; a genuine gap is still measured
against the true latest end. The one display-only
exception is the evidence word counter: its `<textarea>` carries its own
`input` listener (`updateEvidenceWordMeta`) that updates the advisory count
and over-60 message directly in the DOM on every keystroke, without a full
`syncProfileIntoDoc()`/`renderCareer()` pass — that pass would rebuild the
textarea out from under an in-progress keystroke and lose focus/cursor
position. The text itself still reaches `profileDoc` only at the next full
re-render or at Save, same as any other free-text field.

*Pinned by:* `tests/test_super_cv_client_behaviour.py` (block field edit
under the right `block_id`, evidence add/delete, a chip landing in one field
only, the `verified` clear, the role-group delete confirmation, the evidence
word counter updating live while typing, a compact chip adder opened/typed/
Enter-accepted, one opened/Escape-cancelled with no change, an open adder's
text absent from a save payload, and the nested-role career-gap regression
case).

### G8 — Education, miscellaneous, and career goals are editable (P2c)

Completes manual editing of every fact SuperCV renders (V3-P-017 decisions 3
and 7). **Education and miscellaneous entries share one card**, keyed by the
schema's `education_id` embedded in each field's DOM id — the same G7 keying
convention, including a synthetic per-session key for a not-yet-saved entry:
`type` (closed `education_type` `<select>`), `institution`, `qualification`,
`field_of_study`, `location.city`/`location.country_code`, `started`/`ended`
(`dateFieldHtml`, `ended` allows Present), `expires_at` (no Present), and
`description`. **Which section an entry renders under is derived from its
own `type` on every render, never stored separately** — `degree` under
Education, everything else under Miscellaneous (the split S15 shipped,
unchanged); the `type` select triggers the same immediate re-sync-and-render
as a career-history date or vocabulary `<select>` (G7), so re-typing an entry
reclassifies it into the right section on the spot, not only after Save.
Each section has its own `+ Add` control, seeding a new entry's `type` to
`degree` (Education) or `course` (Miscellaneous) with a blank `education_id`
for `assign_new_ids()` to mint; delete takes no confirmation, since an
education entry has no children unlike the role group G7 guards with one.

**`personal_interests`** is editable in G5. **Career goals** is a real form
over `preferences`, replacing the generic key/value dump: `target_roles` is
a free-text chip editor, `target_industries` a chip editor over the
extendable `industry` vocabulary — `suggestionsFor('industries', ...)` (G7)
now also seeds suggestions from any `target_industries` value already in the
document — `seniority_target` a closed-vocabulary `<select>`, and `notes`
free text. Both chip editors reuse `chipEditorHtml` (G7) directly against
`profileDoc.preferences`, a singleton — unlike a block or role-group chip
field there is no stable ID to look up by, so add/remove mutate `preferences`
in place rather than going through `byKey`. `avoid_industries` is never read
by this editor, so it round-trips through the one whole-document save
untouched — see Accepted limitations. S19 deleted the schema's other four
`preferences` fields (`availability`, `work_mode`, `employment_types`,
`location_preference`) outright rather than leaving them permanently
unrendered — see `project_context/consultant-profile.schema.md` §2.3 and
V3-P-019, which resolved the preferences-notes field-deletion experiment
([evidence](../../evidence/v3-preferences-notes-experiment-2026-09-24.md)).

`needs_review` on an education entry clears on save only when the entry's
content actually changed from the pristine `loadedSnapshot` (the same
comparison G7's `verified` clear uses) **and** the entry is now complete: no
empty `institution`/`qualification`, and no `started`/`ended`/`expires_at`
still showing the page's "month needed" note. An entry edited but still
incomplete keeps the flag — this editor only ever clears the flag, never
sets one; schema §6 needs it alive for a fact that is genuinely still
uncertain. The content comparison normalizes `location` before comparing it:
the read-back always materializes `{city, country_code}` on `item.location`
even when the snapshot never had one, so a blank-vs-absent `location` would
otherwise register as a change on every save of an untouched entry and wrongly
clear its flag. `normalizeEducationFieldValue` treats a `location` with both
values blank the same as absent for this comparison only.

*Pinned by:* `tests/test_super_cv_client_behaviour.py` (an education field
edit under the right `education_id`, adding an entry in each section with
the expected default `type`, re-typing an entry to `degree` moving it to
Education, a `needs_review` clear, a retained flag on an incomplete edit, a
retained flag on a complete-but-untouched entry lacking `location`, a career
goal reaching `preferences` without disturbing the unrendered fields, a
`personal_interests` row added and removed).

### Accepted limitations — do not “fix” these

- `avoid_industries` is not rendered — the one `preferences` field left
  unread after S19 (V3-P-019) deleted the schema's other four unrendered
  fields (`availability`, `work_mode`, `employment_types`,
  `location_preference`) rather than leaving them as a permanent
  round-trip-only passthrough.
- No cross-employer "move a block" picker and no drag-reordering of
  positions or evidence (V3-P-017 decisions 8/9). Re-filing a wrongly-filed
  block is a delete-and-re-add; ordering is the CV generator's job at
  generation time (schema §6), not a stored fact.
- A block's `collaboration` object is not editable — rare, nothing gaps on
  it, and a whole-document save round-trips it untouched regardless.
- A block's `client` sub-fields (`department`, `country_code`) are only
  editable where the loaded block already has them; there is no "add a
  client" affordance for a block that has none.
- The CV importer holds the correct lock but does not mint or bump
  `_profile_identity` — G6's content-hash check is the mitigation, not a fix
  to the importer; changing `experience_writer.py` itself stays out of scope.
- There is no new schema validator. The current chat patch continues its
  structured-tool construction; closed-vocabulary `<select>`s and structural
  array edits (`spoken_languages`, and now the whole career-history tree) are
  the validate-by-construction pattern (V3-P-017 decision 3), not a general
  validator — a select left at its "— not set —" option or a date left blank
  saves as an empty string, not a blocked save or an omitted field the way a
  producer LLM would flag it (schema §4.4 does not apply to this manual
  editor).
- `identity.public_links`, `identity.work_authorization`, `style.md`, and the
  photo are not rendered or edited here.
- A profile save does not refresh scores or matching configuration.
- The in-flight-import banner is advisory only; it does not block Save. The
  content-hash guard (G6) is what actually refuses a save that races a
  completed import.
