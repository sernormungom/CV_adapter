# ADR-011: Durable Pilot Background Processing in the Existing ECS Service

**Status:** Rationale retained; implementation narrowed for V2 Pilot by P-006
**Date:** 2026-08-21
**Revised:** 2026-08-21
**Supersedes:** ADR-006's non-durable in-memory background-work assumptions
**Related:** ADR-009, ADR-010, P-006, `docs/architecture/areas/workflows.md`

---

## V2 Pilot amendment

Postgres remains the durable queue/progress store and the existing ECS application
remains the only compute service. V2 Pilot implements one advisory-lock-protected
in-process leader, typed references, idempotency, and startup recovery. Per-job
leases, heartbeats, automated retry/backoff classification, deterministic scheduler
occurrences, and an Admin retry console are deferred. This amendment governs where
the more elaborate mechanics below conflict.

**As built (V2 Step 1.3).** The table is `workflow_runs`, not the `workflow_jobs`
named below, and it has **no lease/heartbeat columns** — the sections below that
describe leases, lease expiry, heartbeat renewal, priority, `available_at`,
retry classification, and the scheduler loop are the deferred design, not the
code. Implemented instead: `backend/workflows/` with `service.py` (enqueue /
claim via `FOR UPDATE SKIP LOCKED` / finish / startup recovery), `leader.py` (a
session advisory lock, re-verified each cycle, retained across a graceful
shutdown until the in-flight handler finishes), and `runner.py` (one supervised
loop that retries leadership so a standby takes over). Handler idempotency plus
startup recovery is what replaces the lease model.

---

## Context

V2 includes work that may outlive an HTTP request: CV import, tailored-CV
generation, email Job ingestion, row/column matching, expiry detection, retention,
and a nightly audit. A browser or ALB connection must not be the durable owner of
that work.

The product is a pilot for a small company with low expected volume. It currently
runs as one small ARM ECS Fargate web task backed by Postgres. The system will be
maintained by Consultants between assignments, so a short handover path and a small
number of moving parts matter more than high throughput, horizontal scaling, or
continuous background availability. The company accepts slower serial processing
and brief interruption during deployment in exchange for low AWS cost and simpler
operations.

The earlier version of this ADR selected two SQS queues, two continuously running
ECS worker services, EventBridge Scheduler, DLQs, and a transactional outbox. That
topology was reliable but disproportionate: it added recurring Fargate cost and a
larger IAM, deployment, monitoring, retry, and handover surface before pilot evidence
justified it.

The current FastAPI lifespan retention loop is also not an acceptable V2 solution
unchanged. It keeps no durable job/progress record, and its automatic deletion of
expired publications contradicts the accepted rule that expiry requires human
action and deletes nothing.

---

## Decision drivers

In priority order:

1. Preserve the accepted Application, CV, expiry, deletion, and idempotency rules.
2. Add no standing AWS compute service and target effectively zero incremental AWS
   infrastructure cost.
3. Keep deployment, diagnosis, and handover understandable to an occasional
   maintainer.
4. Survive application restarts without losing accepted work.
5. Provide visible progress and safe retry for users and operators.
6. Accept low concurrency, slower execution, and brief deployment interruption.
7. Retain a clear upgrade path if measured usage outgrows the pilot design.

Cost reduction does not waive baseline protection for personal data. Authentication,
authorization, TLS, private storage, data minimization, safe logging, and the agreed
deletion/anonymization rules remain required.

---

## Decision

V2 will run durable background work **inside the existing ECS web service**, using
Postgres as the queue and workflow-state store. It will not initially add SQS,
EventBridge Scheduler, dedicated worker services, DLQs, Redis, or a transactional
outbox.

### 1. One durable `workflow_jobs` table

The API creates the business reservation where required and a `workflow_jobs` row
in one Postgres transaction, then returns `202 Accepted` with the job ID and a
polling URL. The database commit—not an in-memory task—is the acceptance boundary.

The logical record contains:

- stable ID, job type, and handler/schema version;
- Site, subject type/ID, requester ID, and acting capability/role;
- unique business idempotency key;
- status: `queued`, `running`, `succeeded`, `partial_succeeded`, or `failed`;
- priority, `available_at`, attempt count, and maximum attempts;
- stage, progress counters, heartbeat, lease owner, and lease expiry;
- safe error code/message and result type/ID/summary; and
- created, started, and finished timestamps.

Large or sensitive inputs remain in their authorized database records or encrypted
private storage. The job row contains references, not raw CVs, email bodies, Job
descriptions, public tokens, or LLM prompts.

### 2. One in-process job runner

Production explicitly starts Uvicorn with one worker process and enables background
loops in that process with one documented environment flag. The application must
fail startup on an unsafe pilot configuration (for example, multiple Uvicorn
workers with the runner enabled). Postgres leases and advisory locks remain the
safety mechanism against accidental duplicate processes; the one-worker setting is
an operational simplicity constraint, not the sole correctness control.

FastAPI starts one bounded runner loop. The runner:

1. claims one eligible job with a short database transaction using
   `FOR UPDATE SKIP LOCKED` and a time-bounded lease;
2. executes at most one expensive job at a time;
3. persists stage/progress and renews its heartbeat/lease;
4. commits the business result idempotently;
5. records success, partial success, or a safe failure; and
6. retries only classified transient failures, using bounded attempts and
   `available_at` backoff.

Blocking document/LLM integration must not block the API event loop; implementation
may use asynchronous clients or a bounded thread/process executor inside the same
container. Concurrency remains one until measurements justify otherwise.

A process failure may interrupt the current execution, but it does not lose the
job. On startup and periodically, the runner requeues or retries expired leases. A
crash after committing a business result but before marking the job complete is
harmless because finalization is idempotent and the retry first observes the
committed result.

### 3. Scheduling in the same service

A small scheduler loop creates due jobs in `workflow_jobs`. It uses a Postgres
advisory lock or lease plus deterministic occurrence keys, so accidental duplicate
scheduler instances cannot duplicate effects.

- `nightly_audit`: once per Stockholm local date, due at 03:00;
- `publication_expiry_scan`: hourly; and
- `retention_scan`: daily, limited to artifacts the accepted rules permit deleting.

If the service is down at the scheduled time, the next successful loop/startup
creates the missed occurrence. Exact-to-the-second execution is not a pilot
requirement. Public CV access always checks expiry synchronously, so a delayed scan
cannot expose an expired CV; the scan exists to create the landing-page action item.

System maintenance jobs have higher claim priority than new LLM/matching work. An
already running LLM job is not preempted; that temporary delay is an accepted pilot
trade-off.

### 4. User and operator behavior

- Long-running endpoints return `202` plus a job polling URL.
- Users see queued/running/stage/succeeded/partial/failed and a safe actionable
  message appropriate to their normal Site/relationship/object authorization.
- An Admin view lists failed and stale jobs and permits an explicit retry after the
  cause is understood.
- There is no DLQ. Exhausted jobs remain durably `failed` in Postgres until reviewed
  or removed under an explicit retention rule.
- Structured logs include job/correlation ID, type, stage, attempt, duration,
  outcome, and safe error code without sensitive content.

### 5. Workflow-specific invariants

- **Apply:** reserve the Consultant–Job pair first; create the Application,
  publication, and review together only after CV generation/validation succeeds.
  Failure removes temporary content and exposes no unusable live Application.
  The stable pair reservation is separate from individual workflow attempts: a
  retry receives a new attempt/generation key while retaining the pair reservation.
  A terminal failed `workflow_jobs` row remains audit evidence and does not
  permanently poison the pair; an authorized retry can create a new job only after
  atomically confirming the reservation has no active attempt.
- **Import CV:** delete the source upload after the final file-dependent stage,
  including when a later stage fails. Validated prior output may produce
  `partial_succeeded`.
- **Email ingestion:** a stable source-message key creates at most one result/Job;
  transient email content is deleted after extraction/finalization.
- **Matching:** input-version keys make repeated row/column/cell work harmless;
  scores remain advisory and never block Apply.
- **Expiry:** elapsed publications stop anonymous access and create/ensure
  `expired_action_required`; no CV, Application, publication, or quarantined Job is
  automatically deleted.
- **Terminal actions/history:** retries create exactly one history result and
  complete the agreed CV deletion without recreating content.

### 6. Deployment and cost

The existing web task, migration task, ECR image, Terraform deployment, Postgres,
and log group remain the infrastructure shape. No additional always-running task is
introduced. The pilot therefore targets effectively **$0 incremental AWS
infrastructure cost**, aside from possible small increases in existing task runtime,
database activity, storage, or logs.

The current 0.25-vCPU/0.5-GB task is tried first. CPU/memory is increased only after
measurement shows that normal UI or a representative CV workflow cannot operate
acceptably. A resource increase is an explicit cost decision, not an assumed part of
this ADR.

Local Docker Compose continues to run Postgres and the app; it needs no AWS queue
emulator. Business handlers accept a job ID and remain directly testable outside the
runner loop.

---

## Consequences

### Positive

- No new standing AWS compute cost or message/scheduler infrastructure.
- One deployable service, one database, and one primary operational runbook.
- Accepted work and user-visible progress survive restarts.
- Database transactions and constraints provide the same required business
  idempotency used by any future queue architecture.
- Local development and handover remain close to the existing application.
- The model is sufficient for one low-volume pilot and can be measured before
  spending on scale.

### Accepted trade-offs

- Web and background work share CPU, memory, failure domain, and deployments.
- Only one expensive job runs at a time, so users may wait behind other work.
- Background processing pauses while the ECS task is unavailable or deploying.
- A broken job handler can affect the web process; bounded execution, timeouts,
  leases, and startup recovery reduce but do not eliminate that risk.
- There is no independently scalable worker or managed DLQ.
- An already-running long job can delay a higher-priority scheduled job.

These are deliberate pilot compromises, not properties to preserve after the
upgrade triggers below are reached.

### Operational obligations

- Alert on or visibly report stale/failed jobs using the existing application/log
  path; avoid a broad new monitoring stack for the pilot.
- Test restart recovery, expired leases, retry exhaustion, and duplicate
  finalization.
- Keep job handlers bounded by timeouts and ensure temporary-file cleanup is itself
  retryable.
- Document the runner, scheduler, common failure codes, manual retry, and deployment
  behavior in one short maintainer runbook.

---

## Alternatives considered

### Two SQS queues and two continuously running ECS workers

Rejected for the pilot after cost and maintainability review. It offers stronger
isolation and availability but adds recurring Fargate cost plus queues, DLQs,
EventBridge, IAM roles, an outbox dispatcher, alarms, redrive, and coordinated
message deployments. It remains the preferred scale-out direction if justified by
evidence.

### Postgres state plus one dedicated ECS worker

Deferred. It provides process isolation but still adds one continuously billed
service and another deployment/logging unit. It is a possible intermediate step if
shared-process interference appears before SQS is needed.

### On-demand or scheduled Fargate tasks

Rejected initially. Compute would be pay-per-run, but task triggering, startup
latency, missed-trigger recovery, and per-run diagnosis are harder to explain than
one durable database runner at the current volume.

### Lambda

Rejected as the main runner. It introduces another runtime/deployment model and has
a 15-minute per-invocation limit. It may be considered later for a narrow bounded
integration only.

### Pure in-memory FastAPI tasks

Rejected. Returning success before a durable database row exists can lose accepted
work on restart and provides no reliable progress/retry record. This ADR uses an
in-process executor, but never an in-memory-only queue.

### Celery/Redis, RabbitMQ, or Step Functions

Rejected/deferred as unnecessary infrastructure or orchestration for current pilot
workflows.

---

## Upgrade triggers and migration path

Revisit SQS and separate ECS workers when any of these is observed:

1. the web service needs more than one replica;
2. background execution materially slows or destabilizes normal UI/API use;
3. job backlog regularly exceeds the agreed waiting-time target;
4. deployments interrupt work often enough to affect users;
5. LLM work must be isolated for cost/rate/concurrency control;
6. a second customer or materially larger Site population is introduced; or
7. operational evidence shows Postgres polling/recovery is inadequate.

The migration keeps `workflow_jobs` as user-visible state. An outbox and SQS message
containing only the job ID can be added, and the same handlers can move to dedicated
workers without changing business invariants or endpoint polling contracts.

---

## Implementation and supersession notes

- ADR-006 remains historical MVP context. Its in-memory-only background-work
  assumptions must not guide V2, but using one deployment unit is intentionally
  retained for the pilot.
- Replace the current `_retention_purge_loop` rather than copying it: V2 scheduling
  writes durable jobs, and publication expiry never performs automatic CV deletion.
- The prior SQS/ECS version of ADR-011 was a planning decision only and was not
  implemented. This revision supersedes it within the same ADR.
- Schema, handler contracts, recovery tests, rollout, and the maintainer runbook
  belong in the executable V2 implementation plan.
