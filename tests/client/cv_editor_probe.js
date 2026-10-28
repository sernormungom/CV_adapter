/*
 * Runs the CV editor's own scripts under a stub DOM and reports every request
 * they made, as JSON on stdout.
 *
 * Why this exists: `S3-P1-R1` finding 1 was a control-flow defect that every
 * text assertion in the static gate was green on. The editor holds three actions
 * that write the consultant's *base* profile material — the photo upload, the
 * chat profile patch, and CV import (V2 Slice 4's durable
 * handleAsyncImportFile, replacing the retired synchronous importer) — and
 * none of them went through _persistCvState(), the one writer that switches on
 * the artifact mode. So in
 * Application mode the page would happily replace the photo and the profile
 * facts that every other CV renders from, while every artifact's revision and
 * hash stayed put: P-009 displayed as kept and not kept. Asserting that the
 * guard *text* is present cannot tell you the guard runs, or that it runs
 * before the awaits that made the window wide.
 *
 * What this probe asserts, and why the stub's fidelity does not matter to it:
 * the assertions are about **which URLs fetch received**, not about rendering.
 * A hand-rolled DOM agrees with a real browser only on the cases someone
 * thought about, so this stub deliberately does not try to model browser
 * behaviour at all — `document` answers every id and selector with the same
 * uniform recording element. That is why it stays short despite driving a much
 * larger page than `jobs_client_probe.js` does, and it is also why it must not
 * grow behavioural special cases: the moment an assertion here depends on the
 * stub *acting* like a browser, this gate starts asserting fiction. A test that
 * needs that is a test that needs a real browser harness.
 *
 * Usage: node cv_editor_probe.js <path-to-cv-importer.html> <path-to-cv-render.js>
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const htmlPath = process.argv[2];
const renderPath = process.argv[3];
const html = fs.readFileSync(htmlPath, 'utf8');

// The page loads: inline block 1, then cv-render.js, then inline block 2. Run
// them in that order, because the second block references the render core's
// globals (skills/expList/render/esc) and the first defines what it builds on.
const blocks = [...html.matchAll(/<script>\r?\n([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (blocks.length !== 2) {
  console.error('expected 2 inline <script> blocks in ' + htmlPath + ', found ' + blocks.length);
  process.exit(2);
}

// ── A uniform recording stub ─────────────────────────────────────────────────
// One element class for every id and selector. No per-element behaviour: see
// the header. `style` and `dataset` are plain objects so assignment works.

class El {
  constructor(id) {
    this.id = id || '';
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
    this.innerText = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.files = [];
    this.style = {};
    this.dataset = {};
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    this.children = [];
    this.parentNode = null;
  }
  addEventListener() {}
  removeEventListener() {}
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { return child; }
  insertBefore(child) { this.children.push(child); return child; }
  remove() {}
  closest() { return new El(''); }
  querySelector() { return new El(''); }
  querySelectorAll() { return []; }
  getAttribute() { return null; }
  setAttribute() {}
  removeAttribute() {}
  focus() {}
  blur() {}
  click() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }; }
}

// Each scenario group gets a freshly loaded page. That is not tidiness: the
// editor's mode lives in a `let`, which in a vm script is script-scoped and so
// is deliberately NOT reachable from here — the probe cannot poke it, and must
// not, because a test that sets the mode directly proves nothing about the code
// that sets it. Every mode below is therefore reached by calling the page's real
// entry point, and a mode reached that way cannot be reversed, so scenarios that
// need a different starting mode need a different page.
function loadPage() {
const elements = new Map();
const document = {
  readyState: 'loading',
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new El(id));
    return elements.get(id);
  },
  createElement: () => new El(''),
  createTextNode: () => new El(''),
  querySelector: () => new El(''),
  querySelectorAll: () => [],
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
  removeEventListener() {},
  execCommand() { return true; },
  _listeners: {},
  body: new El('body'),
  head: new El('head'),
  documentElement: new El('html'),
};

// ── A recording fetch ────────────────────────────────────────────────────────

const requests = [];
let routes = {};

function fetchStub(url, opts) {  // eslint-disable-line no-inner-declarations
  requests.push({
    url: String(url),
    method: ((opts && opts.method) || 'GET').toUpperCase(),
    body: opts && typeof opts.body === 'string' ? opts.body : null,
  });
  const key = Object.keys(routes).find((r) => String(url).indexOf(r) !== -1);
  const r = key ? routes[key] : { body: {} };
  return Promise.resolve({
    ok: r.ok !== false,
    status: r.status || (r.ok === false ? 500 : 200),
    headers: { get: () => 'application/json' },
    json: async () => r.body || {},
    text: async () => JSON.stringify(r.body || {}),
  });
}

// A fetch that never settles, for asserting on what happened *during* a load's
// awaits rather than after them.
function hangingFetch(url, opts) {
  requests.push({ url: String(url), method: ((opts && opts.method) || 'GET').toUpperCase() });
  return new Promise(() => {});
}

const storage = new Map();
const context = {
  document,
  fetch: fetchStub,
  location: { search: '', href: '', hash: '', pathname: '/', origin: 'http://t' },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  URLSearchParams,
  FormData: class { append() {} },
  FileReader: class { readAsDataURL() { if (this.onload) this.onload({ target: { result: 'data:,' } }); } },
  Image: class {},
  console,
  alert() {},
  confirm: () => false,
  prompt: () => null,
  setTimeout: (fn) => { void fn; return 0; },
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: (fn) => { void fn; return 0; },
  matchMedia: () => ({ matches: false, addListener() {}, addEventListener() {} }),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  print() {},
  scrollTo() {},
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
  navigator: { userAgent: 'probe', clipboard: { writeText: async () => {} } },
  addEventListener() {},
  removeEventListener() {},
  requireAuth: () => new Promise(() => {}),
  hasRole: () => true,
  hasAnyRole: () => true,
  logout() {},
};
context.window = context;
context.self = context;
context.globalThis = context;
vm.createContext(context);

const sources = [
  [blocks[0], 'cv-importer.html#1'],
  [fs.readFileSync(renderPath, 'utf8'), path.basename(renderPath)],
  [blocks[1], 'cv-importer.html#2'],
];
for (const [source, filename] of sources) {
  try {
    vm.runInContext(source, context, { filename });
  } catch (err) {
    console.error('failed loading ' + filename + ': ' + err.message);
    process.exit(3);
  }
}

storage.set('mpya_username', 'nora');
storage.set('mpya_consultant_id', 'nora');

// Everything a scenario is allowed to touch. Note there is no way to set the
// mode: see the comment on loadPage().
return {
  ctx: context,
  requests,
  setRoutes(next) { routes = next; },
  hang() { context.fetch = hangingFetch; },
  unhang() { context.fetch = fetchStub; },
  reset() { requests.length = 0; },
};
}

// ── Scenarios ────────────────────────────────────────────────────────────────

// The three endpoints that write the consultant's own profile material. Any
// non-GET to one of these is a base-profile write, whatever the page meant.
const BASE_WRITERS = ['/api/profiles/', '/api/import-cv', '/api/chat/profile-patch'];

function baseWrites(page) {
  return page.requests
    .filter((r) => r.method !== 'GET' && BASE_WRITERS.some((w) => r.url.indexOf(w) !== -1))
    .map((r) => r.method + ' ' + r.url);
}

const photoInput = () => ({ files: [{ name: 'me.png' }], value: 'x' });
const importFile = () => ({ name: 'me.pdf' });
const PATCH = { iName: 'Injected by a tailored session' };

const APP_ROUTES = {
  '/api/applications/app-1/cv': {
    body: {
      tailored_cv: { id: 'art-1', status: 'ready', revision: 3 },
      artifact: {
        mode: 'application', id: 'art-1', revision: 3,
        content_hash: 'h', state: { iName: 'Tailored' },
      },
    },
  },
  '/api/applications/app-1': {
    body: { id: 'app-1', consultant_username: 'nora', status: 'applied', job: {} },
  },
};

async function attemptBaseWrites(page) {
  page.ctx.handlePhoto(photoInput());
  // Started, then raced against a tick rather than awaited to completion. Two
  // of the scenarios below run under a fetch that never resolves — and even
  // under the normal stub, handleAsyncImportFile's poll loop awaits a
  // setTimeout the stub never fires — so a call that is *not* refused
  // legitimately never returns, and awaiting it would park main() forever,
  // which Node reports as a silent exit 0 rather than a failure. Each writer
  // issues its request synchronously before its first await, so a tick is
  // enough for it to have been recorded either way. This is what keeps a
  // missing guard a red assertion instead of an empty result.
  await Promise.race([
    page.ctx.chatApplyProfile(new Object(), PATCH),
    new Promise((resolve) => setImmediate(resolve)),
  ]);
  await Promise.race([
    page.ctx.handleAsyncImportFile(importFile()),
    new Promise((resolve) => setImmediate(resolve)),
  ]);
}

async function main() {
  const out = {};

  // 0. The retired synchronous importer must not be wired up at all. These are
  //    top-level function declarations, so they are observable here.
  const probeOnly = loadPage();
  out.import_symbols = {
    handleImportFile: typeof probeOnly.ctx.handleImportFile,
    populateFromImport: typeof probeOnly.ctx.populateFromImport,
    importBtnState: typeof probeOnly.ctx.importBtnState,
  };
  out.has_base_gate = typeof probeOnly.ctx._baseProfileWritesAllowed;
  out.has_async_import = typeof probeOnly.ctx.handleAsyncImportFile;

  // 0b. S10: the "Generate CV" modal and everything that only served it are
  // deleted, not hidden — these are top-level function/variable declarations,
  // so an absent one is observable here.
  out.generate_modal_symbols = {
    openGenerateModal: typeof probeOnly.ctx.openGenerateModal,
    closeGenerateModal: typeof probeOnly.ctx.closeGenerateModal,
    generateCV: typeof probeOnly.ctx.generateCV,
    _genAppRow: typeof probeOnly.ctx._genAppRow,
    _generateApplications: typeof probeOnly.ctx._generateApplications,
    _selectedApplicationIdx: typeof probeOnly.ctx._selectedApplicationIdx,
  };

  // 0c. S10: body text for all four sections uses one fixed shared style,
  // never read back off DOM inputs — even if elements with the old ids
  // (pfFont/pfSize/...) happen to exist with stale values (e.g. a document
  // that still holds them), render() must not reflect them.
  probeOnly.ctx.document.getElementById('pfFont').value = "'Playfair Display',serif";
  probeOnly.ctx.document.getElementById('pfSize').value = '22';
  probeOnly.ctx.document.getElementById('pfColor').value = '#ff0000';
  probeOnly.ctx.document.getElementById('pfWeight').value = '600';
  probeOnly.ctx.document.getElementById('iSummary').value = 'Some profile summary text.';
  probeOnly.ctx.render();
  out.profile_style_sheet_html = probeOnly.ctx.document.getElementById('cvSheet').innerHTML;

  // 1. Base mode — the control. A page that never entered another mode DOES
  //    write base profile material. Without this the absences below could just
  //    mean the stub never reaches the code at all.
  const base = loadPage();
  base.reset();
  await attemptBaseWrites(base);
  out.base_mode_writes = baseWrites(base);
  out.base_mode_gate = base.ctx._baseProfileWritesAllowed();

  // 2. The pre-await window (`S3-P1-R1` finding 1). The load is parked inside
  //    its first request and never resolves, so this is exactly the state the
  //    page was in for two round trips. The mode must already be claimed, and
  //    a base-profile write attempted right then must still be refused.
  const loading = loadPage();
  loading.hang();
  loading.ctx._enterApplicationEditMode('app-1');
  out.app_mode_claimed_before_await = loading.ctx._appEditMode();
  loading.reset();
  await attemptBaseWrites(loading);
  out.writes_during_app_load = baseWrites(loading);

  // 3. Application mode, fully settled through the real entry point.
  const app = loadPage();
  app.setRoutes(APP_ROUTES);
  await app.ctx._enterApplicationEditMode('app-1');
  out.app_mode_after_load = app.ctx._appEditMode();
  app.reset();
  await attemptBaseWrites(app);
  out.application_mode_base_writes = baseWrites(app);
  out.application_mode_gate = app.ctx._baseProfileWritesAllowed();

  //    ...and a CV-state save in that mode goes to the Application, never base.
  app.reset();
  await app.ctx._persistCvState(false);
  out.application_mode_state_save = app.requests.map((r) => r.method + ' ' + r.url);

  // 4. Base mode's own CV-state save, for contrast with 3. (A publication is a
  //    read-only, server-derived snapshot of a tailored CV since S5-P1-R1 —
  //    there is no separate publication-edit mode to probe here any more.)
  const baseSave = loadPage();
  baseSave.setRoutes({
    '/api/cv/base/nora': {
      body: { mode: 'base', id: 'nora', revision: 2, content_hash: 'h', state: { iName: 'Mine' } },
    },
  });
  await baseSave.ctx.loadProfile();
  baseSave.reset();
  await baseSave.ctx._persistCvState(false);
  out.base_mode_state_save = baseSave.requests.map((r) => r.method + ' ' + r.url);

  // 5. S22: the photo reference the editor *saves* carries no cache-buster.
  //    Every page load mints a fresh `?t=` for the <img>; saving it made the
  //    first autosave after each load a content change that bumped the
  //    revision. Two paths set the in-memory photo on load: restoring a stored
  //    reference (here stored with a stale query, as pre-S22 saves left it),
  //    and scenario 4's fallback to the backend photo when the state has none.
  const savedPhoto = (page) => {
    const put = page.requests.find((r) => r.method === 'PUT');
    return put ? JSON.parse(put.body).state.photoSrc : 'no PUT was sent';
  };
  out.saved_photo_from_backend_fallback = savedPhoto(baseSave);
  const restored = loadPage();
  restored.setRoutes({
    '/api/cv/base/nora': {
      body: {
        mode: 'base', id: 'nora', revision: 2, content_hash: 'h',
        state: { iName: 'Mine', photoSrc: '/api/profiles/nora/photo?t=123' },
      },
    },
  });
  await restored.ctx.loadProfile();
  restored.reset();
  await restored.ctx._persistCvState(false);
  out.saved_photo_from_stored_state = savedPhoto(restored);

  process.stdout.write(JSON.stringify(out, null, 2));
}

// A hang must be a loud failure, not an empty stdout and exit 0 — which is what
// Node does when the event loop drains with main() still pending.
const watchdog = setTimeout(() => {
  console.error('the probe did not finish: a scenario is parked on a promise '
              + 'that never settles. See attemptBaseWrites().');
  process.exit(5);
}, 20000);

main()
  .then(() => clearTimeout(watchdog))
  .catch((err) => {
    clearTimeout(watchdog);
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(4);
  });
