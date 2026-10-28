/*
 * Runs the Consultants page's real script — together with the real
 * html/auth-guard.js it includes — under a stub DOM, and reports what it
 * actually rendered, as JSON on stdout.
 *
 * Why this exists: the S6 review found that Consultants always rendered its
 * top-banner nav with an empty Consultant context (`renderTopNav(..., '')`),
 * dropping whatever `?consultant_id=` a TA arrived with. The round trip this
 * broke: pick Consultant B on Jobs, follow the nav to Consultants, then back
 * to Jobs — Jobs would default back to the first Consultant rather than
 * staying on B. This drives the page's own real `window.__initReady` through
 * a real `resolveConsultantContext()` and a programmable fetch, then checks
 * what the rendered nav links actually carry forward.
 *
 * Usage: node consultants_client_probe.js <path-to-consultants.html> <path-to-auth-guard.js>
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
    setInterval() { return 0; },
    clearInterval() {},
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

function baseRoutes() {
  return {
    '/api/auth/me': { body: ME },
    '/api/me/consultants': { body: { consultants: CONSULTANTS, total: CONSULTANTS.length } },
    '/api/ta/consultants': { body: { consultants: [] } },
    '/api/notifications': { body: { notifications: [], unread: 0 } },
  };
}

async function run(search, routeOverrides) {
  const routes = Object.assign(baseRoutes(), routeOverrides || {});
  const page = loadPage(search, routes);
  await page.context.__initReady;
  const topNav = page.elements.get('topNav');
  return { topNavHtml: topNav ? topNav.innerHTML : '' };
}

async function main() {
  const out = {};

  // 1. Arriving from Jobs with an authorized selected Consultant: the round
  //    trip this page used to break. The nav back to Jobs/Applications/
  //    Published CVs must still carry it.
  out.valid_context = await run('?consultant_id=consultant.b');

  // 2. No context at all — the ordinary case, no consultant_id anywhere.
  out.no_context = await run('');

  // 3. A stale/unauthorized value must not be forwarded either.
  out.invalid_context = await run('?consultant_id=not.mine');

  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
