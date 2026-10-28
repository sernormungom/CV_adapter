/*
 * Runs the Applications page's real script — together with the real
 * html/auth-guard.js it includes — under a stub DOM, and reports what it
 * actually rendered, as JSON on stdout.
 *
 * Why this exists: an S6 correction round found that Applications read its
 * `?consultant_id=` straight from the URL and used it in a banner, a
 * client-side filter and a forwarded nav link without ever asking
 * `GET /api/me/consultants` whether the caller may actually act for it. A
 * stale or hand-typed context therefore rendered as if it were valid. Static
 * text assertions cannot see that — this drives the page's own
 * `window.__initReady` (the same "await the real init" convention
 * html/cv-importer.html already uses for `window.__identityReady`) through
 * a real `resolveConsultantContext()` and a programmable fetch, then reports
 * what actually landed in the DOM.
 *
 * The real html/auth-guard.js is loaded (not re-implemented) for the same
 * reason tests/client/cv_editor_probe.js loads the real cv-render.js: a
 * hand-rolled re-implementation of resolveConsultantContext would only prove
 * the probe's own guess agrees with itself.
 *
 * Usage: node applications_client_probe.js <path-to-applications.html> <path-to-auth-guard.js>
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
  // Records what the page set, nothing more (read back by the S21 scenarios).
  setAttribute(name, value) { this.attributes = Object.assign({}, this.attributes, { [name]: String(value) }); }
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
const APPLICATIONS = [
  { id: 'app-a', consultant_username: 'consultant.a', status: 'applied', job: { title: 'Role A' } },
  { id: 'app-b', consultant_username: 'consultant.b', status: 'applied', job: { title: 'Role B' } },
];

function baseRoutes() {
  return {
    '/api/auth/me': { body: ME },
    '/api/me/consultants': { body: { consultants: CONSULTANTS, total: CONSULTANTS.length } },
    '/api/applications': { body: { applications: APPLICATIONS } },
  };
}

async function run(search, routeOverrides) {
  const routes = Object.assign(baseRoutes(), routeOverrides || {});
  const page = loadPage(search, routes);
  await page.context.__initReady;
  const scopeMsg = page.elements.get('scopeMsg');
  const appCount = page.elements.get('appCount');
  const topNav = page.elements.get('topNav');
  return {
    scopeMsg: scopeMsg ? { className: scopeMsg.className, text: scopeMsg.textContent, html: scopeMsg.innerHTML } : null,
    appCount: appCount ? appCount.textContent : null,
    topNavHtml: topNav ? topNav.innerHTML : '',
  };
}

// The description is untrusted posting text; the URL deliberately carries the
// characters an attribute injection would need.
const DESC_APPS = [
  { id: 'app-desc', consultant_username: 'consultant.a', status: 'applied', job: { id: 'job-desc', title: 'Role D', company: 'Acme' } },
  { id: 'app-js', consultant_username: 'consultant.a', status: 'applied', job: { id: 'job-js', title: 'Role JS' } },
  { id: 'app-404', consultant_username: 'consultant.a', status: 'applied', job: { id: 'job-404', title: 'Role Gone Away' } },
  { id: 'app-gone', consultant_username: 'consultant.a', status: 'closed', job: null },
];
const DETAIL = {
  id: 'job-desc', title: 'Role D', company: 'Acme Corp', location: 'Göteborg',
  close_date: '2026-12-31', status: 'open',
  description: 'Line one\nLine two <img src=x onerror=alert(1)>',
  source_url: 'https://example.test/p?a=1&b="x" onmouseover="alert(1)',
};

async function descriptionScenarios() {
  const page = loadPage('', Object.assign(baseRoutes(), {
    '/api/applications': { body: { applications: DESC_APPS } },
    '/api/jobs/job-desc': { body: DETAIL },
    '/api/jobs/job-js': { body: Object.assign({}, DETAIL, { id: 'job-js', source_url: 'javascript:alert(1)' }) },
    '/api/jobs/job-404': { ok: false, status: 404, body: { detail: 'Job not found' } },
  }));
  await page.context.__initReady;
  const requested = [];
  const realFetch = page.context.fetch;
  page.context.fetch = (url, opts) => { requested.push(url); return realFetch(url, opts); };
  const list = page.elements.get('appList').innerHTML;
  const view = (id) => {
    const el = page.elements.get('desc-' + id);
    const btn = page.elements.get('descbtn-' + id);
    return {
      className: el ? el.className : null,
      html: el ? el.innerHTML : null,
      expanded: btn && btn.attributes ? btn.attributes['aria-expanded'] : null,
    };
  };
  const toggle = (id) => page.context.toggleDescription(DESC_APPS.find((a) => a.id === id));

  const out = {
    list,
    title_control: /data-act="desc"[^>]*>Role D<\/button>/.test(list),
    removed_row_plain: list.includes('(job removed)')
      && !/data-act="desc"[^>]*>\(job removed\)/.test(list),
    removed_row_has_no_view: !list.includes('id="desc-app-gone"'),
  };
  await toggle('app-desc');
  out.expanded = view('app-desc');
  out.fetched = requested.slice();
  await toggle('app-desc');
  out.collapsed = view('app-desc');
  await toggle('app-js');
  out.non_http_url = view('app-js');
  await toggle('app-404');
  out.not_found = view('app-404');
  return out;
}

async function main() {
  const out = {};

  // 1. No context requested at all — no banner, every reachable Application
  //    shown, and nav links carry no consultant_id.
  out.no_context = await run('');

  // 2. A context the policy actually authorizes — the info banner, the
  //    filtered count, and the nav links carry it forward.
  out.valid_context = await run('?consultant_id=consultant.a');

  // 3. A context NOT in the caller's authorized set (a stale link, someone
  //    else's username, a typo). The regression: this used to render as if
  //    it were a real selection. It must now be dropped — no scope banner
  //    claiming it, unfiltered results, and nothing forwarded on nav links.
  out.invalid_context = await run('?consultant_id=not.mine');

  // 4. GET /api/me/consultants itself fails. A failed lookup is not an
  //    "unassigned" one: this must show an error, not silently drop to
  //    "showing all" with no explanation, and must not forward the
  //    unvalidated value either.
  out.failed_lookup = await run('?consultant_id=consultant.a', {
    '/api/me/consultants': { ok: false, status: 500, body: { detail: 'boom' } },
  });

  // 5. S21 (V3-P-032): the Job title expands a read-only description view.
  out.description = await descriptionScenarios();

  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
