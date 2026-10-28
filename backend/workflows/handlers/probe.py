"""The trivial handler that proves the runner works.

This is the walking skeleton's payload: it does no real business work, but it
demonstrates the contract every real handler (async CV import in Slice 4,
tailored generation in Slice 3) must follow.

**The idempotency contract.** A handler's business effect and the runner's
`mark_succeeded` are separate commits, so a crash between them leaves a
`running` row whose effect is already durable; startup recovery re-queues it
and the handler runs again. P-006 defers leases/heartbeats, so a live handler
thread cannot be cancelled when its lock session is lost — a standby can
therefore invoke the *same* run concurrently with the original, still-running
invocation (see `tests/integration/test_workflow_runner_pg.py::
test_lock_loss_during_execution_allows_at_least_once_invocation_not_duplicate_effect`).
The contract is **at-least-once invocation, exactly-once effect**: a handler
must commit its effect through an atomic, race-safe boundary, never a
check-then-effect read followed by a separate write. `commit_result_ref_once`
(`service.py`) is that boundary here — one `UPDATE ... WHERE result_ref IS
NULL ... RETURNING` claims the effect for exactly one invocation; every other
invocation, whether a benign re-queue or a genuine concurrent duplicate,
observes the already-committed value and performs no effect of its own. Real
handlers need the same shape at their own domain boundary (e.g. an `INSERT ...
ON CONFLICT DO NOTHING` on a uniquely-keyed record, not a prior `SELECT`).

`_call_counts` exists so tests can prove the work happened exactly once across
a re-queue and a duplicate enqueue — a status of `succeeded` alone would not
distinguish "ran once" from "ran twice".
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import Session

from backend.db.models.workflow import WorkflowRun
from backend.workflows import registry, service

PROBE_WORKFLOW_TYPE = "probe"

#: idempotency_key → number of times real work was performed. Test-only
#: instrumentation; nothing in production reads it.
_call_counts: Counter[str] = Counter()


def call_count(idempotency_key: str) -> int:
    return _call_counts[idempotency_key]


def reset_call_counts() -> None:
    _call_counts.clear()


def handle_probe(db: Session, run: WorkflowRun) -> str:
    """Produce a deterministic result reference, exactly once."""
    service.record_stage(db, run, "working", progress=50)
    result_ref = f"probe-result:{run.idempotency_key}"

    # ── The atomic business commit. Deliberately separate from the runner's
    # `mark_succeeded`, so the crash-after-business-commit window is real and
    # testable rather than hypothetical. A second invocation after recovery
    # can occur; only the contender that atomically stored the reference did
    # the business effect. ──
    stored_ref, performed_effect = service.commit_result_ref_once(
        db, run, result_ref
    )
    if performed_effect:
        _call_counts[run.idempotency_key] += 1
    return stored_ref


def raise_probe_failure(db: Session, run: WorkflowRun) -> str:  # noqa: ARG001
    """A deliberately failing type, so failure handling has a fixture."""
    raise service.WorkflowError(
        "probe_failed", "the probe workflow was asked to fail"
    )


PROBE_FAILURE_WORKFLOW_TYPE = "probe_failure"

registry.register(PROBE_WORKFLOW_TYPE, handle_probe)
registry.register(PROBE_FAILURE_WORKFLOW_TYPE, raise_probe_failure)
