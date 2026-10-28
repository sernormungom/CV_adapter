# Durable background work — subsystem contract

**Owns:** `backend/workflows/`, table `workflow_runs`.
**Decisions:** ADR-011, narrowed by P-006 to what the pilot actually builds.
**Read alongside:** [ADR-011](../adr/ADR-011-durable-background-processing.md).
**Handlers live in their own areas:** [cv-import.md](cv-import.md)
(`cv_import`), [cv-artifacts.md](cv-artifacts.md) G6/G7 (`cv_generate`), and
[jobs.md](jobs.md) (`matching_config` — `backend/matching/config_generation.py`,
S1's "Update match configurator", the only caller `config_bootstrapper.bootstrap_config`
has ever had).

Why this exists: CloudFront and the ALB both time out at a **verified 60
seconds** (`docs/evidence/m1-timeout-inventory.md`), and a timed-out request
keeps running at the origin — so retrying a synchronous import or generation
duplicates work. **Acceptance is a database commit plus an idempotency key,
never an open HTTP connection.**

---

## Contract

| File | Responsibility |
|---|---|
| `service.py` | All `workflow_runs` state transitions. A domain service stages through this module; it never writes the table itself. |
| `leader.py` | `LeaderLock` — a session-scoped `pg_try_advisory_lock` on a dedicated connection. |
| `runner.py` | `run_once`/`drain` (synchronous, testable core) and `WorkflowRunner` (leadership supervision + asyncio loop). |
| `registry.py` | `register` / `get_handler` / `registered_types`. |
| `handlers/probe.py` | The reference handler shape. Copy this. |

**Table `workflow_runs`** (`backend/db/models/workflow.py`): type, status
(`queued|running|succeeded|failed`), stage/progress, unique `idempotency_key`,
typed `subject_type`/`subject_id`, `input_ref`/`result_ref`, bounded
`error_code`/`error_message`, attempts, timestamps.

**Config:** `WORKFLOW_RUNNER_ENABLED` (default true), `WORKFLOW_POLL_SECONDS`
(2.0). The LLM timeout override constant is `backend/llm.py`'s
`WORKFLOW_TIMEOUT_SECONDS` (170.0), not a `config.py` value.

---

## Guards

### G1 — References only. Never content, never tokens

No CV, email, prompt, or token text in any column.
*Pinned by:* `tests/test_workflow_runner.py::test_workflow_run_has_no_content_bearing_columns`
— a **column allowlist** that fails when a content-shaped column is added —
and `::test_reference_columns_cannot_hold_a_large_payload`.

### G2 — `enqueue` commits the caller's transaction on all three return paths

Key-already-exists, lost-the-unique-index-race, and new-run. A caller may hold
the domain row this run is for uncommitted and have it land in the same
commit — that is how `tailored_cv` makes the artifact and its run visible
together under one lock.

**The invariant is about the commit, not about flushing.** `Session.close()`
discards whatever was not committed — pending objects if never flushed, an open
transaction if they were — so a missing commit loses the caller's work whatever
the session's autoflush setting. `backend/db/session.py` builds `SessionLocal`
with `autoflush=False`; do **not** describe the lookup `SELECT` as autoflushing
(SQLAlchemy's default, but not this application's).

Two ways this broke, both fixed: an unqualified `rollback()` on the duplicate
path discarded the caller's work — hence the savepoint; and the
key-already-exists path returned with no commit at all, so the caller got a
successful run having silently lost its row.
*Pinned by:* `tests/test_workflow_runner.py::test_enqueue_commits_caller_owned_work_when_it_creates_the_run`,
`::test_enqueue_commits_caller_owned_work_when_the_key_already_exists`,
`::test_enqueue_commits_caller_owned_work_when_it_loses_the_index_race`
— **each under both flush policies** — plus
`tests/integration/test_workflow_runner_pg.py::test_enqueue_takes_the_integrity_error_path_and_returns_the_winner`,
`::test_the_unique_index_is_what_enforces_one_run_per_key`,
`tests/test_workflow_runner.py::test_duplicate_enqueue_returns_the_same_run`,
`::test_duplicate_enqueue_does_not_double_run_the_handler`.

### G3 — The contract is at-least-once *invocation*, exactly-once *effect*

**Not** "handlers only ever re-run after a clean crash." A live handler thread
cannot be cancelled, so a standby that recovers a `running` row can invoke a
handler **concurrently with a still-executing original**.

A handler must therefore commit its effect through an atomic, race-safe
boundary — never a check-then-effect read followed by a separate write.
`service.commit_result_ref_once` is the reference: one
`UPDATE … WHERE result_ref IS NULL … RETURNING`. `handlers/probe.py` is the
reference shape; each real handler repeats it at its own domain boundary.
*Pinned by:* `tests/integration/test_workflow_runner_pg.py::test_lock_loss_during_execution_allows_at_least_once_invocation_not_duplicate_effect`.

**What is deliberately not deduplicated:** the LLM call itself, because P-006
defers the leases that would make that possible. A lock loss during execution
can cost a second model call; the artifact, its revision, and its hash still
move exactly once.

### G4 — Terminal writes are guarded, not just effects

`mark_failed` refuses to downgrade an already-`succeeded` row (returns `False`
rather than corrupting it). `record_stage` is a no-op once the row is terminal.
`mark_succeeded` **may** upgrade a stale `failed` row — a committed effect is
authoritative over an earlier wrong guess — and is idempotent without
rewriting `finished_at`. `stage_succeeded` is that same write **without the
commit**, for a caller that must publish the run transition and its own domain
effect in one transaction; `cv_importer.service._try_reconcile_stuck_attempt`
is the only caller (see [cv-import.md](cv-import.md) G5).
*Why:* a stale invocation's late failure or progress write must not corrupt a
sibling invocation's completed outcome.
*Pinned by:* `tests/test_workflow_runner.py::test_mark_failed_does_not_downgrade_an_already_succeeded_run`,
`::test_mark_failed_applies_normally_when_not_already_succeeded`,
`::test_mark_succeeded_can_upgrade_a_stale_failed_row`,
`::test_mark_succeeded_is_idempotent_and_does_not_rewrite_finished_at`,
`::test_record_stage_is_a_noop_once_the_run_is_terminal`,
`tests/integration/test_workflow_runner_pg.py::test_a_late_failing_invocation_cannot_overwrite_a_succeeded_row`.

### G5 — An arbitrary exception's text never reaches the row

`_safe_error`: only a deliberately raised `WorkflowError` contributes a
message; anything else records its class name only. Messages are length-bounded.
*Pinned by:* `tests/test_workflow_runner.py::test_unexpected_exception_text_is_never_persisted`,
`::test_error_message_is_bounded`.

### G6 — One leader, by a session-scoped advisory lock

`pg_try_advisory_lock` on a dedicated connection — **database-local, not
cluster-wide**. Try-not-wait, re-verified every cycle (`verify()`), released
when the backend session ends (including SIGKILL), so there is no lease to wait
out. This is what stops the old and new task of a rolling ECS deployment both
running a runner. A standby retries after losing the initial race — it must not
stay runnerless after a rolling deployment.
*Pinned by:* `tests/integration/test_workflow_runner_pg.py::test_only_one_process_can_hold_leadership`,
`::test_leadership_is_released_when_the_leader_process_dies`,
`::test_acquire_is_idempotent_for_the_holder`,
`::test_a_second_runner_takes_over_after_the_leader_loses_its_lock`,
`::test_a_runner_stands_down_when_its_lock_session_is_killed`,
`tests/test_workflow_runner.py::test_a_standby_keeps_trying_and_takes_over`,
`::test_runner_does_not_consume_work_when_it_is_not_leader`,
`::test_runner_stands_down_when_leadership_can_no_longer_be_verified`,
`::test_runner_start_is_not_fatal_when_leadership_errors`.

### G7 — Shutdown retains leadership until the in-flight handler finishes, including past the grace period

*Why:* a `to_thread` worker cannot be cancelled. Releasing on a timeout — or on
the supervisor task being cancelled afterwards — would let a replacement leader
recover and invoke the same run concurrently. G3 means a handler must tolerate
that anyway; this guard keeps it rare rather than routine.
*Pinned by:* `tests/integration/test_workflow_runner_pg.py::test_shutdown_keeps_leadership_until_the_handler_finishes`,
`::test_grace_expiry_retains_leadership_while_the_handler_runs`,
`::test_cancelling_the_supervisor_after_grace_does_not_unlock_a_live_thread`,
`::test_losing_the_lock_while_a_handler_runs_stops_further_consumption`.

### G8 — Claiming is `FOR UPDATE SKIP LOCKED`, FIFO by creation

*Pinned by:* `tests/integration/test_workflow_runner_pg.py::test_skip_locked_lets_a_second_claimer_pass_a_locked_row`,
`::test_claims_are_fifo_by_creation`,
`::test_a_committed_claim_is_not_visible_to_the_next_claimer`,
`::test_status_check_constraint_rejects_an_unknown_status`,
`tests/test_workflow_runner.py::test_terminal_runs_are_not_claimed_again`.

### G9 — Recovery requeues interrupted work without redoing committed work

And a failing recovery blocks consumption rather than proceeding past it.
*Pinned by:* `tests/test_workflow_runner.py::test_recovery_requeues_a_run_interrupted_mid_flight`,
`::test_recovery_after_business_commit_does_not_redo_the_work`,
`::test_recovery_leaves_terminal_runs_alone`,
`::test_recovery_failure_blocks_consumption_until_it_succeeds`,
`tests/integration/test_workflow_runner_pg.py::test_a_killed_runner_is_recovered_without_repeating_the_work`,
`::test_recovery_then_drain_completes_interrupted_work`.

### G10 — A handler failure is recorded, never raised into the loop

An unknown workflow type fails its run without killing the loop. Handlers run
via `asyncio.to_thread`, so blocking DB/LLM work never stalls the API event
loop.
*Pinned by:* `tests/test_workflow_runner.py::test_handler_failure_is_recorded_not_raised`,
`::test_unknown_workflow_type_fails_the_run_without_killing_the_loop`,
`::test_the_app_process_has_the_built_in_handlers_registered`,
`::test_the_lifespan_composes_a_runner_on_the_app_session_factory`.

### G11 — Every handler LLM call passes the workflow timeout override

`timeout=WORKFLOW_TIMEOUT_SECONDS` (170s). `backend/llm.py`'s module default
(30s, 1 retry) exists only for `/api/chat` and `/api/chat/profile-patch`, which
are still synchronous on the request path and must fail inside the 60s edge
budget. Full reasoning in [cv-import.md](cv-import.md) G13.
*Pinned by:* `tests/test_llm.py::test_llm_timeout_budget_is_below_the_gateway_deadline`,
`::test_workflow_timeout_is_generously_above_the_gateway_budget`,
`::test_cv_extraction_uses_the_workflow_timeout_not_the_gateway_default`,
`::test_profile_merge_uses_the_workflow_timeout_not_the_gateway_default`,
`::test_cv_generation_uses_the_workflow_timeout_not_the_gateway_default`.

### Deferred by P-006 — do not add without changing that decision

Per-job leases · heartbeats · automated retry/backoff classification ·
priority and `available_at` · scheduler occurrences · an Admin retry console ·
SQS · dedicated worker processes.

The first real handler, and the shape every later one copies, is
`backend/tailored_cv/handler.py`.
