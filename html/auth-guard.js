/* Shared client-side auth guard (Phase 2e).
 * All requests are same-origin so the httpOnly session cookie is sent.
 * Server-side enforcement (AUTH_ENFORCED) is the real gate; this guard drives
 * the UX (redirect to login, role-correct landing, seed identity). */

const LOGIN_URL = '/html/login.html';

/* Roles are a many-to-many membership (V2 Step 1): /api/auth/me returns
 * `roles` (every role held — the authoritative field) plus `primary_role`
 * (display/landing hint only). Always test membership with hasRole, never
 * compare primary_role: an Admin+Consultant holds both. */
function rolesOf(me) {
  return (me && Array.isArray(me.roles)) ? me.roles : [];
}

function hasRole(me, role) {
  return rolesOf(me).includes(role);
}

function hasAnyRole(me, roles) {
  const held = rolesOf(me);
  return roles.some(r => held.includes(r));
}

function landingFor(me) {
  if (hasRole(me, 'admin')) return '/html/admin-dashboard.html';
  // The TA surface is named Consultants (V3-P-003); html/ta-dashboard.html
  // survives only as a redirect shim, because the server's own post-login
  // landing (backend/auth/routes.py::_LANDING) is still keyed to that path.
  if (hasRole(me, 'ta')) return '/html/consultants.html';
  return '/html/cv-importer.html';
}

async function fetchMe() {
  try {
    const r = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

/* Require a session. If `allowedRoles` is given and the user holds none of
 * them, send them to their own landing. Returns the user, or never resolves
 * (redirects) when unauthenticated/unauthorized. */
async function requireAuth(allowedRoles) {
  const me = await fetchMe();
  if (!me) {
    window.location.href = LOGIN_URL;
    return new Promise(() => {});
  }
  if (allowedRoles && !hasAnyRole(me, allowedRoles)) {
    window.location.href = landingFor(me);
    return new Promise(() => {});
  }
  return me;
}

async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } finally {
    window.location.href = LOGIN_URL;
  }
}

/* ── Role-specific top-banner navigation (V3-P-003, S6; S15) ──
 * One shared destination matrix so every first-party surface's nav agrees,
 * rather than each page hand-writing its own copy. A surface never links to
 * itself. SuperCV is a real consultant destination, not a disabled Base-CV
 * placeholder, so an authorized consultant can reach its read-only profile. */
const NAV_ORDER = {
  consultant: ['Jobs', 'Applications', 'Base CV', 'SuperCV', 'Published CVs'],
  ta: ['Jobs', 'Consultants', 'Applications', 'Published CVs'],
};
const NAV_HREF = {
  'Jobs': '/html/jobs.html',
  'Base CV': '/html/cv-importer.html',
  'SuperCV': '/html/super-cv.html',
  'Applications': '/html/applications.html',
  'Published CVs': '/html/published-cvs.html',
  'Consultants': '/html/consultants.html',
};

/* The destinations `currentSurface` links to, in V3-P-003's order, for one
 * role's matrix. `role` is 'consultant' or 'ta' — not an account, which may
 * hold both; the caller picks which matrix applies (renderTopNav below). */
function navDestinations(role, currentSurface) {
  const order = NAV_ORDER[role];
  if (!order) return [];
  const out = [];
  order.forEach(name => {
    if (name === currentSurface) return;
    out.push({ label: name, href: NAV_HREF[name] });
  });
  return out;
}

/* ── Consultant context resolution (V3-P-003, S6 correction) ──
 * `GET /api/me/consultants` is the one source of "which Consultant contexts
 * this caller may act in" (backend/authz/routes.py) — the same set the server
 * authorizes against. Every page that carries a Consultant context forward
 * (`?consultant_id=`, a stashed localStorage value, …) must resolve it through
 * this helper before using or forwarding it: a client-supplied value that
 * is not in the returned set is never trusted, and a failed lookup is not an
 * empty/valid one. Both cases return `consultant: ''` — the caller clears
 * rather than acting on an unauthorized guess. */
async function resolveConsultantContext(wanted) {
  const options = [];
  const consultantIds = {};
  let error = '';
  try {
    const r = await fetch('/api/me/consultants', { credentials: 'same-origin' });
    if (!r.ok) throw new Error('Request failed (' + r.status + ')');
    const data = await r.json();
    (data.consultants || []).forEach(c => {
      options.push(c.username);
      consultantIds[c.username] = c.id;
    });
  } catch (e) {
    error = 'Could not load your consultant list: ' + e.message;
  }
  const consultant = (wanted && options.indexOf(wanted) !== -1) ? wanted : '';
  return { options, consultantIds, consultant, error };
}

/* Renders `me`'s top-banner nav into `el`. A TA always sees the TA matrix
 * (Consultants, not Base CV) even when also Consultant/Admin; otherwise a
 * Consultant sees the Consultant matrix. An Admin holding neither role sees no
 * matrix — V3-P-003 defines only these two, and Admin's own surfaces (the
 * provisioning dashboard) are not part of it.
 *
 * `consultantUsername`, when given, is carried as `?consultant_id=` on every
 * link so a selected Consultant context survives navigation. This grants
 * nothing: the destination page re-validates it against its own
 * `GET /api/me/consultants` exactly as this page did, so a link can carry a
 * context forward but never authorize one the server would refuse. */
function renderTopNav(el, me, currentSurface, consultantUsername) {
  if (!el) return;
  const role = hasRole(me, 'ta') ? 'ta' : (hasRole(me, 'consultant') ? 'consultant' : null);
  const dests = role ? navDestinations(role, currentSurface) : [];
  el.innerHTML = dests.map(d => {
    if (!d.href) {
      return '<span class="hbtn nav-disabled" title="Not available yet">' + d.label + '</span>';
    }
    const q = consultantUsername ? '?consultant_id=' + encodeURIComponent(consultantUsername) : '';
    return '<a class="hbtn" href="' + d.href + q + '">' + d.label + '</a>';
  }).join('');
}
