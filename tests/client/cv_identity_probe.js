/*
 * Runs only the CV editor's identity-seeding IIFE (`window.__identityReady`,
 * the first of html/cv-importer.html's two inline <script> blocks — see
 * tests/client/cv_editor_probe.js's header for why the split is exactly two)
 * under a stub, and reports the resulting localStorage state as JSON.
 *
 * Why this exists: the S6 correction round found that an admin/TA's
 * `?consultant_id=` view-as target was written to localStorage — becoming
 * the identity every base-CV read/write on the page acts as — with no check
 * that `GET /api/me/consultants` actually authorized it. A hand-typed or
 * stale target was trusted exactly like a real one, even though the design
 * intent (stated in auth-guard.js's own renderTopNav comment) is that a
 * carried context "grants nothing" until the destination re-validates it.
 * This page was the one place that skipped the re-validation.
 *
 * `hasRole`/`hasAnyRole` are reimplemented faithfully (not blanket-`true`
 * stubs, unlike tests/client/cv_editor_probe.js's fakes) because this probe's
 * whole point is the role-conditioned branch; `resolveConsultantContext` is a
 * controllable fake, since exercising the real fetch-based one is already
 * covered by tests/test_applications_client_behaviour.py and friends.
 *
 * Usage: node cv_identity_probe.js <path-to-cv-importer.html>
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const pagePath = process.argv[2];
const html = fs.readFileSync(pagePath, 'utf8');

const blocks = [...html.matchAll(/<script>\r?\n([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (blocks.length !== 2) {
  console.error('expected 2 inline <script> blocks in ' + pagePath + ', found ' + blocks.length);
  process.exit(2);
}
const identitySource = blocks[0];

function rolesOf(me) { return (me && Array.isArray(me.roles)) ? me.roles : []; }
function hasRole(me, role) { return rolesOf(me).includes(role); }
function hasAnyRole(me, roles) { const held = rolesOf(me); return roles.some((r) => held.includes(r)); }

function run({ me, search, consultantOptions, consultantLookupFails, preseed }) {
  const storage = new Map(Object.entries(preseed || {}));
  const context = {
    location: { search: search || '' },
    URLSearchParams,
    console,
    hasRole,
    hasAnyRole,
    requireAuth: async () => me,
    resolveConsultantContext: async (wanted) => {
      if (consultantLookupFails) {
        return { options: [], consultantIds: {}, consultant: '', error: 'boom' };
      }
      const options = consultantOptions || [];
      const consultant = (wanted && options.indexOf(wanted) !== -1) ? wanted : '';
      return { options, consultantIds: {}, consultant, error: '' };
    },
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(identitySource, context, { filename: path.basename(pagePath) });
  return context.__identityReady.then(() => ({
    mpya_username: storage.has('mpya_username') ? storage.get('mpya_username') : null,
    mpya_consultant_id: storage.has('mpya_consultant_id') ? storage.get('mpya_consultant_id') : null,
  }));
}

async function main() {
  const out = {};

  // 1. A plain Consultant session, no target: always their own identity.
  out.consultant_own = await run({
    me: { username: 'nora', roles: ['consultant'] },
    search: '',
  });

  // 2. A plain Consultant session with a hand-typed target: still their own —
  //    a Consultant-only account may never switch (existing behaviour, not
  //    part of this correction, kept here as the control).
  out.consultant_ignores_target = await run({
    me: { username: 'nora', roles: ['consultant'] },
    search: '?consultant_id=someone.else',
    consultantOptions: ['nora'],
  });

  // 3. A TA view-as, target authorized: seeds the target identity.
  out.ta_valid_target = await run({
    me: { username: 'the.ta', roles: ['ta'] },
    search: '?consultant_id=consultant.a',
    consultantOptions: ['consultant.a', 'consultant.b'],
  });

  // 4. The regression: a TA view-as target NOT in GET /api/me/consultants'
  //    set. Must not seed that identity — cleared, not trusted.
  out.ta_invalid_target = await run({
    me: { username: 'the.ta', roles: ['ta'] },
    search: '?consultant_id=not.mine',
    consultantOptions: ['consultant.a', 'consultant.b'],
  });

  // 5. Same, but the lookup itself fails — a failure is not a valid target
  //    either.
  out.ta_lookup_fails = await run({
    me: { username: 'the.ta', roles: ['ta'] },
    search: '?consultant_id=consultant.a',
    consultantLookupFails: true,
  });

  // 6. A dual-role Admin+Consultant with an invalid target: falls back to
  //    their own profile rather than ending up with no identity at all.
  out.dual_role_invalid_target_falls_back_to_self = await run({
    me: { username: 'the.admin', roles: ['admin', 'consultant'] },
    search: '?consultant_id=not.mine',
    consultantOptions: ['consultant.a'],
  });

  // 7. A TA/Admin-only session with no ?consultant_id= at all: any stale
  //    value from a prior session/selection on this browser must be cleared,
  //    not left in place.
  out.ta_no_target_clears_stale_identity = await run({
    me: { username: 'the.ta', roles: ['ta'] },
    search: '',
    consultantOptions: ['consultant.a'],
    preseed: { mpya_username: 'stale.consultant', mpya_consultant_id: 'stale.consultant' },
  });

  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
