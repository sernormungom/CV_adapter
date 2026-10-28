#!/usr/bin/env bash
# Prove the dev container is a complete test environment, and fail loudly on
# any gap. Run inside the `dev` service of .devcontainer/compose.yml:
#
#   docker compose -f .devcontainer/compose.yml exec dev scripts/devcontainer-smoke.sh
#
# Every check exists because its absence was once found the hard way, during
# real work. In particular a *skip* is treated as a failure: an absent Node or
# database skips tests, and a skipped gate reads exactly like a passed one.
set -euo pipefail

cd "$(dirname "$0")/.."

SMOKE_T0=$(date +%s)
declare -a SUMMARY=()

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; STEP_NAME=$*; STEP_T0=$(date +%s); }
ok() {
  local secs=$(( $(date +%s) - STEP_T0 ))
  echo "-- ok (${secs}s)"
  SUMMARY+=("${STEP_NAME} — ok, ${secs}s${1:+ — $1}")
}

: "${CV_ADAPTER_TEST_DATABASE_URL:?must be set (compose.yml sets it)}"
[ "${CV_ADAPTER_REQUIRE_INTEGRATION:-}" = "1" ] \
  || fail "CV_ADAPTER_REQUIRE_INTEGRATION must be 1, or an absent database skips instead of failing"
[ "${DATABASE_URL:-}" = "$CV_ADAPTER_TEST_DATABASE_URL" ] \
  || fail "DATABASE_URL must name the test database here (as in ci.yml)"

# ── 1. versions, and one interpreter ─────────────────────────────────────────
step "1. tool versions"
python --version
python -m pytest --version
alembic --version
echo "node $(node --version), npm $(npm --version)"
python -m playwright --version
psql --version
git --version
echo "claude: $(claude --version)"
echo "codex: $(codex --version)"

# `python`, `pytest` and `alembic` must be one interpreter (S26 ran pytest
# from a Python that was not the one with the dependencies).
py_real=$(readlink -f "$(command -v python)")
for tool in pytest alembic; do
  shebang=$(head -n1 "$(command -v "$tool")")
  tool_py=$(readlink -f "${shebang#\#!}")
  [ "$tool_py" = "$py_real" ] || fail "$tool runs on $tool_py, python is $py_real"
done
echo "python, pytest, alembic all resolve to $py_real"
ok

# ── 2. the test database is up, on loopback ──────────────────────────────────
step "2. pg_isready on localhost"
for _ in $(seq 1 60); do
  pg_isready -q -h localhost -p 5432 -U cvtest -d cvtest && break
  sleep 1
done
pg_isready -h localhost -p 5432 -U cvtest -d cvtest || fail "test Postgres not ready on localhost:5432"
ok

# ── 3. the deploy's migration sequence, as ci.yml runs it ────────────────────
step "3. alembic upgrade head && alembic check"
alembic upgrade head
alembic check
ok

# ── 4. the integration gate's safety check still refuses what it should ──────
# The container makes Postgres appear on localhost so the check passes; this
# proves that did not happen by loosening it.
step "4. integration target allowlist"
python - <<'PY'
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "itest_conftest", "tests/integration/conftest.py"
)
conftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conftest)

conftest._assert_target_is_an_allowed_test_service(
    os.environ["CV_ADAPTER_TEST_DATABASE_URL"]
)
print("accepted:", os.environ["CV_ADAPTER_TEST_DATABASE_URL"].split("@")[1])

for refused in (
    "postgresql+psycopg://cvtest:cvtest@db:5432/cvtest",
    "postgresql+psycopg://cvtest:cvtest@10.0.0.5:5432/cvtest",
    "postgresql+psycopg://cvtest:cvtest@localhost:5432/cvadapter",
):
    try:
        conftest._assert_target_is_an_allowed_test_service(refused)
    except conftest.NotDisposableError:
        print("refused: ", refused.split("@")[1])
    else:
        raise SystemExit(f"the allowlist accepted {refused}")
PY
ok

# ── 5. headless Chromium actually launches ───────────────────────────────────
step "5. headless Chromium"
python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.set_content("<title>smoke</title><p id='x'>rendered</p>")
    assert page.title() == "smoke", page.title()
    assert page.inner_text("#x") == "rendered"
    print("chromium", browser.version, "rendered a page")
    browser.close()
PY
ok

# ── 6. files written to the bind mount belong to the host user ───────────────
step "6. bind-mount ownership"
probe=$(mktemp -p /workspace .devcontainer-smoke-owner.XXXXXX)
owner=$(stat -c '%u:%g' "$probe")
rm -f "$probe"
[ "$owner" = "$(id -u):$(id -g)" ] || fail "new file owned by $owner, expected $(id -u):$(id -g)"
[ "$(id -u)" != "0" ] || fail "running as root"
echo "new file owned by $owner ($(id -un))"
ok "$owner"

# ── 7. every gate, for real ──────────────────────────────────────────────────
step "7. make test-complete"
junit=$(mktemp --suffix=.xml)
# A real ANTHROPIC_API_KEY must never reach the suite; unset, the root
# conftest.py substitutes its dummy.
set +e
env -u ANTHROPIC_API_KEY CV_ADAPTER_REQUIRE_INTEGRATION=1 \
  PYTEST_ADDOPTS="-rs --junitxml=$junit" make test-complete
gate_rc=$?
set -e

# Parse the outcome rather than trusting the exit code alone: exit 0 with
# skips is exactly the failure mode this script exists to catch.
gate_summary=$(python - "$junit" <<'PY'
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

cases = ET.parse(sys.argv[1]).getroot().iter("testcase")
totals = Counter()
ran_by_module = Counter()
skips = []
for case in cases:
    module = case.get("classname", "")
    if case.find("skipped") is not None:
        totals["skipped"] += 1
        skips.append(f"{module}::{case.get('name')}: {case.find('skipped').get('message')}")
        continue
    if case.find("failure") is not None:
        totals["failed"] += 1
    elif case.find("error") is not None:
        totals["errors"] += 1
    else:
        totals["passed"] += 1
    ran_by_module[module] += 1

problems = []
if skips:
    problems.append(f"{len(skips)} skipped:\n  " + "\n  ".join(skips))

# The Node client behaviour modules and the integration package must each
# have run tests, not merely collected and skipped them.
behaviour = sorted(
    "tests." + p.stem for p in Path("tests").glob("test_*_behaviour.py")
)
for module in behaviour:
    if ran_by_module[module] == 0:
        problems.append(f"{module} ran no tests (Node missing?)")
integration = sum(n for m, n in ran_by_module.items() if m.startswith("tests.integration."))
if integration == 0:
    problems.append("no integration test ran")

print(
    f"passed={totals['passed']} failed={totals['failed']} errors={totals['errors']} "
    f"skipped={totals['skipped']}; integration ran={integration}; "
    f"node behaviour modules ran={sum(ran_by_module[m] for m in behaviour)} "
    f"tests across {len(behaviour)} modules"
)
if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(1)
PY
) || { echo "$gate_summary"; fail "test-complete outcome check failed (see above)"; }
rm -f "$junit"
echo "$gate_summary"
[ "$gate_rc" -eq 0 ] || fail "make test-complete exited $gate_rc"
ok "$gate_summary"

# Per-gate sizes: collection only, so the suite still runs exactly once.
step "7b. per-gate collected counts"
per_gate=""
for gate in fast static integration; do
  n=$(python -m pytest -m "$gate" --collect-only -q 2>/dev/null | tail -n1)
  echo "$gate: $n"
  per_gate+="$gate: ${n%% in *}; "
done
ok "$per_gate"

echo
echo "== SMOKE PASSED in $(( $(date +%s) - SMOKE_T0 ))s"
printf '  %s\n' "${SUMMARY[@]}"
