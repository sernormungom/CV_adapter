/*
 * Runs the Jobs page's own inline script under a stub DOM and reports what it
 * actually did, as JSON on stdout.
 *
 * Why this exists: the static gate asserts that markers appear in the served
 * HTML, which cannot see a *control-flow* defect. Two S2-P1 review findings
 * were exactly that shape — a consultant-context load failure whose error
 * banner was immediately overwritten by the "ask an admin" message, and
 * decision badges rendered from the whole visible map instead of the selected
 * consultant. Both were green under text assertions. This executes the real
 * code path instead.
 *
 * Deliberately no browser, no framework, no build step: a few dozen lines of
 * stub is enough to drive the two functions under test, and anything heavier
 * would be a new dependency for a gate that documents needing none.
 *
 * BEFORE EDITING THE STUB BELOW: it is size- and shape-capped by guards in
 * `tests/test_jobs_client_behaviour.py` ("The stub may not become a browser"),
 * which state the rule and the reasoning. Adding *scenarios* is free and is
 * what this file is for. Teaching the stub more browser behaviour is not: a
 * hand-rolled DOM agrees with a real one only on the cases someone thought
 * about, so past a certain point this gate starts asserting fiction. If the
 * page needs more DOM than this, the answer is a browser harness, not a
 * bigger stub.
 *
 * Usage: node jobs_client_probe.js <path-to-jobs.html>
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');

// The page has two <script> tags: the auth-guard include (which has a src
// attribute) and the inline one. Match the attribute-less opening tag.
const match = html.match(/<script>\n([\s\S]*?)<\/script>/);
if (!match) {
  console.error('could not find the inline <script> block in ' + htmlPath);
  process.exit(2);
}
const source = match[1];

// ── A stub DOM, only as much as the page touches ─────────────────────────────

class El {
  constructor(id) {
    this.id = id;
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.dataset = {};
    this.listeners = {};
    this._html = '';
  }
  addEventListener(type, fn) {
    this.listeners[type] = fn;
  }
  closest() {
    return null;
  }
  get innerHTML() {
    return this._html;
  }
  set innerHTML(v) {
    this._html = v;
    // A <select> whose options are replaced adopts the first option's value,
    // or '' when there are none — the behaviour loadConsultants relies on.
    if (this.id === 'consultantSelect') {
      const first = /value="([^"]*)"/.exec(v);
      this.value = first ? first[1] : '';
    }
  }
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new El(id));
    return elements.get(id);
  },
};

// ── A programmable fetch ─────────────────────────────────────────────────────

let routes = {};
function fetchStub(url) {
  const key = Object.keys(routes).find((r) => url.startsWith(r));
  if (!key) {
    return Promise.resolve({ ok: true, json: async () => ({}) });
  }
  const r = routes[key];
  return Promise.resolve({
    ok: r.ok !== false,
    status: r.status || (r.ok === false ? 500 : 200),
    json: async () => r.body,
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
  // auth-guard.js globals. requireAuth never settles on purpose: the page's
  // init() IIFE awaits it, so it parks there and cannot race the scenarios
  // below, while every function it would have called stays reachable.
  requireAuth: () => new Promise(() => {}),
  hasRole: () => true,
  hasAnyRole: () => true,
  logout() {},
};
context.window = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: path.basename(htmlPath) });

// ── Scenarios ────────────────────────────────────────────────────────────────

const ctx = () => document.getElementById('ctxMsg');
const banner = () => ({ className: ctx().className, text: ctx().textContent });

const CONSULTANTS = [
  { id: 'id-selected', username: 'selected.consultant', full_name: 'Selected' },
  { id: 'id-other', username: 'other.consultant', full_name: 'Other' },
];

async function main() {
  const out = {};

  // 1. The context request fails. The error must survive; a failed load is not
  //    an empty assignment list.
  routes = { '/api/me/consultants': { ok: false, status: 500, body: { detail: 'boom' } } };
  await context.loadConsultants();
  out.context_failure = banner();

  // 2. The context request succeeds but the caller genuinely has no context.
  routes = { '/api/me/consultants': { body: { consultants: [], total: 0 } } };
  await context.loadConsultants();
  out.context_empty = banner();

  // 3. The ordinary case: a context is selected and no banner is shown.
  routes = { '/api/me/consultants': { body: { consultants: CONSULTANTS, total: 2 } } };
  await context.loadConsultants();
  out.context_ok = Object.assign(banner(), {
    selected: document.getElementById('consultantSelect').value,
  });

  // 4. Badges. The selected consultant is CONSULTANTS[0]; a shared job on which
  //    only CONSULTANTS[1] has acted must not badge as the selected one's.
  const job = (reviews, applications) => ({
    id: 'job-1',
    title: 'Shared Role',
    status: 'open',
    company: '',
    location: '',
    reviews,
    applications,
  });

  const otherActed = context.jobRow(job({ 'id-other': 'reject' }, {}));
  out.badges_other_consultant_only = {
    rejected_badge: otherActed.includes('pill rejected'),
    applied_badge: otherActed.includes('pill applied'),
    other_chip: otherActed.includes('pill other'),
    reject_button_disabled: /data-act="reject"[^>]*disabled/.test(otherActed),
  };

  const selectedActed = context.jobRow(job({ 'id-selected': 'reject' }, {}));
  out.badges_selected_consultant = {
    rejected_badge: selectedActed.includes('pill rejected'),
    other_chip: selectedActed.includes('pill other'),
    apply_button_disabled: /data-act="apply"[^>]*disabled/.test(selectedActed),
  };

  const selectedApplied = context.jobRow(job({}, { 'id-selected': 'applied' }));
  out.badges_selected_applied = {
    applied_badge: selectedApplied.includes('pill applied'),
    reject_button_disabled: /data-act="reject"[^>]*disabled/.test(selectedApplied),
  };

  // 5. S21: the selected consultant's decided Jobs leave the default list.
  //    Recorded from the scenario side, so the capped stub above is untouched:
  //    the page resolves `fetch` on the context at call time.
  const requested = [];
  context.fetch = (url) => { requested.push(url); return fetchStub(url); };
  const flush = () => new Promise((r) => setImmediate(r));
  const listed = (id, extra) => Object.assign({
    id, title: 'Role ' + id, status: 'open', company: '', location: '',
    reviews: {}, applications: {},
  }, extra);
  const JOBS = [
    listed('undecided'),
    listed('mine-rejected', { reviews: { 'id-selected': 'reject' } }),
    listed('mine-applied', { applications: { 'id-selected': 'applied' } }),
    listed('other-rejected', { reviews: { 'id-other': 'reject' } }),
    listed('mine-rejected-closed', { status: 'closed', reviews: { 'id-selected': 'reject' } }),
  ];
  const listRoute = { body: { jobs: JOBS, total: JOBS.length, unscored_count: 0 } };
  const shown = () => {
    const html = document.getElementById('jobList').innerHTML;
    return JOBS.map((j) => j.id).filter((id) => html.includes('data-id="' + id + '"'));
  };
  const listRun = async (show) => {
    requested.length = 0;
    document.getElementById('statusFilter').value = show;
    await context.loadJobs();
    return {
      shown: shown(),
      urls: requested.slice(),
      count: document.getElementById('jobCount').textContent,
      list: document.getElementById('jobList').innerHTML,
    };
  };

  routes = { '/api/jobs': listRoute };
  out.hide_open = await listRun('open');
  out.hide_all = await listRun('');
  out.show_rejected = await listRun('rejected');

  // Every Job decided for the selected consultant: an explanation, not
  // "No jobs yet".
  routes = { '/api/jobs': { body: { jobs: [JOBS[1], JOBS[2]], total: 2, unscored_count: 0 } } };
  out.hide_everything_decided = await listRun('open');

  // 6. The confirmation outlives the reload that removes the row. Specific
  //    routes first: the stub matches by prefix, in insertion order.
  document.getElementById('decisionMsg').className = 'result';
  routes = {
    '/api/jobs/undecided/reject': { body: { created: true } },
    '/api/jobs/undecided': { body: { id: 'undecided', title: "O'Brien <b>Role</b>", description: 'x' } },
    '/api/applications': { body: { created: true } },
    '/api/jobs': listRoute,
  };
  document.getElementById('statusFilter').value = 'open';
  await context.rejectJob('undecided');
  await flush();
  const decisionMsg = () => ({
    className: document.getElementById('decisionMsg').className,
    text: document.getElementById('decisionMsg').textContent,
  });
  out.reject_confirmation = decisionMsg();
  await context.loadJobs();       // any later reload leaves it in place
  out.reject_confirmation_after_reload = decisionMsg();
  await context.applyJob('undecided');
  await flush();
  out.apply_confirmation = decisionMsg();

  // Switching the picker clears it: it names the consultant it was for. The
  // change handler also touches a timer and auth-guard's nav; both are
  // supplied here as no-ops so the capped stub above stays as it is.
  context.clearTimeout = () => {};
  context.renderTopNav = () => {};
  document.getElementById('consultantSelect').listeners.change({ target: { value: 'other.consultant' } });
  await flush();
  out.confirmation_after_switch = decisionMsg();

  // 7. With no consultant selected, nothing is hidden (runs last: it clears
  //    the context the scenarios above depend on).
  routes = { '/api/me/consultants': { body: { consultants: [], total: 0 } } };
  await context.loadConsultants();
  routes = { '/api/jobs': listRoute };
  out.no_consultant_open = await listRun('open');
  out.no_consultant_rejected = await listRun('rejected');

  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
