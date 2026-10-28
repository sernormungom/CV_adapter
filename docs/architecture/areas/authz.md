# Identity, roles, and authorization

**Owns:** `backend/authz/`, `backend/auth/`, `backend/sites/`,
`backend/bootstrap/pilot.py`, tables `users`, `user_roles`, `ta_consultants`,
`sites`.
**Decisions:** P-004 (the fabricated pilot cast), P-005 (Site is not
authorization); invariant 6a (reversed for the Jobs/matching pipeline by
V3-P-007/V3-P-008 — see G3); P-018 (Slice 6 discarded, taking the token path
with it — G8).
**Read alongside:** [lia.md](../../legal/lia.md) and
[dpia.md](../../legal/dpia.md) for why several of these guards are legal
commitments rather than engineering preferences.

Every area's authorization guard ultimately calls into this one. The two
conventions that differ per area (404-secrecy versus 403) are documented where
they apply — [jobs.md](jobs.md) G7, [applications.md](applications.md) G6,
[cv-artifacts.md](cv-artifacts.md) G10, [cv-import.md](cv-import.md) G9,
[cv-publish.md](cv-publish.md) G11.

---

## Contract

| File | Responsibility |
|---|---|
| `authz/policy.py` | **The** authorization policy. Every access decision. Its module docstring records the two invariants it exists to hold. |
| `authz/dependencies.py` | The FastAPI wrappers that apply it: `require_roles(...)`, `require_username_access`, `check_consultant_access`. Replaces the deleted `backend/tenancy/{authz,dependencies}.py`. `require_consultant_access` was deleted in S1 — it had zero callers since S2 removed the last route mounted behind it (V3-O-007); its logic (`check_consultant_access`) keeps direct unit coverage in `tests/test_auth_enforcement.py`. |
| `backend/tenancy/routes.py` | **Still exists** — serves `/api/ta/...` (TA↔Consultant assignment listings). Only that package's `authz.py`/`dependencies.py` were deleted. See G4 for why a client must not use it to pick a consultant context. |
| `authz/routes.py` | `GET /api/me/consultants` — the Consultant contexts the caller may act in. |
| `auth/dependencies.py` | **Authentication only** (who is calling): session cookie. No authorization. |
| `db/models/user.py` | `User`, `UserRoleAssignment` (table `user_roles`), `TAConsultant`. |
| `sites/service.py` | `require_active_site(db, site_id=None, *, code=None)` resolves one active Site, explicit or unambiguous, when provisioning. The startup single-Site guard (`assert_single_active_site`) is gone — S1 provisions four Sites (GBG, MAL, STH, UPS) and there is no longer a "must be exactly one" invariant to assert at boot. |
| `bootstrap/pilot.py` + `scripts/seed_pilot.py` | The only place that creates the approved fabricated pilot cast. |

---

## Guards

### G1 — One policy module. Add a named predicate; never inline a role check

Routes call named helpers rather than comparing roles. **Do not write
`if user.has_role(...)` in a route** — add the predicate to `policy.py` so
there is one place to audit.

### G2 — Roles are a many-to-many membership, not a scalar column

Ask `user.has_role(...)` (the authorization primitive) or use `user.roles`
(frozenset). **`user.primary_role` is a display/landing hint only and must
never be an access input** — an Admin+Consultant is genuinely both, and V1's
"admin counts as a consultant" bypass is gone. `role_assignments` is
`selectin`-eager because policy helpers routinely receive **detached** `User`
instances.
*Pinned by:* `tests/test_authz_fixture.py::test_cast_has_the_expected_roles`,
`::test_detached_instances_are_fully_loaded`,
`::test_usernames_are_normalized_from_the_reserved_email_domain`.

### G3 — Site is now a legitimate predicate for the Jobs/matching pipeline; still absent from every Consultant-owned-record path (V3-P-007/V3-P-008 reverse invariant 6a there, P-005)

V2 Pilot's single-Site guard and "Site is never an authz predicate" rule held
because there was exactly one Site and no product reason to gate on it. V3
packet S1 changed the product: **Jobs distribute by Site** (four Sites — GBG,
MAL, STH, UPS), so `same_site()` is now a real, named predicate in
`policy.py`, read by `can_access_job`, `ensure_consultant_operates_job`,
`can_access_application` (creation path only — see below),
`ensure_same_site_assignment` (TA↔Consultant assignment creation), and
`auth/service.py::create_user` (a non-Admin creator's new account is pinned to
their own Site). **The boundary is deliberately narrow:** every
Consultant-owned-record path — base profile/CV, import, chat/photo,
publications, privacy export/erasure, and *reading* an existing Application
(`can_access_application`, used for the tailored-CV routes too) — still
reaches data only through `ensure_consultant_access`/`operable_consultant_ids`,
which is already narrower than any Site rule, and must **not** gain a Site
predicate (S1 brief acceptance 3; this is what keeps the CV pipeline
independent of the matching pipeline, invariant 1/ADR-001). A Site or a cached
score row never grants access on its own, and Admin authority stays
unrestricted everywhere.
*Pinned by:* `tests/test_authz_fixture.py::test_policy_does_not_read_site_for_owned_access`,
`::test_multiple_active_sites_require_explicit_provisioning_site`,
[jobs.md](jobs.md) G7/G10, [applications.md](applications.md) G6,
[cv-artifacts.md](cv-artifacts.md), [cv-import.md](cv-import.md),
[cv-publish.md](cv-publish.md) (whose own `..._does_not_read_site` tests still
hold, unchanged, for the CV pipeline).

### G4 — A client picking a consultant context reads `GET /api/me/consultants`, never `/api/ta/consultants`

`policy.operable_consultants` is the User-shaped form of
`operable_consultant_ids` — the same set the Job and Application projections
are keyed by. `/api/ta/consultants` is a **TA-assignment listing** and 400s for
an Admin without an explicit `ta_id`; that mismatch is what left the pilot
Admin+Consultant able to act only for themselves.
*Pinned by:* `tests/test_consultant_context.py::test_the_returned_id_is_the_one_the_job_projections_are_keyed_by`,
`::test_every_returned_context_is_one_the_policy_actually_allows`,
`::test_a_consultant_gets_themselves`,
`::test_a_ta_gets_the_consultants_assigned_to_them`,
`::test_an_admin_gets_every_consultant`,
`::test_a_ta_with_no_assignments_gets_an_empty_list_not_an_error`,
`::test_the_admin_still_gets_400_from_the_ta_assignment_endpoint`,
`::test_it_requires_authentication`.

V3-P-003/S6's role-specific top-banner navigation (`html/auth-guard.js`'s
`renderTopNav`/`navDestinations`, used by `html/jobs.html`,
`html/applications.html`, `html/published-cvs.html`, `html/consultants.html`,
and `html/cv-importer.html`) follows the same rule at one remove: a selected
Consultant carries forward as `?consultant_id=` on a nav link so context
survives navigation, but every destination page resolves it through
`html/auth-guard.js`'s shared `resolveConsultantContext()` — which itself calls
nothing but `GET /api/me/consultants` — before rendering, filtering, or
forwarding it (or, for Published CVs, also passing the resolved value as
`GET /api/publications`'s own `consultant_username` filter). An unauthorized,
stale, or unresolvable query value is dropped rather than trusted: a link can
carry a context forward; it cannot grant one. This closed a real gap (S6
correction round 1): the first delivery read the raw query/localStorage value
directly on Applications, Published CVs, Consultants and the CV editor, so an
invalid `?consultant_id=` could render a banner, a list filter or a forwarded
identity under a context the caller was never authorized for, even though the
underlying APIs stayed correctly scoped server-side. V3-P-012/S7 closed a
second, narrower gap in the same IIFE: a TA/Admin-only session (no
`consultant` role) that opened `html/cv-importer.html` with **no**
`?consultant_id=` at all fell through both branches untouched, leaving
whatever a prior session — or a prior consultant selection on the same
browser — had written to `localStorage`. The IIFE now clears both keys
explicitly in that case, so a fresh no-context load starts from "no identity"
rather than reusing a stale one.
`html/consultants.html` (the TA surface formerly named `html/ta-dashboard.html`)
still reads `/api/ta/consultants` for its own "my consultants" list — that is
correct here, since this page **is** the TA-assignment listing, not a
consultant-context picker for some other action — but resolves any incoming
`?consultant_id=` through `resolveConsultantContext()` purely to forward it
intact to whichever surface the caller returns to next.
*Pinned by:* `tests/client/*_probe.js` behavioural probes driven from
`tests/test_applications_client_behaviour.py`,
`tests/test_published_cvs_client_behaviour.py`,
`tests/test_consultants_client_behaviour.py` and
`tests/test_cv_identity_behaviour.py` — see those files' own docstrings for
what each scenario proves.

### G5 — An unrelated TA is forbidden, not merely given an empty result

*Pinned by:* `tests/test_authz_fixture.py::test_unrelated_ta_is_forbidden_not_merely_empty`,
`tests/test_tenancy.py::test_consultant_access_rules`,
`::test_ensure_consultant_access_raises`,
`::test_consultant_cannot_access_ta_routes`,
`::test_admin_must_specify_ta_id`,
`::test_admin_ta_id_pointing_at_a_consultant_is_refused`,
`::test_admin_ta_id_for_a_nonexistent_user_is_refused`,
`::test_assignment_cannot_be_created_under_a_non_ta`,
`::test_service_refuses_a_ta_id_that_is_not_a_ta`,
`::test_ta_consultants_listing_reflects_assignment`,
`::test_ta_can_toggle_looking_for_own_consultant`,
`::test_ta_cannot_toggle_looking_for_unassigned_consultant`.

### G6 — Auth cannot be disabled in production

`AUTH_ENFORCED` defaults **true**, is refused outright when
`APP_ENV=production`, and can only be turned off in a development/test process
that **also** sets `CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS=true`
(`backend/config.py`). `BCRYPT_ROUNDS` below 12 is refused in production for
the same reason.
*Pinned by:* `tests/test_v2_pilot_bootstrap.py::test_production_process_refuses_disabled_auth`,
`tests/test_auth_enforcement.py::test_requires_auth_when_on`,
`::test_noop_when_flag_off`,
`::test_self_allowed_others_blocked`,
`::test_consultant_scoped_route_401_when_enforced_and_no_session`,
`::test_consultant_scoped_route_403_for_other_consultant`,
`::test_profile_route_401_when_enforced`,
`::test_base_cv_state_401_when_enforced`,
`::test_base_cv_state_403_for_other_consultant`,
`::test_chat_401_when_enforced`,
`::test_profiles_list_requires_ta_or_admin`,
`::test_profiles_list_allowed_for_ta`.

### G7 — Migrations create schema only; accounts come from the seed

`bootstrap/pilot.py` is the only place that creates the approved fabricated
pilot cast (P-004). Reruns **validate rather than mutate**, and refuse to mix
with non-pilot users.
*Pinned by:* `tests/test_v2_pilot_bootstrap.py::test_pilot_seed_creates_exactly_the_approved_cast`,
`::test_pilot_seed_refuses_to_mix_with_other_accounts`,
`::test_a_long_but_non_compliant_seed_password_is_refused_before_anything_exists`.

### G8 — Retired permanently: the bearer API-token authentication path

Removed in STAB-6 with the discarded email-ingestion slice (P-018); STAB-7
removed its frontend ("API tokens" cards in the TA and Admin dashboards). **Do
not reintroduce it, or `local_tools/push_client.py`, without a new decision
reopening Slice 6.**

### Accepted limitations — do not "fix" these

- **Login throttling is not per-IP.** Nothing in `backend/` reads
  `X-Forwarded-For`, and behind the ALB `request.client.host` is the balancer —
  a per-IP limiter would collapse to a global one while looking more precise.
  Recorded by STAB-2, to be closed once for both this and
  [cv-publish.md](cv-publish.md) G9.
- **Username normalisation strips `@domain` suffixes** (invariant 6), and lives
  in **two** places: `backend/auth/service.py::normalize_username` is what the
  routes in this area call; `backend/main.py::_normalize_username` is the
  profile-directory helper invariant 6 names. Use one of them consistently; do
  not introduce raw email-address lookups against the profile directory.
