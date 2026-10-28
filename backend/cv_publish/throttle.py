"""A global request-rate cap for the four anonymous capability routes (STAB-5).

`docs/legal/dpia.md`'s planned V2 treatment for CV publishing named five
controls required before external use: fragment-only link transport, hash-only
token storage, **a throttled anonymous exchange**, sanitized/minimized output,
and strict browser controls. The other four shipped with Slice 5 / `S5-P2`;
this module is the third.

**What this is not.** It is not a defence against guessing a live token.
`backend.auth.security.generate_token` is `secrets.token_urlsafe(32)` — 256
bits — so random search is infeasible at any request rate a limiter could
change, and describing this as brute-force protection would overstate what it
buys. What it does buy, on a surface that is deliberately open to anyone with
no identity check at all, is a bound on unbounded automated hammering and the
database/disk work each such request causes.

**Global, not per-token.** The abuse shape worth bounding is a script walking
many *different* tokens, so a counter scoped to one token would reset for free
on every attempt. This counts every request to the four capability routes
regardless of which token (or no token) is presented.

**Global, not per-IP.** No trusted client address exists at this layer: nothing
in `backend/` reads `X-Forwarded-For`, and behind the ALB/CloudFront these
requests will sit behind, `request.client.host` is the load balancer's own
address. A per-IP limiter would therefore collapse to a global one while
*looking* more precise, so this is an honest global counter instead.
`docs/handoffs/stab-2-password-policy-and-throttling.md` recorded the same
absent-trusted-header gap for login throttling; a real trusted-proxy contract
should be established once, for both.

**The trade-off, stated rather than hidden:** one budget is shared by every
concurrent legitimate viewer. At this pilot's realistic external traffic (a
small handful of concurrent hiring-manager sessions; today, before any V2
deployment, zero) a 60-request minute is far above one viewer's whole session —
a CV load is `meta` + `content` + `photo` + one `opened` beacon, four requests
— so the sharing costs nothing real while still slowing a bulk script by
orders of magnitude.

**In-process and in-memory** — no dependency, no schema, no cache service, the
same smallest-mechanism-that-fits choice the in-process workflow runner makes.
The known limitation: this throttles *per Python process*, so the effective
cap multiplies by the number of worker processes and tasks. The pilot's
deployed shape makes that theoretical rather than real today — the `Dockerfile`
`CMD` runs `uvicorn` with no `--workers` flag (one worker process) and the ECS
service runs at desired count 1 (`docs/evidence/m1-timeout-inventory.md` §2) —
but the multiplication is real the moment either number rises, and this comment
is the warning rather than a later surprise.

**Deliberately not locked.** Two threads (FastAPI runs these sync routes in a
threadpool) can interleave a read and a write of the counter, so the count can
be off by a few, and a window rollover can in the worst case admit up to a
second window's budget momentarily. That is immaterial for an abuse bound at
this scale and locking it would cost more than the handful of requests it
saves — the same reasoning `STAB-2` recorded for its own login-failure counter
and the stop rule in `docs/plans/v2-pilot.md` §1.

**Fail-open on restart.** The window and counter are memory only, so a deploy
or crash resets the budget to zero used. That is the safe direction for an
abuse bound: it can never lock legitimate traffic out of a freshly started
process.
"""

from __future__ import annotations

import time

from backend import config


class PublicCapabilityThrottle:
    """A fixed-window counter shared by all four public capability routes.

    Fixed window rather than sliding: one integer and one timestamp, no
    per-request bookkeeping to grow, and the only cost of the simpler shape —
    a burst straddling a window boundary can briefly exceed the nominal rate —
    is irrelevant to bounding sustained automated traffic.

    Thresholds are read from `backend.config` on every call, not captured at
    construction, so a test can raise or lower them without a 60-request loop
    (the pattern `backend/auth/service.py` uses for its own thresholds).

    `window_started_at` and `request_count` are deliberately public: the
    codebase's established way to test time-bounded state is to move that
    state directly rather than sleep (`locked_until` in `STAB-2`), and that
    needs something to move.
    """

    def __init__(self) -> None:
        self.window_started_at: float = time.monotonic()
        self.request_count: int = 0

    def reset(self) -> None:
        """Start a fresh, empty window. Used by the test fixture that keeps
        one test's public-route calls from spending another's budget."""
        self.window_started_at = time.monotonic()
        self.request_count = 0

    def allow(self) -> bool:
        """Count this request and report whether it is within budget.

        A refused request is still counted; in a fixed window that changes
        nothing (the window expires on time either way) and keeps the counter
        an honest record of arrivals rather than of admissions.

        The clock is `time.monotonic()`, so a wall-clock adjustment can
        neither extend nor collapse a window.
        """
        now = time.monotonic()
        if now - self.window_started_at >= float(
            config.CV_PUBLISH_PUBLIC_RATE_WINDOW_SECONDS
        ):
            self.window_started_at = now
            self.request_count = 0
        self.request_count += 1
        return self.request_count <= config.CV_PUBLISH_PUBLIC_RATE_MAX_REQUESTS


#: The one shared budget. Module-level because the cap is global by design —
#: a per-request or per-token instance would be exactly the scoping this
#: module's docstring explains does not defend against anything.
public_capability_throttle = PublicCapabilityThrottle()
