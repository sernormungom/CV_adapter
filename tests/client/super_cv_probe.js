/*
 * Runs the SuperCV page's own inline script under a stub DOM and reports what
 * it actually did, as JSON on stdout.
 *
 * Why this exists: S16 added the identity save round trip — Save reads DOM
 * inputs into the one in-memory document, Discard reverts it from a pristine
 * snapshot, and a 409 must show the server's message and a Reload control
 * without silently retrying. None of that is visible to a text-marker
 * assertion on the served HTML; this drives the page's real functions
 * instead, the pattern `jobs_client_probe.js` and
 * `applications_client_probe.js` already use.
 *
 * S17 (P2b) extends this to career-history editing: `saveIdentity`/
 * `discardIdentity` were renamed to `saveProfile`/`discardProfile` (they
 * already saved the whole document, not just Identity), and new scenarios
 * below drive `addChip`/`removeChip`, `addEvidence`/`deleteEvidence`, and
 * `deleteRoleGroup`'s confirmation directly, the same "call the top-level
 * function" convention as every scenario above. Career-tree fields are read
 * back from the DOM by schema-stable ID (`role_group_id`/`block_id`/
 * `evidence_id`), so a scenario that edits one field seeds every other field
 * of the same object to its loaded value first — the stub does not parse
 * `value="..."`/`selected` out of rendered HTML (see the header note above),
 * so an unseeded field reads back blank on every sync, same as row 0 of the
 * language list above.
 *
 * S18 (P2c) extends this the same way to education/miscellaneous entries,
 * `personal_interests`, and career goals: `addEducationEntry`, the `type`
 * select's own `change` listener (proving the section reclassification —
 * acceptance #2), and `addTargetRole`/`addInterestRow` are driven directly,
 * keyed by `education_id` the same way career-tree scenarios key by
 * `block_id`.
 *
 * S19 extends this to the compact chip adder: scenarios fire the real
 * `AddBtn`/`Input` listeners `wireChipAdder` attaches (click to open, a
 * `keydown` event object with `key: 'Enter'`/`'Escape'` to accept/cancel),
 * not `addChip` directly, since the thing under test is the open/close
 * wiring itself. It also drives `careerHoleWarnings` directly with a
 * synthetic role-group list — it takes plain data in and returns a plain
 * array out, so it needs no stub DOM at all.
 *
 * Deliberately no browser, no framework, no build step. The page's top-level
 * `function` declarations (not `let`/`const`) are reachable as properties of
 * the vm context, so scenarios call them directly — `saveProfile()`,
 * `dismissGap(id)`, `discardProfile()` — instead of simulating a click and
 * hoping a stub DOM agrees with a real one about event dispatch.
 *
 * The stub never parses the HTML strings the page assigns to `innerHTML`, so
 * it cannot reproduce a browser turning `value="English"` into a real input's
 * `.value`. Scenarios that need an existing row's value seed it explicitly
 * right after the render that would have produced it, documented at the one
 * place this matters (spoken-language row 0 below) — this is not a new
 * emulation, it is the test standing in for a browser's own attribute
 * parsing, which the stub deliberately does not attempt.
 *
 * BEFORE EDITING THE STUB BELOW: see the size/shape guards in
 * `tests/test_super_cv_client_behaviour.py` ("The stub may not become a
 * browser"). Adding *scenarios* is free; teaching the stub more browser
 * semantics is not — see that file's header comment for why.
 *
 * Usage: node super_cv_probe.js <path-to-super-cv.html>
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');

const match = html.match(/<script>\n([\s\S]*?)<\/script>/);
if (!match) {
  console.error('could not find the inline <script> block in ' + htmlPath);
  process.exit(2);
}
const source = match[1];

// ── A stub DOM ───────────────────────────────────────────────────────────

class El {
  constructor(id) {
    this.id = id;
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.style = {};
    this.listeners = {};
    this._html = '';
  }
  addEventListener(type, fn) {
    this.listeners[type] = fn;
  }
  focus() {}
  get innerHTML() {
    return this._html;
  }
  set innerHTML(v) {
    this._html = v;
  }
}

function loadPage() {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new El(id));
      return elements.get(id);
    },
  };

  const requests = [];
  let routes = {};
  function fetchStub(url, opts) {
    const method = ((opts && opts.method) || 'GET').toUpperCase();
    requests.push({ url: String(url), method, body: opts && opts.body });
    const key = Object.keys(routes).find((r) => String(url).indexOf(r) !== -1);
    const r = key ? routes[key] : { body: {} };
    return Promise.resolve({
      ok: r.ok !== false,
      status: r.status || (r.ok === false ? 500 : 200),
      json: async () => r.body || {},
    });
  }

  const context = {
    document,
    fetch: fetchStub,
    location: { search: '' },
    URLSearchParams,
    console,
    alert() {},
    confirm() { return false; },
    prompt() { return null; },
    // auth-guard.js globals. Every scenario here drives the save/discard/edit
    // functions directly, never the requireAuth()-gated init() IIFE, so these
    // only need to exist, not settle.
    requireAuth: () => new Promise(() => {}),
    hasRole: () => true,
    hasAnyRole: () => true,
    logout() {},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: path.basename(htmlPath) });

  return { ctx: context, requests, setRoutes(next) { routes = next; } };
}

// ── Scenarios ────────────────────────────────────────────────────────────

function baseDoc() {
  return {
    metadata: {
      schema_version: '1.3',
      consultant_id: 'nora',
      updated_at: '2026-01-01T00:00:00Z',
      _last_import_key: 'cv_import:example',
    },
    identity: {
      full_name: 'Original Name',
      email: 'orig@example.invalid',
      phone: '',
      location: { city: 'Gothenburg', country: 'Sweden' },
      spoken_languages: [{ language: 'English', proficiency: 'native', is_native: true }],
    },
    preferences: {},
    education: [],
    career_history: { role_groups: [] },
    gaps: [
      { gap_id: 'g_email', kind: 'data_conflict', status: 'open', suggested_question: 'Which email is correct?' },
    ],
  };
}

function putRequests(page) {
  return page.requests.filter((r) => r.method === 'PUT');
}

async function main() {
  const out = {};

  // 1. An identity edit and a structural language-row add both reach the PUT
  //    payload: Save reads the DOM at click time, not a copy made at load.
  const edit = loadPage();
  edit.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'h0', profile: baseDoc() });
  edit.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'h1', profile: baseDoc() } },
  });
  // The stub does not parse rendered HTML into DOM values (see header
  // comment): seed row 0 as a browser would have from the value="English"
  // attribute renderLanguageRows() just emitted, so the read-back below
  // reflects an untouched existing row rather than one that "lost" its text.
  edit.ctx.document.getElementById('langName0').value = 'English';
  edit.ctx.document.getElementById('langProf0').value = 'native';
  edit.ctx.document.getElementById('idFullName').value = 'New Name';
  edit.ctx.document.getElementById('idEmail').value = 'new@example.invalid';
  edit.ctx.addLanguageRow();
  edit.ctx.document.getElementById('langName1').value = 'Swedish';
  edit.ctx.document.getElementById('langProf1').value = 'fluent';
  await edit.ctx.saveProfile();
  const editPayload = JSON.parse(putRequests(edit)[0].body);
  out.edit_save_payload = {
    full_name: editPayload.profile.identity.full_name,
    email: editPayload.profile.identity.email,
    languages: editPayload.profile.identity.spoken_languages.map((l) => l.language),
    revision: editPayload.revision,
    content_hash: editPayload.content_hash,
  };
  out.identity_msg_after_save = edit.ctx.document.getElementById('identityMsg').textContent;

  // 2. Discard reverts the in-memory document, not just the banner text — a
  //    dismissed gap comes back as `open` in the very next save, proving the
  //    revert is real state, not a display-only message.
  const discard = loadPage();
  discard.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'h0', profile: baseDoc() });
  discard.ctx.dismissGap('g_email');
  discard.ctx.discardProfile();
  out.discard_msg_class = discard.ctx.document.getElementById('identityMsg').className;
  discard.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'h1', profile: baseDoc() } },
  });
  await discard.ctx.saveProfile();
  const discardPayload = JSON.parse(putRequests(discard)[0].body);
  out.gap_status_after_discard = discardPayload.profile.gaps[0].status;

  // 3. A 409 shows the server's message and a Reload control, and does not
  //    quietly adopt a new revision/hash to retry with.
  const conflict = loadPage();
  conflict.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'h0', profile: baseDoc() });
  conflict.setRoutes({
    '/api/super-cv/': { ok: false, status: 409, body: { detail: 'This profile was changed by someone else.' } },
  });
  conflict.ctx.document.getElementById('idFullName').value = 'Racing Edit';
  await conflict.ctx.saveProfile();
  out.conflict_message_html = conflict.ctx.document.getElementById('identityMsg').innerHTML;
  out.conflict_put_count = putRequests(conflict).length;
  const conflictPayload = JSON.parse(putRequests(conflict)[0].body);
  out.conflict_kept_original_revision =
    conflictPayload.revision === 0 && conflictPayload.content_hash === 'h0';

  // ── S17 (P2b): career-history editing ─────────────────────────────────
  //
  // One role group (rg1) / one block (blk1) / one evidence item (ev1), all
  // real (already-saved) schema IDs. `verified: true` on the block lets one
  // scenario prove it clears on an edit. These helpers seed a career-tree
  // object's DOM fields to its own loaded values — the stub does not parse
  // rendered `value="..."`/`selected` (see the file header), so an unseeded
  // field reads back blank on every sync, the same reason row 0 of the
  // language list is seeded explicitly above.

  function careerDoc() {
    return {
      metadata: { schema_version: '1.3', consultant_id: 'nora', updated_at: '2026-01-01T00:00:00Z' },
      identity: { full_name: 'Nora M', email: 'nora@example.invalid', phone: '',
        location: { city: '', country: '' }, spoken_languages: [] },
      preferences: {},
      education: [],
      career_history: {
        role_groups: [
          {
            role_group_id: 'rg1',
            organization: { name: 'Acme', country_code: '', industries: [] },
            display_role_title: 'Engineer',
            started: '', ended: '', employment_type: '',
            blocks: [
              {
                block_id: 'blk1',
                role_title: 'Old Title',
                started: '', ended: '',
                block_type: '', seniority: '',
                domains: ['embedded systems'], industries: [], languages: [], tools: [],
                processes_standards: [], verification_validation: [],
                verified: true,
                evidence_items: [
                  { evidence_id: 'ev1', type: 'responsibility', text: 'Did a thing' },
                ],
              },
            ],
          },
        ],
      },
      gaps: [],
    };
  }

  function setVal(page, id, value) { page.ctx.document.getElementById(id).value = value; }

  function seedDate(page, prefix, value, allowPresent) {
    const raw = String(value == null ? '' : value);
    if (raw === 'present') {
      if (allowPresent) page.ctx.document.getElementById(prefix + 'Present').checked = true;
      return;
    }
    const full = /^(\d{4})-(\d{2})$/.exec(raw);
    if (full) { setVal(page, prefix + 'Year', full[1]); setVal(page, prefix + 'Month', full[2]); return; }
    const yearOnly = /^(\d{4})$/.exec(raw);
    if (yearOnly) setVal(page, prefix + 'Year', yearOnly[1]);
  }

  function seedRoleGroupFields(page, rg) {
    const key = rg.role_group_id;
    setVal(page, 'rgOrgName_' + key, (rg.organization || {}).name || '');
    setVal(page, 'rgOrgCountry_' + key, (rg.organization || {}).country_code || '');
    setVal(page, 'rgTitle_' + key, rg.display_role_title || '');
    seedDate(page, 'rgStart_' + key, rg.started, false);
    seedDate(page, 'rgEnd_' + key, rg.ended, true);
    setVal(page, 'rgEmpType_' + key, rg.employment_type || '');
    setVal(page, 'rgSummary_' + key, rg.summary || '');
  }

  function seedBlockFields(page, block) {
    const key = block.block_id;
    setVal(page, 'blkTitle_' + key, block.role_title || '');
    seedDate(page, 'blkStart_' + key, block.started, false);
    seedDate(page, 'blkEnd_' + key, block.ended, true);
    setVal(page, 'blkType_' + key, block.block_type || '');
    setVal(page, 'blkSeniority_' + key, block.seniority || '');
    (block.evidence_items || []).forEach((ev) => {
      setVal(page, 'evType_' + ev.evidence_id, ev.type || '');
      setVal(page, 'evText_' + ev.evidence_id, ev.text || '');
    });
  }

  // 4. A block field edit reaches the save payload under the right block_id
  //    — the read-back is keyed by the schema's stable ID, not array
  //    position (s17.md "Relevant behavior").
  const blockEdit = loadPage();
  const blockDoc = careerDoc();
  blockEdit.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: blockDoc });
  seedRoleGroupFields(blockEdit, blockDoc.career_history.role_groups[0]);
  seedBlockFields(blockEdit, blockDoc.career_history.role_groups[0].blocks[0]);
  setVal(blockEdit, 'blkTitle_blk1', 'New Title');
  blockEdit.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await blockEdit.ctx.saveProfile();
  const blockPayload = JSON.parse(putRequests(blockEdit)[0].body);
  const savedBlock = blockPayload.profile.career_history.role_groups[0].blocks[0];
  out.block_edit_block_id = savedBlock.block_id;
  out.block_edit_role_title = savedBlock.role_title;

  // 5. Adding and deleting an evidence item: add one (a blank, not-yet-saved
  //    evidence_id — minted server-side, schema §2.6), delete the original,
  //    and confirm the block ends with exactly the new one.
  const evidence = loadPage();
  const evidenceDoc = careerDoc();
  evidence.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: evidenceDoc });
  evidence.ctx.addEvidence('blk1');
  evidence.ctx.deleteEvidence('blk1', 'ev1');
  setVal(evidence, 'evText_new1', 'Fresh evidence');
  evidence.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await evidence.ctx.saveProfile();
  const evidencePayload = JSON.parse(putRequests(evidence)[0].body);
  const evidenceItems = evidencePayload.profile.career_history.role_groups[0].blocks[0].evidence_items;
  out.evidence_add_delete_ids = evidenceItems.map((e) => e.evidence_id);
  out.evidence_add_delete_text = evidenceItems[0] && evidenceItems[0].text;

  // 6. A chip added to one typed group lands in that field and no other —
  //    the six groups write back to six separate schema fields, never a
  //    merged display-only list (acceptance #3).
  const chip = loadPage();
  const chipDoc = careerDoc();
  chip.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: chipDoc });
  setVal(chip, 'blkTools_blk1Input', 'Docker');
  chip.ctx.addChip('blk1', 'tools');
  chip.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await chip.ctx.saveProfile();
  const chipPayload = JSON.parse(putRequests(chip)[0].body);
  const chipBlock = chipPayload.profile.career_history.role_groups[0].blocks[0];
  out.chip_added_tools = chipBlock.tools;
  out.chip_added_domains = chipBlock.domains;

  // 7. Editing a block's content clears its `verified` flag (acceptance #6)
  //    — every other field is seeded to its loaded value first, so the one
  //    deliberate edit is what triggers the clear, not a stub artifact.
  const verify = loadPage();
  const verifyDoc = careerDoc();
  verify.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: verifyDoc });
  seedRoleGroupFields(verify, verifyDoc.career_history.role_groups[0]);
  seedBlockFields(verify, verifyDoc.career_history.role_groups[0].blocks[0]);
  setVal(verify, 'blkTitle_blk1', 'Changed Title');
  verify.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await verify.ctx.saveProfile();
  const verifyPayload = JSON.parse(putRequests(verify)[0].body);
  out.verified_after_content_edit =
    !!verifyPayload.profile.career_history.role_groups[0].blocks[0].verified;

  // 8. Deleting a whole role group asks first (acceptance #5): a declined
  //    confirmation leaves it in the next save payload; a confirmed one
  //    removes it.
  const rgDelete = loadPage();
  const rgDeleteDoc = careerDoc();
  rgDelete.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: rgDeleteDoc });
  rgDelete.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  rgDelete.ctx.deleteRoleGroup('rg1'); // stub confirm() defaults to false
  await rgDelete.ctx.saveProfile();
  const declinedPayload = JSON.parse(putRequests(rgDelete)[0].body);
  out.role_groups_after_declined_confirm = (declinedPayload.profile.career_history.role_groups || []).length;

  rgDelete.ctx.confirm = () => true;
  rgDelete.ctx.deleteRoleGroup('rg1');
  await rgDelete.ctx.saveProfile();
  const rgDeleteRequests = putRequests(rgDelete);
  const confirmedPayload = JSON.parse(rgDeleteRequests[rgDeleteRequests.length - 1].body);
  out.role_groups_after_confirmed_delete = (confirmedPayload.profile.career_history.role_groups || []).length;

  // 9. The evidence word counter is live (acceptance #4): typing past 60
  //    words updates the advisory count and over-guidance message from the
  //    textarea's own `input` event, not only on the next unrelated
  //    re-render — the wiring under test is wireEvidence's `input` listener
  //    calling updateEvidenceWordMeta, never syncProfileIntoDoc+renderCareer
  //    (that would rebuild the textarea out from under the keystroke).
  const counter = loadPage();
  const counterDoc = careerDoc();
  counter.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: counterDoc });
  const evTextEl = counter.ctx.document.getElementById('evText_ev1');
  const evMetaEl = counter.ctx.document.getElementById('evMeta_ev1');
  out.word_meta_before_typing = evMetaEl.textContent;
  const overLongText = Array.from({ length: 65 }, (_, i) => 'word' + i).join(' ');
  evTextEl.value = overLongText;
  evTextEl.listeners.input();
  out.word_meta_after_typing = evMetaEl.textContent;
  out.word_meta_color_after_typing = evMetaEl.style.color;

  // ── S18 (P2c): education, miscellaneous, personal interests, career goals ─
  //
  // One saved education entry (edu1) plus a full `preferences` object
  // carrying both the fields this editor renders and, per S19 acceptance #5,
  // `availability`/`work_mode`/`employment_types`/`location_preference` —
  // deleted from the schema itself but still present on this one document,
  // proving an unknown key still round-trips through the whole-document save
  // untouched rather than being dropped. Same "seed every DOM field to its
  // loaded value first" discipline as the career-tree helpers above.

  function goalsDoc() {
    return {
      metadata: { schema_version: '1.3', consultant_id: 'nora', updated_at: '2026-01-01T00:00:00Z' },
      identity: { full_name: 'Nora M', email: 'nora@example.invalid', phone: '',
        location: { city: '', country: '' }, spoken_languages: [], personal_interests: ['Chess'] },
      preferences: {
        target_roles: ['Embedded engineer'],
        target_industries: ['automotive'],
        seniority_target: 'senior',
        notes: 'Existing note',
        availability: { available_from: '2026-02-01', notice_period_weeks: 4, notes: '' },
        work_mode: ['remote'],
        employment_types: ['permanent'],
        location_preference: { primary_cities: ['Gothenburg'], willing_to_relocate: false, willing_to_travel_pct: 10 },
        avoid_industries: ['defense'],
      },
      education: [
        {
          education_id: 'edu1', type: 'certification', institution: 'Original Institute',
          qualification: 'Original Qualification', field_of_study: '',
          location: { city: '', country_code: '' }, started: '', ended: '', expires_at: '', description: '',
        },
      ],
      career_history: { role_groups: [] },
      gaps: [],
    };
  }

  function educationReviewDoc() {
    return {
      metadata: { schema_version: '1.3', consultant_id: 'nora', updated_at: '2026-01-01T00:00:00Z' },
      identity: { full_name: 'Nora M', email: 'nora@example.invalid', phone: '',
        location: { city: '', country: '' }, spoken_languages: [] },
      preferences: {},
      education: [
        {
          education_id: 'edu_clear', type: 'degree', institution: 'Old Institute',
          qualification: 'Old Qualification', field_of_study: '', location: { city: '', country_code: '' },
          started: '2015-09', ended: '2019-06', expires_at: '', description: '', needs_review: true,
        },
        {
          education_id: 'edu_keep', type: 'degree', institution: '', qualification: 'Kept Qualification',
          field_of_study: '', location: { city: '', country_code: '' },
          started: '2015-09', ended: '2019-06', expires_at: '', description: '', needs_review: true,
        },
        // No `location` key at all (not even a blank object) — the shape an
        // importer that never captured one actually produces. Complete and
        // untouched otherwise: saving must not clear its flag.
        {
          education_id: 'edu_untouched', type: 'degree', institution: 'Untouched Institute',
          qualification: 'Untouched Qualification', field_of_study: '',
          started: '2015-09', ended: '2019-06', expires_at: '', description: '', needs_review: true,
        },
      ],
      career_history: { role_groups: [] },
      gaps: [],
    };
  }

  function seedEducationFields(page, item) {
    const key = item.education_id;
    setVal(page, 'eduType_' + key, item.type || '');
    setVal(page, 'eduInstitution_' + key, item.institution || '');
    setVal(page, 'eduQualification_' + key, item.qualification || '');
    setVal(page, 'eduField_' + key, item.field_of_study || '');
    setVal(page, 'eduCity_' + key, (item.location || {}).city || '');
    setVal(page, 'eduCountry_' + key, (item.location || {}).country_code || '');
    seedDate(page, 'eduStart_' + key, item.started, false);
    seedDate(page, 'eduEnd_' + key, item.ended, true);
    seedDate(page, 'eduExpires_' + key, item.expires_at, false);
    setVal(page, 'eduDescription_' + key, item.description || '');
  }

  // 10. An education field edit reaches the save payload under the right
  //     `education_id` — the read-back is keyed by the schema's stable ID,
  //     the same convention G7 established for career history.
  const eduEdit = loadPage();
  const eduEditDoc = goalsDoc();
  eduEdit.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: eduEditDoc });
  seedEducationFields(eduEdit, eduEditDoc.education[0]);
  setVal(eduEdit, 'eduInstitution_edu1', 'New Institute');
  eduEdit.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: goalsDoc() } },
  });
  await eduEdit.ctx.saveProfile();
  const eduEditPayload = JSON.parse(putRequests(eduEdit)[0].body);
  const savedEdu = eduEditPayload.profile.education.find((e) => e.education_id === 'edu1');
  out.education_edit_id = savedEdu.education_id;
  out.education_edit_institution = savedEdu.institution;

  // 11. Adding an entry in each section seeds the expected default `type`
  //     (acceptance #3) — `addEducationEntry` is exactly what each section's
  //     own `+ Add` button calls, driven directly the same way `addEvidence`
  //     is above.
  const eduAdd = loadPage();
  const eduAddDoc = goalsDoc();
  eduAdd.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: eduAddDoc });
  eduAdd.ctx.addEducationEntry('degree');
  eduAdd.ctx.addEducationEntry('course');
  eduAdd.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: goalsDoc() } },
  });
  await eduAdd.ctx.saveProfile();
  const eduAddPayload = JSON.parse(putRequests(eduAdd)[0].body);
  out.education_added_types = eduAddPayload.profile.education
    .filter((e) => e.education_id === '')
    .map((e) => e.type);

  // 12. Re-typing an entry to `degree` moves it into the Education grid on
  //     the very next render (acceptance #2) — driven by firing the `type`
  //     select's own `change` listener, the same technique scenario 9 uses
  //     for the evidence textarea's `input` listener.
  const retype = loadPage();
  const retypeDoc = goalsDoc();
  retype.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: retypeDoc });
  seedEducationFields(retype, retypeDoc.education[0]);
  setVal(retype, 'eduType_edu1', 'degree');
  retype.ctx.document.getElementById('eduType_edu1').listeners.change();
  out.education_after_retype_in_education =
    retype.ctx.document.getElementById('education').innerHTML.indexOf('edu1') !== -1;
  out.education_after_retype_in_misc =
    retype.ctx.document.getElementById('misc').innerHTML.indexOf('edu1') !== -1;

  // 13. `needs_review` clears for an entry the human actually edited once it
  //     is complete, and stays for one edited but still missing a required
  //     field (acceptance #7). Also stays for a complete entry the human left
  //     untouched, even though `location`'s absence gets normalized to
  //     {city: '', country_code: ''} on read-back — that normalization must
  //     not be counted as a content edit (regression for the read-back bug
  //     flagged in review).
  const review = loadPage();
  const reviewDoc = educationReviewDoc();
  review.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: reviewDoc });
  seedEducationFields(review, reviewDoc.education[0]);
  seedEducationFields(review, reviewDoc.education[1]);
  seedEducationFields(review, reviewDoc.education[2]);
  setVal(review, 'eduInstitution_edu_clear', 'New Institute');
  setVal(review, 'eduQualification_edu_keep', 'Kept Qualification Updated');
  review.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: educationReviewDoc() } },
  });
  await review.ctx.saveProfile();
  const reviewPayload = JSON.parse(putRequests(review)[0].body);
  const clearedItem = reviewPayload.profile.education.find((e) => e.education_id === 'edu_clear');
  const keptItem = reviewPayload.profile.education.find((e) => e.education_id === 'edu_keep');
  const untouchedItem = reviewPayload.profile.education.find((e) => e.education_id === 'edu_untouched');
  out.needs_review_cleared = !clearedItem.needs_review;
  out.needs_review_retained = !!keptItem.needs_review;
  out.needs_review_untouched_retained = !!untouchedItem.needs_review;

  // 14. A career goal reaches `preferences` without disturbing the four
  //     schema-deleted fields plus `avoid_industries` (S19 acceptance #5) —
  //     an unknown `preferences` key still survives the whole-document save
  //     untouched, the existing passthrough rather than a migration.
  const goals = loadPage();
  const goalsEditDoc = goalsDoc();
  goals.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: goalsEditDoc });
  setVal(goals, 'prefRolesInput', 'Staff engineer');
  goals.ctx.addTargetRole();
  setVal(goals, 'prefSeniority', 'staff');
  goals.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: goalsDoc() } },
  });
  await goals.ctx.saveProfile();
  const goalsPayload = JSON.parse(putRequests(goals)[0].body);
  const savedPrefs = goalsPayload.profile.preferences;
  out.career_goal_target_roles = savedPrefs.target_roles;
  out.career_goal_seniority = savedPrefs.seniority_target;
  // Compared against an independently-built expectation, not `goalsEditDoc`
  // itself — `profileDoc` is that same object by reference (applyLoadedArtifact
  // does not clone it), so comparing against it would trivially "survive"
  // even if a handler mutated it.
  const untouchedExpected = goalsDoc().preferences;
  out.career_goal_unrendered_survive = ['availability', 'work_mode', 'employment_types',
    'location_preference', 'avoid_industries'].every(
    (field) => JSON.stringify(savedPrefs[field]) === JSON.stringify(untouchedExpected[field])
  );

  // 15. A `personal_interests` row is added and removed (acceptance #4),
  //     reusing the spoken-languages row-seeding discipline from scenario 1.
  const interests = loadPage();
  const interestsDoc = goalsDoc();
  interests.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: interestsDoc });
  setVal(interests, 'interest0', 'Chess');
  interests.ctx.addInterestRow();
  setVal(interests, 'interest1', 'Hiking');
  interests.ctx.removeInterestRow(0);
  // The stub does not parse rendered HTML into DOM values (see file header):
  // reseed row 0 to what the just-triggered re-render now shows there.
  setVal(interests, 'interest0', 'Hiking');
  interests.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: goalsDoc() } },
  });
  await interests.ctx.saveProfile();
  const interestsPayload = JSON.parse(putRequests(interests)[0].body);
  out.personal_interests_after_edit = interestsPayload.profile.identity.personal_interests;

  // ── S19: compact chip adders, and the nested-role career-gap fix ────────
  //
  // Every scenario below fires the real `AddBtn`/`Input` listeners
  // `wireChipAdder` attaches, not `addChip`/`addOrgIndustry`/`addTargetRole`
  // directly — the thing under test is the open/close wiring itself
  // (acceptance #1/#2), reusing `careerDoc()`'s `blk1` (`tools: []`,
  // `domains: ['embedded systems']`).

  // 16. Open -> type -> Enter adds a chip, and the compact "+ Add" control
  //     comes back (closing the adder) as soon as the value lands.
  const chipEnter = loadPage();
  const chipEnterDoc = careerDoc();
  chipEnter.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: chipEnterDoc });
  const toolsPrefix = 'blkTools_blk1';
  chipEnter.ctx.document.getElementById(toolsPrefix + 'AddBtn').listeners.click();
  out.chip_adder_open_display = chipEnter.ctx.document.getElementById(toolsPrefix + 'Open').style.display;
  out.chip_adder_compact_display_while_open =
    chipEnter.ctx.document.getElementById(toolsPrefix + 'Compact').style.display;
  setVal(chipEnter, toolsPrefix + 'Input', 'Docker');
  chipEnter.ctx.document.getElementById(toolsPrefix + 'Input').listeners.keydown({ key: 'Enter', preventDefault() {} });
  chipEnter.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await chipEnter.ctx.saveProfile();
  const chipEnterPayload = JSON.parse(putRequests(chipEnter)[0].body);
  out.chip_enter_added_tools = chipEnterPayload.profile.career_history.role_groups[0].blocks[0].tools;

  // 17. Open -> Escape adds nothing and closes back to compact — the typed
  //     text never reaches `profileDoc`, so the pre-existing chip survives
  //     unchanged and no new one appears.
  const chipEscape = loadPage();
  const chipEscapeDoc = careerDoc();
  chipEscape.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: chipEscapeDoc });
  const domainsPrefix = 'blkDomains_blk1';
  chipEscape.ctx.document.getElementById(domainsPrefix + 'AddBtn').listeners.click();
  setVal(chipEscape, domainsPrefix + 'Input', 'robotics');
  chipEscape.ctx.document.getElementById(domainsPrefix + 'Input').listeners.keydown({ key: 'Escape', preventDefault() {} });
  out.chip_adder_open_display_after_escape = chipEscape.ctx.document.getElementById(domainsPrefix + 'Open').style.display;
  chipEscape.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await chipEscape.ctx.saveProfile();
  const chipEscapePayload = JSON.parse(putRequests(chipEscape)[0].body);
  out.chip_escape_domains = chipEscapePayload.profile.career_history.role_groups[0].blocks[0].domains;

  // 18. An open adder's typed text is absent from the save payload even when
  //     Save is pressed with the adder still open (acceptance #2) — Save
  //     never reads a chip `Input`, only `profileDoc`, which addChip alone
  //     mutates.
  const chipUnsaved = loadPage();
  const chipUnsavedDoc = careerDoc();
  chipUnsaved.ctx.applyLoadedArtifact({ revision: 0, content_hash: 'ch0', profile: chipUnsavedDoc });
  chipUnsaved.ctx.document.getElementById(toolsPrefix + 'AddBtn').listeners.click();
  setVal(chipUnsaved, toolsPrefix + 'Input', 'Never Saved');
  chipUnsaved.setRoutes({
    '/api/super-cv/': { body: { username: 'nora', revision: 1, content_hash: 'ch1', profile: careerDoc() } },
  });
  await chipUnsaved.ctx.saveProfile();
  const chipUnsavedPayload = JSON.parse(putRequests(chipUnsaved)[0].body);
  out.chip_open_unsaved_tools = chipUnsavedPayload.profile.career_history.role_groups[0].blocks[0].tools;

  // 19. The nested-role career-gap regression (acceptance #3): a role group
  //     fully contained inside an earlier, longer one must not manufacture a
  //     gap against the role group that follows it, and the real gap must
  //     still be measured against the true latest end. `careerHoleWarnings`
  //     takes plain data and returns plain data, so this needs no stub page.
  const nested = loadPage();
  out.nested_role_gap_warnings = nested.ctx.careerHoleWarnings([
    { role_group_id: 'rg_ge', organization: { name: 'General Electric' }, started: '2005-09', ended: '2020-01' },
    { role_group_id: 'rg_itesm', organization: { name: 'ITESM' }, started: '2009-01', ended: '2009-06' },
    { role_group_id: 'rg_mpya', organization: { name: 'Mpya' }, started: '2021-10', ended: 'present' },
  ]);

  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
