/*
 * Runs the Published CVs page's real script — together with the real
 * html/auth-guard.js it includes — under a stub DOM, and reports what it
 * actually rendered, as JSON on stdout.
 *
 * Why this exists: the same S6 correction round that fixed Applications
 * found the identical defect here — `?consultant_id=` used in the scope
 * banner and forwarded to `renderTopNav` with no `GET /api/me/consultants`
 * check — plus a second one: a publication row offered only Extend/Revoke,
 * with no way to open the Application or tailored-CV editor it belongs to,
 * including an expired publication that needs republishing. Static text
 * assertions cannot see either: this drives the page's own real
 * `window.__initReady` through a real `resolveConsultantContext()` and a
 * programmable fetch, then reports what actually landed in the DOM.
 *
 * Usage: node published_cvs_client_probe.js <path-to-published-cvs.html> <path-to-auth-guard.js>
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const pagePath = process.argv[2];
const authGuardPath = process.argv[3];
const html = fs.readFileSync(pagePath, 'utf8');
const authGuardSource = fs.readFileSync(authGuardPath, 'utf8');

const match = html.match(/<script>\n([\s\S]*?)<\/script>/);
if (!match) {
  console.error('could not find the inline <script> block in ' + pagePath);
  process.exit(2);
}
const pageSource = match[1];

// ── A stub DOM ──────────────────────────────────────────────────────────────

class El {
  constructor(id) {
    this.id = id || '';
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this._html = '';
  }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  closest() { return null; }
  querySelectorAll() { return []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = v; }
}

// `routes` must be fully populated *before* the page script runs: requireAuth's
// first synchronous action is `fetch('/api/auth/me')`, called while
// `vm.runInContext(pageSource, ...)` is still executing below — a route table
// filled in afterward is too late and every request falls through to the
// `{}` fallback, which requireAuth reads as an unauthorized session and never
// resolves. See tests/client/cv_identity_probe.js's `run()` for the same
// precedent: build the stub fully, then evaluate.
function loadPage(search, routes) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new El(id));
      return elements.get(id);
    },
  };

  function fetchStub(url) {
    const key = Object.keys(routes).find((r) => url.startsWith(r));
    if (!key) return Promise.resolve({ ok: true, json: async () => ({}) });
    const r = routes[key];
    return Promise.resolve({
      ok: r.ok !== false,
      status: r.status || (r.ok === false ? 500 : 200),
      json: async () => r.body,
    });
  }

  const context = {
    document,
    fetch: (...args) => fetchStub(...args),
    location: { search: search || '' },
    URLSearchParams,
    console,
    alert() {},
    confirm() { return false; },
    prompt() { return null; },
    logout() {},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(authGuardSource, context, { filename: path.basename(authGuardPath) });
  vm.runInContext(pageSource, context, { filename: path.basename(pagePath) });

  return { context, elements };
}

// ── Scenarios ────────────────────────────────────────────────────────────────

const ME = { username: 'the.ta', roles: ['ta'] };
const CONSULTANTS = [
  { id: 'id-a', username: 'consultant.a', full_name: 'A' },
  { id: 'id-b', username: 'consultant.b', full_name: 'B' },
];

const NOW = Date.now();
const PUBLICATIONS = [
  {
    id: 'pub-active', application_id: 'app-active', consultant_username: 'consultant.a',
    consultant_full_name: 'A', title: 'Role A', job_id: 'job-a', revoked: false,
    expires_at: new Date(NOW + 5 * 86400000).toISOString(), view_count: 0,
    last_viewed_at: null, created_at: new Date(NOW - 86400000).toISOString(), status: 'published',
  },
  {
    // Expired but not revoked — the case that most needs Open, since Extend
    // is unavailable and republishing means opening the editor first.
    id: 'pub-expired', application_id: 'app-expired', consultant_username: 'consultant.b',
    consultant_full_name: 'B', title: 'Role B', job_id: 'job-b', revoked: false,
    expires_at: new Date(NOW - 86400000).toISOString(), view_count: 2,
    last_viewed_at: new Date(NOW - 3600000).toISOString(),
    created_at: new Date(NOW - 10 * 86400000).toISOString(), status: 'link_opened',
  },
  {
    id: 'pub-revoked', application_id: 'app-revoked', consultant_username: 'consultant.a',
    consultant_full_name: 'A', title: 'Role C', job_id: 'job-c', revoked: true,
    expires_at: new Date(NOW + 86400000).toISOString(), view_count: 0,
    last_viewed_at: null, created_at: new Date(NOW - 2 * 86400000).toISOString(), status: 'published',
  },
];

function baseRoutes() {
  return {
    '/api/auth/me': { body: ME },
    '/api/me/consultants': { body: { consultants: CONSULTANTS, total: CONSULTANTS.length } },
    '/api/publications': { body: { publications: PUBLICATIONS, total: PUBLICATIONS.length } },
  };
}

async function run(search, routeOverrides) {
  const routes = Object.assign(baseRoutes(), routeOverrides || {});
  const page = loadPage(search, routes);
  await page.context.__initReady;
  const scopeMsg = page.elements.get('scopeMsg');
  const topNav = page.elements.get('topNav');
  const pubList = page.elements.get('pubList');
  return {
    scopeMsg: scopeMsg ? { className: scopeMsg.className, text: scopeMsg.textContent, html: scopeMsg.innerHTML } : null,
    topNavHtml: topNav ? topNav.innerHTML : '',
    pubListHtml: pubList ? pubList.innerHTML : '',
  };
}

async function main() {
  const out = {};

  out.no_context = await run('');
  out.valid_context = await run('?consultant_id=consultant.a');
  out.invalid_context = await run('?consultant_id=not.mine');
  out.failed_lookup = await run('?consultant_id=consultant.a', {
    '/api/me/consultants': { ok: false, status: 500, body: { detail: 'boom' } },
  });

  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
