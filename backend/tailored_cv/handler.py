"""The durable tailored-CV generation handler — the runner's first real payload.

`handlers/probe.py` is the shape; this is the thing itself. Everything the
probe's docstring says about the contract applies literally here, because CV
generation is exactly the work P-006 was written for: it calls an LLM, it can
outlast the verified 60-second CloudFront/ALB timeout
(`docs/evidence/m1-timeout-inventory.md`), and a client that retries a timed-out
request must not cause a second document to be produced.

**It reads the snapshot and nothing else.** No profile file, no style file, no
Job row — every input was frozen when a human asked for the generation
(`service.build_input_snapshot`). That is what makes a re-queued invocation
produce the same document as the original, and what stops an edit to
`profile.md` landing mid-flight from changing an approved generation.

**Its effect is one conditional UPDATE.** `service.commit_generated_state_once`
is the atomic boundary the contract demands — never a read-then-write. Two live
invocations of the same run, or a superseded attempt still executing beside a
newer one, all reach it; exactly one changes the row and the rest observe the
result. The LLM call itself is *not* deduplicated (P-006 defers the leases that
would make that possible), so a lock-loss during execution can cost a second
call. That is a cost, not a correctness failure: the artifact, its revision, and
its hash still move exactly once.

**A failure is recorded on the artifact as well as the run.** The consultant is
looking at the tailored CV, not at `workflow_runs`, so the row carries a
bounded, operator-safe reason — and, crucially, keeps any previously generated
content: a transient LLM failure during a *re*generation must not cost someone
the document they already had.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from backend.cv_pipeline.cv_content_generator import generate_cv_content
from backend.cv_pipeline.form_data import cv_content_to_form_data
from backend.db.models.workflow import WorkflowRun
from backend.tailored_cv import service
from backend.tailored_cv.keys import GENERATION_WORKFLOW_TYPE, GenerationKey
from backend.tailored_cv.state_builder import build_cv_state
from backend.workflows import registry
from backend.workflows import service as workflows_service
from backend.workflows.service import WorkflowError

logger = logging.getLogger(__name__)


def _generate(snapshot: dict) -> tuple[dict | None, list[dict]]:
    """Call the existing generator with the snapshot's inputs.

    The generator is async and this handler is not: the runner executes handlers
    in a worker thread (`asyncio.to_thread`) precisely so blocking work does not
    stall the API event loop, so there is no loop in this thread to reuse.
    """
    context = {
        "consultant_id": snapshot.get("consultant_username", ""),
        "profile_yaml": snapshot.get("profile_yaml", ""),
        "style_md": snapshot.get("style_md", ""),
        "job_description": snapshot.get("job_description", ""),
    }
    return asyncio.run(generate_cv_content(context))


def handle_tailored_cv_generation(db: Session, run: WorkflowRun) -> str:
    """Produce this Application's tailored CV, exactly once."""
    key = service.parse_generation_key(run.idempotency_key)
    if key is None:  # pragma: no cover - only a hand-written run row
        raise WorkflowError(
            "bad_generation_key", "the run's idempotency key is not a "
            "tailored-CV generation key"
        )

    tailored = service.get_by_id(db, key.tailored_cv_id)
    if tailored is None:
        # The Application (and with it the artifact) was erased between
        # enqueue and execution. Nothing to do and nothing wrong.
        raise WorkflowError(
            "artifact_deleted", "the tailored CV no longer exists"
        )

    # Superseded by a newer attempt, or already landed by a sibling invocation.
    # Observe rather than work: re-running the LLM here could only produce a
    # document nobody asked for, and the effect boundary would refuse it anyway.
    if (
        tailored.input_hash != key.input_hash
        or tailored.generation_attempt != key.attempt
        or not tailored.is_generating
    ):
        logger.info(
            "tailored CV %s: run %s is not the current generation; no effect",
            tailored.id,
            run.id,
        )
        return _result_ref(tailored)

    workflows_service.record_stage(db, run, "generating", progress=25)
    snapshot = dict(tailored.input_snapshot or {})

    try:
        cv_content, errors = _generate(snapshot)
    except Exception as exc:  # noqa: BLE001 - classified and recorded below
        _record_failure(db, key, exc)
        raise

    if cv_content is None:
        failure = WorkflowError(
            "generation_invalid",
            _summarize(errors) or "the generated CV did not pass validation",
        )
        _record_failure(db, key, failure)
        raise failure

    workflows_service.record_stage(db, run, "storing", progress=80)
    state = build_cv_state(
        cv_content_to_form_data(cv_content), snapshot.get("base_cv_state")
    )
    tailored, performed = service.commit_generated_state_once(db, key, state)
    if not performed:
        logger.info(
            "tailored CV %s: another invocation already committed this "
            "generation; no duplicate effect",
            tailored.id,
        )
    return _result_ref(tailored)


def _result_ref(tailored) -> str:
    return f"{service.input_ref(tailored.id)}:rev:{tailored.revision}"


def _summarize(errors: list[dict] | None) -> str:
    """A bounded, operator-safe reason from the generator's error list.

    Only the stable `kind` slugs, never the `context` text: that can quote the
    model's raw output, which can contain the prompt or the consultant's CV
    (`workflows.service._safe_error` holds the same line for exceptions).
    """
    kinds = sorted({str((e or {}).get("kind", "error")) for e in errors or []})
    return ", ".join(kinds)[:200]


def _record_failure(db: Session, key: GenerationKey, exc: BaseException) -> None:
    """Mirror the failure onto the artifact, safely and conditionally."""
    if isinstance(exc, WorkflowError):
        code, message = exc.code, exc.message
    else:
        # Never `str(exc)`: an LLM or HTTP client error can embed the prompt,
        # a CV excerpt, or a token, and this row is shown to users.
        code, message = "unexpected_error", type(exc).__name__
    try:
        db.rollback()
        service.record_generation_failure(db, key, code=code, message=message)
    except Exception:  # noqa: BLE001 - the run's own failure record still stands
        logger.exception(
            "tailored CV %s: could not record the generation failure",
            key.tailored_cv_id,
        )


# Registered against the leaf-module constant, never `service.…`: reaching into
# the service here would make registration depend on which package Python
# imported first (see `keys.py`).
registry.register(GENERATION_WORKFLOW_TYPE, handle_tailored_cv_generation)
