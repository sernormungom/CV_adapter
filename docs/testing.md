# Testing gates

**Status:** active maintainer documentation. These gates are non-negotiable.

This page is for the occasional maintainer: which command to run, what it
proves, and what it needs installed.

---

## Test commands and CI gates

| Gate | Command | Needs | What it proves |
| --- | --- | --- | --- |
| **fast** | `make test-fast` | nothing | Application logic against in-memory SQLite. The loop to run while editing. |
| **static** | `make test-static` | nothing (Node optional) | The static client (HTML/JS/CSS) is served and still carries its contract markers. With Node present it also *runs* the Jobs page's script — see below. |
| **integration** | `make test-integration` | Docker | Production database semantics on real Postgres 16 — migrations, JSONB, partial indexes, transactions, concurrency, `SKIP LOCKED`. |
| **complete** | `make test-complete` | Docker | Local aggregate of fast, static, and integration tests. |

`make test` is an alias for `make test-complete`.

Run gates directly with pytest if you prefer: `pytest -m fast`,
`pytest -m static`, `pytest -m integration`, `pytest -m complete`.

### Codex workspace-write sandbox

The default network-disabled Linux `workspace-write` sandbox blocks the
`sendto` syscall for all sockets, including the Unix socket pair asyncio uses
to wake Starlette's in-process `TestClient` portal. The repository-root
`conftest.py` detects Codex's `CODEX_SANDBOX_NETWORK_DISABLED=1` marker and
uses `os.write` for that one local wake-up instead. This preserves the sandbox's
network restriction: static and fast tests still have no external-service
requirement and make no network connection.

Do not enable sandbox network access merely to run the test gates. The
workaround is test-only and does not load in the application process.

---

## Which command to run when

`complete` contains every test selected by the other three commands. Never run
all four locally in sequence merely to reconcile counts; that executes the test
suite twice without adding behavioral evidence. CI runs the fast, static,
integration, migration, and Docker checks independently before an image can be
built.

| Moment | Run | Why |
| --- | --- | --- |
| Editing | `pytest -m fast -k <area>` | Seconds. The loop. |
| Finished a change | `make test-fast` | Catches what your `-k` filter did not select. |
| Touched a client page | `+ make test-static` | The HTML/JS contract, and Node behaviour tests. |
| Touched schema, a lock, a transaction, or concurrency | `+ make test-integration` | SQLite cannot answer those questions. |
| **Handing a change to review** | the gates relevant to the changed surface | A change needs evidence for what it changed, not a duplicate release rehearsal. |
| Cross-cutting local release rehearsal | `make test-complete` once | Useful before a demo or when a change spans several gate categories. |
| **Correction round** | a targeted subset plus the one gate covering the changed risk | The reviewer re-runs what it doubts. |
| Pull request or deployable commit | repository CI | CI is the authoritative full release check and cannot skip a gate. |

A **mutation check**—temporarily breaking a fix and watching its regression test
fail—is useful when a test's sensitivity is not obvious or a reviewer doubts
it. It is not mandatory ceremony for every regression test.

A **live HTTP demo** against real Postgres is useful for a changed wire contract
or a release demo. Do not re-author one for a correction that changes no route.
If the check has continuing value, keep it as an integration test.

### What actually costs time, measured

Worth knowing before optimising the wrong thing (measured September 2026, WSL,
repo on `/mnt/c`):

- **bcrypt was ~85% of the fast gate.** At the production work factor of 12 one
  hash costs ~0.24s; the `actors` fixture hashes four per test and each login
  verifies one, so per-test *setup* was ~1.0s and the gate was ~371s. The suite
  now runs at 4 rounds (`BCRYPT_ROUNDS`, set in the repository-root
  `conftest.py`), setup is ~0.10s, and the gate is ~57s. `backend/config.py`
  refuses a cost below 12 in production and refuses it anywhere without the
  same escape hatch `AUTH_ENFORCED=false` needs; `tests/test_auth.py` pins the
  floor, the default, and a real 12-round round-trip, so the production cost is
  still exercised.
- **`pytest-xdist` is not worth adding here.** Measured 54.3s with `-n auto`
  (8 workers) against 56.8s serial for the same 478 tests: each worker pays
  ~12s of interpreter and import startup, because the venv lives on the 9p
  `/mnt/c` mount. Re-measure before reaching for it again — on a native
  filesystem the arithmetic would be different.
- **Process startup is ~7s of I/O wait**, not CPU (`import backend.main`: 7.3s
  wall, 1.3s user). That is the 9p mount. A venv on the WSL-native filesystem
  would cut it; nothing in the repository depends on where the venv lives.
- **The dev container** (measured 1 October 2026, same machine, 812 tests):

  | Setup | `make test-fast` | `make test-complete` |
  | --- | --- | --- |
  | WSL venv, checkout on `/mnt/c` | 79–81s | n/a |
  | Dev container, checkout on `/mnt/c` | 73–79s | 204s |
  | Dev container, checkout on the WSL-native filesystem | 63–65s | 158s |

  Moving the interpreter off the 9p mount buys most of what is available. A
  native checkout saves a further ~15% on `fast` and ~25% on `complete`.

The one environment where every gate runs for real, with Node, Chromium,
Postgres 16 and no skips, is the dev container (`docs/running.md` → Dev
container). `scripts/devcontainer-smoke.sh` runs `make test-complete` there and
fails if **any** test skipped. Without it, install the test tooling once, into
the WSL venv:

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
```

---

## How a test gets its marker

You do not annotate tests by hand. The repository-root `conftest.py` applies
markers during collection:

- a test under `tests/integration/` → `integration`
- a module with `pytestmark = pytest.mark.static` → `static`
- anything that is neither → `fast`
- **every** test → `complete`

So `-m complete` is a real selector covering the whole suite, not a label with
no members. `pytest.ini` sets `--strict-markers`, so a typo in a marker name is
an error rather than a silent "0 tests selected, all green".

Adding a test needs no marker work. Adding a *new kind* of gate does: register
the marker in `pytest.ini` and teach `pytest_collection_modifyitems` how to
apply it.

### Client behaviour, not just client markers

Most of the `static` gate asserts that markers appear in the served HTML. That
cannot see a **control-flow** defect, and V2 Slice 2's review found two of
exactly that shape: a consultant-context load failure whose error banner was
immediately overwritten by the "ask an admin" message, and decision badges
rendered from the whole visible map rather than the selected Consultant. Both
were green under text assertions.

`tests/test_jobs_client_behaviour.py` runs the Jobs page's own inline script
under a stub DOM (`tests/client/jobs_client_probe.js`) and asserts on what it
actually did. No browser, no framework, no build step — a few dozen lines of
stub drives the functions directly.

It needs Node, which is **not** a documented requirement of this gate, so an
absent interpreter *skips* the module the way absent Docker skips the
integration gate. CI runners ship Node, so it runs there. If you add a
behavioural client test, put its scenario in the probe and assert on the JSON it
prints.

**The stub is capped, and the cap is enforced.** A hand-rolled DOM agrees with a
real browser only on the cases someone thought about; past a certain size it
starts asserting fiction, and a green gate that asserts fiction is worse than no
gate. The failure mode this guards is not the loud one — a missing DOM method
raises a `TypeError` naming itself — but the quiet one, where a stub is taught
another browser *behaviour* to turn one red test green, and every later test
inherits the guess.

So four guards at the foot of `tests/test_jobs_client_behaviour.py` hold the
boundary rather than merely documenting it. They pin the stub element's and
document's members to an allowlist, cap the stub's code at 90 lines (77 today),
and permit exactly one branch on element identity — the `<select>` value
adoption that already exists. They need no Node, so unlike the behavioural tests
they never skip. Each was verified by mutation: adding a `querySelector`, a
second identity branch, a `document.querySelectorAll`, or 40 lines of padding
each fails exactly its own guard.

Adding **scenarios** is free and is what the harness is for. Widening the stub
is a decision: take it deliberately, or — when the page genuinely needs more DOM
than this — replace the harness with a browser one instead of growing this into
a bad imitation of one.

---

## Isolated data roots

`backend/config.py` reads `DATA_DIR`, `JOB_STORE_DIR`,
`APPLICATION_TRACKER_DIR`, `TA_CONFIG_DIR` and `CV_PUBLISH_PHOTO_DIR` from the
environment **and creates those directories at import time**. (`JOB_STORE_DIR`
and `TA_CONFIG_DIR` no longer have a reader — the JSON Job Store went with V2
Slice 1 and the Cockpit's config with Step 1. Both constants stay because
`tests/test_test_isolation.py` asserts on all five roots, and that test is not
weakened to shorten a list.) The shipped
defaults are repo-relative (`data/profiles`, `data/job_store/jobs`, ...), so
before M1 a bare `pytest` run materialised and could write into the real data
tree — and any test that forgot to monkeypatch a path wrote to live data and
still passed.

The repository-root `conftest.py` now points all five at a throwaway directory
before `backend.config` is ever imported. That is why the file sits at the root
rather than under `tests/`: pytest imports conftest files from the rootdir
downwards, so it runs before `tests/conftest.py` pulls in `backend.main`.

- `tests/test_test_isolation.py` fails if any data root resolves back inside
  the repository. Do not weaken it.
- `CV_ADAPTER_TEST_DATA_ROOT=/some/path` chooses *where* the isolated root
  goes, for debugging what a run wrote. It cannot disable isolation.
- A root the suite created is deleted at session end; a root you named is left
  alone.

`ANTHROPIC_API_KEY` is defaulted to a dummy value for the same reason: no test
should reach the network, and a slip should fail at the call site rather than
spend money on a real account.

---

## Fixtures

### Job postings

`tests/fixtures/jobs/` holds committed, sanitized job postings. Load them with
`tests.fixtures.job_text(...)`. **Never read `data/` from a test** — that is the
G-037 failure mode this replaced.

Two files, covering both identity branches of the job-record contract:

- `email_posting_with_native_id.txt` — a forwarded job-lead email with a
  `Job Posting ID` line (the `native:` branch)
- `plain_posting_no_native_id.txt` — no source-owned identifier (the
  `content:` body-hash branch)

Read `tests/fixtures/jobs/PROVENANCE.md` before touching either. They are
contract fixtures: changing a byte changes the derived `identity_key` and
`job_id`, which is what the tests assert on. They are also the only job corpus
that survives M0, and
`tests/test_job_fixtures.py::test_fixtures_carry_no_real_identifiers` keeps them
non-personal. (That module was `tests/test_ingest.py` until V2 Slice 1 retired
the paste-ingest endpoint it also covered; the fixture guards outlived it.)

### Authorization actors

`tests/conftest.py::actors` builds the standard disposable cast in the test's
own throwaway database:

| Actor | Roles | Relationship |
| --- | --- | --- |
| `actors.ta` | TA | serves `actors.consultant` |
| `actors.consultant` | Consultant | assigned to `actors.ta` |
| `actors.other_ta` | TA | **no** relationship — the negative case |
| `actors.admin` | **Admin + Consultant** | global |

```python
def test_something(actors, client):
    actors.login(client, actors.ta)
    ...
```

`actors.admin` holds **two** roles since V2 Step 1, so the multi-role case
(P-004) is exercised by default rather than only where a
test remembers to construct it. Roles are a membership: assert with
`user.has_role(...)` or `user.roles`, never by comparing `user.primary_role`,
which is a display/landing hint. There is no `user.role` column any more.

A companion fixture, `second_pair`, builds a **second populated** TA↔Consultant
relationship (`{"ta": Actor, "consultant": Actor}`). The standard cast has only
one populated pair plus an unrelated TA, so questions about *shared* objects —
a Job two Consultants both reach, an Application one of them owns — cannot be
asked with `actors` alone. That gap is why the original Slice 1 suite missed a
cross-Consultant disclosure; reach for `second_pair` whenever a test needs two
ordinary non-Admin Consultants on the same object.

Each `Actor` carries `user` (a fully-loaded, detached `User`), `user_id`,
`username`, `email`, and `password`. The password satisfies the account
password policy in force — 12 characters with an uppercase letter, a number,
and a symbol, and not the account's own username or email local part (STAB-2;
the earlier note here describing D-014 as a 15-character policy was stale, and
15 is the pilot *seed*'s bootstrap-only bar). `tests/test_authz_fixture.py`
asserts the fixture password against
`backend.auth.service.validate_password_policy` itself rather than a copied
number, so a later policy change surfaces as one failing assertion instead of
every login test.

The `Session` fixture seeds one `GBG` Site into each throwaway database,
because provisioning an account now requires an active Site
(`backend/sites/service.py`). That is fixture-local state, not the
`make seed-pilot` command — no test depends on the pilot cast existing. The
`actors` fixture (built on `Session`) adds a second Site (`MAL`) and moves
`other_ta` onto it — the two-Site pair most cross-Site authorization tests
need (S1, [authz.md](architecture/areas/authz.md) G3).

Nothing is seeded into a shared or long-lived environment. Staging may seed the
same shape. A *temporary* TA in a production canary needs explicit separate
authorization, and a third permanent production identity is not allowed
(M1/D-012).

### Profiles, and the four bindings that have to move together

A test that exercises scoring or CV generation needs a real `profile.md` under
an isolated root. Redirecting `backend.config.DATA_DIR` alone is **not enough**:
three modules bind the path with `from backend.config import DATA_DIR` at import
time and would keep pointing at the real root while the test quietly exercised
nothing. `tests/test_tailored_cv.py::profiles` patches all four —
`config.DATA_DIR`, `backend.profile_reader.DATA_DIR`,
`backend.matching.config_reader.DATA_DIR`, and
`backend.cv_pipeline.context_assembler.DATA_DIR`. Copy that fixture rather than
a subset of it. (`backend/cv_state/service.py`, added in Slice 3, deliberately
reads `config.DATA_DIR` through the module instead, so it follows a single
patch.)

CV import tests need a different, smaller pair:
`backend.cv_importer.experience_writer.DATA_DIR` (bound at import time, the
same trap) plus `config.DATA_DIR` (which covers `backend/cv_importer/service.py`
and `backend/cv_importer/preview.py`, both of which read it through the
module). `tests/test_cv_import.py::import_profiles` is that fixture; the
tailored-CV `profiles` fixture patches modules CV import never touches, so
copying it would work but patch more than necessary.

### Stubbing the model

Tests never reach the network — `ANTHROPIC_API_KEY` is a dummy so a slip fails
at the call site. Stub the model as narrowly as possible: `tests/test_tailored_cv.py`
replaces `backend.tailored_cv.handler.generate_cv_content` and nothing else, so
the cv-content mapping, the state builder, and the handler's atomic effect all
run for real. A stub placed higher — at the handler itself — would leave the
part most likely to break untested.

CV import (`tests/test_cv_import.py`) has **two** LLM call sites, not one, and
a test exercising a re-import needs both stubbed or it reaches the real
network on the second: `handler.extract_experience` (always called) and
`backend.cv_importer.experience_writer.merge_profiles` (called only when
`write_profile` finds an existing `profile.md` — i.e. only on a second-or-later
import for the same consultant). The fast suite's first pass at this stubbed
only the first and got an `AuthenticationError` from a real
`https://api.anthropic.com` call on the retry-after-success scenario; copy
`stub_extraction` in that file rather than a subset of it.

---

## Integration gate details

`tests/integration/` runs against a disposable Postgres 16 container that the
suite starts and destroys itself — never against a database you care about.

These tests `CREATE DATABASE`, `DROP DATABASE`, and `pg_terminate_backend`, so
an externally supplied `CV_ADAPTER_TEST_DATABASE_URL` is checked against a
positive allowlist before any connection opens: **loopback host only**, and the
database must be one this project provisions (`cvtest`, or a gate-created
`itest_<12 hex>`). Remote and external URLs are unsupported with no opt-out. A
target that clears that is then refused if any business table already holds
rows. Want your own throwaway database? Unset the variable and let the Docker
path create one.

See `tests/integration/README.md` for the full contract and what each test
proves.

If Docker is unavailable the integration tests **skip** rather than fail
locally, so `make test-complete` still works on a machine without Docker. CI
does not get that latitude: the integration job there has a real Postgres
service and `CV_ADAPTER_REQUIRE_INTEGRATION=1`, which turns an unavailable
database into a hard failure.

---

## CI

`.github/workflows/ci.yml` holds the gates. It runs directly on **pull
requests** and manual dispatch; it does **not** run directly on pushes to
`main` — a deployable push reaches the same gates through
`.github/workflows/build-push.yml`, which calls `ci.yml` as a reusable workflow
and declares `needs: gates`. So an image is never built or pushed unless every
gate passed, and each commit is gated once rather than twice. See `DEPLOY.md`.

Do not weaken a gate to unblock a change (M1 rollback rule).

---

## Manual pilot walkthrough

The automated gates above prove behavior; they cannot prove a browser actually
does the right thing with a real response. Run this walkthrough by hand
against the local stack (with the real `make seed-pilot` cast) before any live
demo, and whenever a change touches the editor, the Jobs client, or the public
viewer. It is not a substitute for the automated gates and is not itself a
gate — nothing runs it in CI.

1. Sign in as the TA (`patrik.artman`), then as the Admin+Consultant
   (`norberto.munoz`).
2. Import a CV asynchronously: the `202`, visible stage/progress, completion,
   and the preview staged into the form. There is no Save button in this
   editor — confirm the staged facts land in `profile.md` via the existing
   30-second autosave, not a missed explicit step.
3. Create a Job manually (`+ Job`), run the advisory score for both
   consultants, and confirm a missing/unscorable config returns `scored:
   false` with a reason rather than hiding or rejecting the Job (invariant 7).
4. Reject one pair; Apply on another. Confirm the pair holds one live decision
   and Apply is a real row with a status. Each decided Job leaves that
   consultant's Jobs list (S21): find the rejected one under Show → Rejected
   jobs, and the applied one on Applications, where its title opens the
   description.
5. Generate the tailored CV for the Application, edit it, then return to the
   base CV and confirm it did not move (P-009). Reopen the tailored CV, note
   the revision in its banner, and leave it idle past two autosave ticks
   (over 60 seconds). Reload once and idle again. The revision must not move
   without an edit (S22): it used to climb on every 30-second tick.
6. Download the PDF from the editor and confirm no internal banner prints.
7. Publish the tailored CV, open `/cv/view#<token>` in an unauthenticated
   browser session, and confirm the view lands on the TA dashboard as "Viewed
   by hiring manager" with a view count.
8. **Click Download PDF in that unauthenticated viewer and confirm the print
   dialog actually opens.** Keep this step permanently: a stub-DOM or markup
   assertion can show an inline handler is gone and that the external script
   owns the action, but only a real browser against the real `/cv/view`
   response can show the CSP does not also block the *replacement* handler —
   `S5-P2-R1` found exactly that gap survive two review rounds because the CSP
   and the button were each asserted in tests that never met
   (`tests/test_static_pages.py::test_the_hardened_viewer_shell_has_no_inline_event_handler_its_csp_would_kill`).
9. As the TA or the Admin+Consultant, select a Consultant in the editor (via
   `?consultant_id=` or the nav), confirm the top nav carries that context,
   then close the tab and reopen `html/cv-importer.html` with **no**
   `?consultant_id=` at all. Confirm the top nav shows no consultant context
   — a stale `localStorage` value from the prior selection must not carry
   forward (V3-P-012/S7). Also exercise the deferred S6 acceptance-5
   evidence here: fabricated multi-Site Consultant and TA navigation
   matrices, switching between two Consultants, and an invalid/revoked
   `?consultant_id=` context.

Record a run's outcome with the change or release it supports (the pull
request, or a dated file under `docs/evidence/` if it must outlive that). This
section owns only the reusable script, not any one run's results. Record a step
that was skipped or waived as such, not as passed.
