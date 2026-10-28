from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

# Optional at import time: the anthropic SDK reads ANTHROPIC_API_KEY from the
# environment directly, and DB/migration tooling must import this module without
# requiring an LLM key. Validation belongs at the call site, not at import.
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")

# Database. Postgres in all environments (Docker Compose locally, RDS in cloud);
# swap targets by changing this one env var.
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://cvadapter:cvadapter@localhost:5432/cvadapter",
)

# Auth / sessions. Authentication is on by default and cannot be disabled in a
# production process. A deliberately noisy development/test escape hatch keeps
# legacy fast tests and local UI exploration practical while making an
# accidental insecure deployment fail at import/startup.
APP_ENV: str = os.getenv("APP_ENV", "development").strip().lower()
_AUTH_REQUESTED: bool = os.getenv("AUTH_ENFORCED", "true").lower() == "true"
_ALLOW_INSECURE_AUTH: bool = (
    os.getenv("CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS", "false").lower()
    == "true"
)
if not _AUTH_REQUESTED:
    if APP_ENV in {"production", "prod"}:
        raise RuntimeError("AUTH_ENFORCED=false is forbidden in production")
    if APP_ENV not in {"development", "dev", "test"} or not _ALLOW_INSECURE_AUTH:
        raise RuntimeError(
            "Disabling auth requires APP_ENV=development or test and "
            "CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS=true"
        )
AUTH_ENFORCED: bool = _AUTH_REQUESTED

# Password hashing cost (the bcrypt work factor). 12 is both the default and
# the floor, and lowering it is refused on exactly the terms `AUTH_ENFORCED`
# uses — never in production, and elsewhere only in a development/test process
# that has also set the noisy insecure-auth escape hatch. It is tunable at all
# for one reason: at 12 rounds a single hash costs ~0.24s, the `actors` fixture
# hashes four passwords per test plus a verify per login, and that arithmetic
# was ~85% of the fast gate's whole runtime. The suite sets 4 (~1ms). See
# docs/testing.md. Production behaviour is pinned by
# tests/test_auth.py::test_the_bcrypt_cost_floor_*.
BCRYPT_MIN_ROUNDS: int = 12
_REQUESTED_ROUNDS: int = int(os.getenv("BCRYPT_ROUNDS", str(BCRYPT_MIN_ROUNDS)))
if _REQUESTED_ROUNDS < BCRYPT_MIN_ROUNDS:
    if APP_ENV in {"production", "prod"}:
        raise RuntimeError(
            f"BCRYPT_ROUNDS below {BCRYPT_MIN_ROUNDS} is forbidden in production"
        )
    if APP_ENV not in {"development", "dev", "test"} or not _ALLOW_INSECURE_AUTH:
        raise RuntimeError(
            f"BCRYPT_ROUNDS below {BCRYPT_MIN_ROUNDS} requires APP_ENV="
            "development or test and CV_ADAPTER_ALLOW_INSECURE_AUTH_FOR_TESTS=true"
        )
BCRYPT_ROUNDS: int = _REQUESTED_ROUNDS

# Password policy and login throttling (STAB-2, docs/plans/v2-pilot.md §7).
# Deliberately plain constants rather than env vars: unlike `BCRYPT_ROUNDS` —
# which is tunable only because the test suite would otherwise spend most of
# its runtime hashing, and which carries its own production guard because of
# that — nothing needs to weaken these per environment, and an env var is a
# way to lower a security floor by accident. `backend/auth/service.py` reads
# them through the module (`config.PASSWORD_MIN_LENGTH`, ...) so a test can
# still monkeypatch a threshold without a 5-failure loop everywhere.
# The pilot seed's separate 15-character bar (`backend/bootstrap/pilot.py`) is
# a bootstrap-only requirement and stays stricter than this general minimum.
PASSWORD_MIN_LENGTH: int = 12
LOGIN_MAX_FAILED_ATTEMPTS: int = 5
LOGIN_LOCKOUT_MINUTES: int = 15

SESSION_COOKIE_NAME: str = os.getenv("SESSION_COOKIE_NAME", "cv_session")
# Set true in any environment served over HTTPS (cloud). False for local http.
# Also gates the HSTS response header (main.py) — both signal "we are behind
# TLS", so one flag drives both (docs/legal/decisions.md Session 4).
SESSION_COOKIE_SECURE: bool = (
    os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
)
SESSION_TTL_HOURS: int = int(os.getenv("SESSION_TTL_HOURS", "168"))  # 7 days
INVITE_TTL_HOURS: int = int(os.getenv("INVITE_TTL_HOURS", "72"))  # 3 days
# Base URL used to build invite links in provisioning responses.
APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")

# CORS (GDPR/Art 32 — Session 4 of docs/legal/decisions.md). Comma-
# separated allowlist; replaces the previous allow_origins=["*"]. Defaults to
# just this app's own base URL — there is no legitimate in-browser
# cross-origin caller today. Add origins here (not by reverting to "*") if a
# real one shows up.
CORS_ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
] or [APP_BASE_URL]

# Notifications (Phase 3). The DB notification always writes (the in-app inbox);
# email/SMS are best-effort external channels, gated OFF by default and log-only
# in dev. Flip on per-channel once AWS SES/SNS are configured. Mirrors the
# AUTH_ENFORCED rollout pattern.
NOTIFICATIONS_EMAIL_ENABLED: bool = (
    os.getenv("NOTIFICATIONS_EMAIL_ENABLED", "false").lower() == "true"
)
NOTIFICATIONS_SMS_ENABLED: bool = (
    os.getenv("NOTIFICATIONS_SMS_ENABLED", "false").lower() == "true"
)
AWS_REGION: str | None = os.getenv("AWS_REGION")
SES_SENDER_EMAIL: str | None = os.getenv("SES_SENDER_EMAIL")

# CV publishing (ADR-009). A published CV's capability link expires after this
# many days; republish or an explicit extend resets the clock. The link is served
# through the app (not a raw object URL) so views can be tracked and revoked.
CV_PUBLISH_TTL_DAYS: int = int(os.getenv("CV_PUBLISH_TTL_DAYS", "28"))
# Publication-owned photo copies (Session 2). On publish the consultant's current
# photo is copied here keyed by token, so the published CV keeps the photo the
# consultant approved even if they later change their profile photo. The file is
# hard-deleted on purge/revoke-expiry and GDPR erasure (privacy/service.py).
CV_PUBLISH_PHOTO_DIR: Path = Path(
    os.getenv("CV_PUBLISH_PHOTO_DIR", "data/cv_publications/photos")
)
# The anonymous capability exchange's request-rate cap (STAB-5 — the last of
# the five controls `docs/legal/dpia.md` requires before external CV-publish
# use). One global budget shared by the four unauthenticated capability routes;
# see `backend/cv_publish/throttle.py` for why it is global rather than
# per-token or per-IP, and what it does and does not defend against.
# 60 requests per minute is far above one real hiring-manager session — a CV
# load is four requests (meta, content, photo, the opened beacon) — while still
# slowing a bulk script by orders of magnitude.
# Plain constants, not env vars, for the same reason as the password/lockout
# thresholds above: nothing needs to weaken these per environment, and an env
# var is a way to lower a security floor by accident. The throttle reads them
# through this module on every call so a test can move a threshold without a
# 60-request loop.
CV_PUBLISH_PUBLIC_RATE_MAX_REQUESTS: int = 60
CV_PUBLISH_PUBLIC_RATE_WINDOW_SECONDS: int = 60

# Durable workflow runner (P-006, ADR-011 §2). One in-process loop, protected
# by a Postgres advisory lock so overlapping ECS deployments cannot run two
# leaders. Disable only to isolate the web process during debugging — accepted
# work then sits queued until a runner comes back, which is the point of the
# durable row.
WORKFLOW_RUNNER_ENABLED: bool = (
    os.getenv("WORKFLOW_RUNNER_ENABLED", "true").lower() == "true"
)
WORKFLOW_POLL_SECONDS: float = float(os.getenv("WORKFLOW_POLL_SECONDS", "2.0"))

DATA_DIR: Path = Path(os.getenv("DATA_DIR", "data/profiles"))
# Gates the ~400-char raw-LLM-response content previews logged at several call
# sites (extraction, merge, CV generation, config bootstrap/update). Off by
# default — these previews can contain consultant PII (GDPR Art 5(1)(c)); flip
# on only for local debugging.
DEBUG_LLM_LOGGING: bool = os.getenv("DEBUG_LLM_LOGGING", "false").lower() == "true"
# Kept as isolated data roots (see tests/test_test_isolation.py) though nothing
# reads or writes into any of the three any more, and none is created at import:
#   JOB_STORE_DIR — the one-JSON-file-per-job store, retired in V2 Slice 1;
#                   Postgres `jobs` is now the only authoritative Job store.
#   APPLICATION_TRACKER_DIR — the file-based verdict tracker,
#                   retired once verdicts moved to Postgres; its last reader,
#                   the one-off `scripts/import_verdicts.py`, was removed in
#                   STAB-7.
#   TA_CONFIG_DIR — the Cockpit's sources.yaml and browser-profile paths,
#                   retired in V2 Step 1 (ADR-010).
JOB_STORE_DIR: Path = Path(os.getenv("JOB_STORE_DIR", "data/job_store/jobs"))
APPLICATION_TRACKER_DIR: Path = Path(os.getenv("APPLICATION_TRACKER_DIR", "data/application_tracker"))
TA_CONFIG_DIR: Path = Path(os.getenv("TA_CONFIG_DIR", "data/ta_config"))

# Retention (GDPR Art 5(1)(c),(e) — docs/legal/decisions.md Session 3).
# Original uploaded CV files are deleted this many days after a successful
# import; only profile.md facts + the metadata.source_archives filename list
# survive. (The verdict-row retention this section used to also configure was
# removed with the `verdicts` table — V3 packet S2.)
UPLOAD_RETENTION_DAYS: int = int(os.getenv("UPLOAD_RETENTION_DAYS", "30"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
APPLICATION_TRACKER_DIR.mkdir(parents=True, exist_ok=True)
CV_PUBLISH_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
