# M1 timeout inventory and 504 boundary evidence

**Historical origin:** V2 M1 from the superseded implementation plan. The timeout
finding remains active evidence for the minimal durable runner described in
`docs/architecture/areas/workflows.md`.
**Question answered:** whether the observed 504 came from CloudFront, ALB, Uvicorn,
the LLM client, or the browser —
*"Repository code alone does not prove whether the observed 504 came from
CloudFront, ALB, Uvicorn, the LLM client, or a browser timeout. ... M1 must
inventory every synchronous endpoint and each deployed timeout, reproduce the
failing boundary, and record which calls must move behind M3's durable `202`
polling contract. 'Use background work' is the likely remedy, not the
root-cause finding."*
**Evidence gathered:** 2026-08-24. All AWS inspection was read-only; nothing was
deployed, created, or modified.

---

## 1. The finding, up front

**The failing boundary is CloudFront's `origin_read_timeout`, deployed at 60
seconds — the maximum available without an AWS support quota increase.**

Three independent reasons the 504 cannot be coming from anywhere else:

1. **Uvicorn cannot produce it.** Uvicorn has no request timeout, and the
   container's `CMD` sets none. It will hold a request open indefinitely.
2. **The Anthropic SDK cannot produce it.** A client-side timeout there raises
   `APITimeoutError` inside the handler, which surfaces as an application error
   response — not a gateway `504` with an HTML body. And its own budget is
   *larger* than the edge's: `timeout=60.0` with `max_retries=4` means a single
   call can legitimately spend ~5 × 60 s plus backoff before it gives up, all
   of it after the edge has already answered the caller.
3. **The browser cannot produce it.** No `AbortController`, `AbortSignal`, or
   fetch timeout exists anywhere in `html/` — the client sets no deadline of its
   own, so it simply receives whatever the edge returns.

That leaves the two gateway layers, and both are at 60 s. CloudFront loses the
tie **by construction**: its read clock starts when it forwards the request to
the ALB, and the ALB's idle clock only starts when the ALB receives it, one
network hop later. CloudFront's deadline therefore always expires first, by
roughly the CloudFront→ALB latency, and it serves its own 504 page.

The consequence for M3: raising CloudFront past 60 s requires an AWS support
ticket, **and** would then immediately hit the ALB's 60 s. Both would have to
move. This is a ceiling to design around, not a value to tune.

### The part that makes it a correctness problem

Past the deadline the request is aborted; **the work behind it is not**. The
origin keeps running to completion, unaware. A user who sees the 504 and retries
an import or a generation therefore starts a second run while the first is still
going. That is why M3's answer is a durable `202` contract plus an idempotency
key, and why a larger timeout would not have been a fix even if one were
available.

---

## 2. Deployed topology and verified timeout values

```
browser  ──►  CloudFront  ──►  ALB  ──►  Uvicorn (ECS Fargate, ARM64)  ──►  Anthropic API
   no          60 s            60 s        no request timeout              60 s × 5 attempts
 timeout   origin_read_    idle_timeout
            timeout
```

| Layer | Setting | Deployed value | How verified (read-only) |
| --- | --- | ---: | --- |
| Browser | any fetch abort/timeout | **none** | no `AbortController`/`AbortSignal`/fetch timeout in `html/` |
| CloudFront `<distribution-id>` | `OriginReadTimeout` | **60 s** | `aws cloudfront list-distributions` |
| CloudFront | `OriginKeepaliveTimeout` | 5 s | same |
| ALB `cv-adapter-dev-alb` | `idle_timeout.timeout_seconds` | **60 s** | `aws elbv2 describe-load-balancer-attributes` |
| Target group | health-check `timeout` | 5 s | `infra/modules/service/main.tf:81` — health checks only, not request handling |
| Uvicorn | request timeout | **none exists** | `Dockerfile` `CMD` passes no timeout flag; uvicorn has no request-timeout option |
| Uvicorn | `--timeout-keep-alive` | 5 s (default) | idle keep-alive between requests, not a request deadline |
| Anthropic SDK | `timeout` | 60 s | `backend/llm.py:41` `_TIMEOUT_SECONDS = 60.0` |
| Anthropic SDK | `max_retries` | 4 | `backend/llm.py:40` — so up to 5 attempts, ~300 s + backoff |

Both edge values were previously *lower*. `infra/envs/dev/cloudfront.tf:48-53`
records the earlier increase from CloudFront's 30 s default to 60, with the
reason: *"CloudFront was timing out and serving its own 504 HTML page before the
origin could respond. 60 is the max allowed without an AWS support ticket to
raise the account-level quota further."* That is the same failure this record
now pins down, and it confirms 60 is a hard ceiling at that layer.

### Verified deployed revision

| Item | Value |
| --- | --- |
| ECS cluster | `cv-adapter-dev` |
| Service | `cv-adapter-dev` (ACTIVE, desired 1, running 1) |
| Task definition | **`cv-adapter-dev:9`** |
| Image | `cv-adapter-app:5d7e1ed2a953e002fea6b880fda29498f8efdd92` |
| Deployment | `ecs-svc/3392790283731897857`, PRIMARY, updated 2026-08-12T16:31:28+02:00 |
| CloudFront domain | `<distribution>.cloudfront.net` (Deployed) |

The image tag is commit `5d7e1ed`. Repository `main` is at `a460fed`, which is
one documentation-only commit ahead, so **the deployed code is current** and the
values above describe the code inventoried below.

---

## 3. Inventory: every synchronous LLM and document endpoint

`max_tokens` is the useful risk signal here, because output generation dominates
latency. At roughly 50–80 output tokens/second, a 6 000-token completion is
**75–120 s of generation alone** — already past the 60 s edge budget before any
retry, document parsing, or second call.

| # | Endpoint | Handler | LLM calls in one request | Model | `max_tokens` | Verdict against the 60 s edge |
| --- | --- | --- | --- | --- | ---: | --- |
| 1 | `POST /api/import-cv` | `cv_importer/file_receiver.py:31` (`async`) | **2** — `extract_experience` then `merge_profiles` | Sonnet | 6 000 **+ 8 000** | **Cannot reliably fit.** Also does PDF/DOCX parsing first. Highest risk in the system. |
| 2 | `POST /api/generate-cv` | `cv_pipeline/handoff_receiver.py:21` (`async`) | 1 — `generate_cv_content` | Sonnet | 6 000 | **Routinely exceeds** the budget on a full CV. |
| 3 | `POST /api/dashboard/{id}/complete-cycle` | `dashboard/routes.py:194` (`def`) | 1 — `propose_config_update` | **Opus** | 8 000 | **Routinely exceeds.** Largest budget on the slowest model. |
| 4 | `POST /api/dashboard/{id}/score-jobs` | `dashboard/routes.py:290` (`def`) | 0 or 1 — `bootstrap_config`, only when no config exists | Sonnet | 4 000 | At risk on a consultant's **first** run only. |
| 5 | `POST /api/opportunities/run-matching` | `pre_filter_matcher/routes.py:21` (`def`) | 0 or 1 — same `bootstrap_config` path | Sonnet | 4 000 | Same as #4. |
| 6 | `POST /api/chat/profile-patch` | `cv_assistant/routes.py:90` (`async`) | 1 — `profile_patcher` | Haiku | 4 000 | Borderline. Haiku is fast, but a retry crosses the line. |
| 7 | `POST /api/chat` | `cv_assistant/routes.py:38` (`async`) | 1 — `chat_engine` | Sonnet | 2 000 | Usually fits. A single SDK retry does not. |
| 8 | `GET /api/chat/gaps/{username}` | `cv_assistant/routes.py:59` (`def`) | **0** | — | — | Document/disk read only. Not a timeout risk. |

All eight are synchronous request/response. None streams, none returns `202`,
and none is idempotent on retry.

### A note on #3–#5

`propose_config_update` and `bootstrap_config` are **synchronous** functions that
call `asyncio.run()` internally (`config_updater.py:123`,
`config_bootstrapper.py:148`). They work today only because their endpoints are
declared `def`, so FastAPI runs them in a worker thread. Converting any of those
endpoints to `async def` without changing the callee would raise
*"asyncio.run() cannot be called from a running event loop"*. M3 should record
this when it moves these behind the workflow runner.

---

## 4. Reproduction of the boundary

**Script:** `scripts/m1_timeout_probe.py`. It stands up a synthetic origin that
answers too late behind a synthetic gateway with a read timeout — the shape of
the deployed path — and reports which layer answered.

Safe and non-persistent by construction: everything binds `127.0.0.1` on an
ephemeral port, nothing is written to disk, no database or credential is
touched, and no AWS resource is created or changed.
`tests/test_timeout_boundary.py::test_probe_binds_only_loopback` keeps that
true.

Run at the deployed value, 2026-08-24T12:40:18Z–12:41:33Z:

```
$ python -m scripts.m1_timeout_probe --gateway-timeout 60 --origin-delay 75 --also-under-budget
{
  "gateway_timeout": 60.0,
  "origin_delay": 3.0,
  "status": 200,
  "answered_by": "origin",
  "client_elapsed": 3.004,
  "origin_completed": true,
  "origin_elapsed": 3.003
}
{
  "gateway_timeout": 60.0,
  "origin_delay": 75.0,
  "status": 504,
  "answered_by": "gateway",
  "client_elapsed": 60.083,
  "origin_completed": true,
  "origin_elapsed": 75.003
}
```

Reading the second result:

- `status: 504`, `answered_by: "gateway"` — the edge generated the response. No
  application-side error handling could have changed what the caller saw.
- `client_elapsed: 60.083` against `origin_delay: 75.0` — the caller gave up at
  the *gateway's* deadline, not when the work finished. This is why raising an
  application or SDK timeout cannot fix it.
- `origin_completed: true`, `origin_elapsed: 75.003` — **the work finished
  anyway, 15 seconds after the client was told the request failed.** The
  correctness problem, not just the error message.

The control run (`origin_delay: 3.0`) confirms the boundary only bites when the
origin is genuinely too slow.

`tests/test_timeout_boundary.py` runs the same code at 0.75 s so the finding
stays under test in the fast gate; one of those tests asserts the behaviour is
scale-invariant, which is what licenses testing 60 s behaviour at 0.75 s.

---

## 5. What must move behind M3's `202` contract

Recorded here as M1's input to M3; M3 owns the decision.

**Must move.** These cannot fit the 60 s ceiling and the ceiling cannot be
raised:

- `POST /api/import-cv` (#1) — two large calls plus document parsing. Also
  D-007/T-006 territory: the source upload must be deleted at its last
  processing stage even on the partial-failure paths a 504 creates today.
- `POST /api/generate-cv` (#2).
- `POST /api/dashboard/{id}/complete-cycle` (#3).

**Must move, lower frequency.** Only the first-run bootstrap is slow, but it
fails exactly when a new consultant is onboarded:

- `POST /api/dashboard/{id}/score-jobs` (#4) and
  `POST /api/opportunities/run-matching` (#5) — move the `bootstrap_config`
  call, not necessarily the scoring, which is deterministic and fast.

**May stay synchronous, with a caveat.** Interactive latency is the product
requirement here, and a `202` round trip would hurt it:

- `POST /api/chat` (#7) and `POST /api/chat/profile-patch` (#6). Both fit the
  budget in the normal case but not with an SDK retry. If they stay
  synchronous, the retry budget should be reduced for these call sites so the
  SDK gives up *inside* the edge window and the user gets a real application
  error instead of an opaque gateway 504.

**No change needed.**

- `GET /api/chat/gaps/{username}` (#8) — no LLM call.

### One thing to fix regardless of what moves

`backend/llm.py` sets the SDK timeout to exactly the edge timeout (60 s = 60 s)
and then retries four times. For any call that stays synchronous, that
combination guarantees the edge wins and the user never sees the application's
own error handling. Whatever M3 moves, the synchronous remainder needs an SDK
budget that fits *inside* the edge window, not one equal to it.

---

## 6. What was **not** verified

Stated explicitly so this record is not read as more than it is.

- **No 504 was reproduced against the deployed stack.** Doing so would need
  either a slow request against production (a production write, and the
  slow paths all mutate profile or CV state) or a deployment of probe code.
  Both are outside the read-only authorization for this milestone. The
  reproduction above is a faithful synthetic model of the mechanism; the
  deployed *values* it is run at are verified live, and the deployed
  *behaviour* is independently corroborated by the CloudFront 30→60 s change
  and its recorded reason in the infra repository.
- **The CloudFront-versus-ALB tie is settled by reasoning, not measurement** —
  clock-start ordering plus the recorded infra comment. It does not change the
  conclusion: both layers are at 60 s, so the ceiling is 60 s either way.
- **Output-token throughput (50–80 tok/s) is an estimate**, not a measurement
  from this deployment. It is used only to rank endpoints by risk; the ranking
  does not depend on the exact figure, since the two-call `import-cv` path
  exceeds the budget under any plausible rate.
