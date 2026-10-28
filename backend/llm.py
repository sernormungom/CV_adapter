"""
Central Anthropic client + model registry for every LLM call site.

One shared AsyncAnthropic instance (connection pooling + uniform retry/timeout),
one place to choose models, and token-usage logging on every call so spend is
visible in the app logs.

The client is created lazily so importing this module never requires an API key
(DB/migration tooling imports the package without ANTHROPIC_API_KEY set; validation
belongs at the call site, per backend/config.py).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)


def opaque_label(prefix: str, identifier: str) -> str:
    """`f"{prefix}:{hash}"` for use in LLM call labels/logs instead of raw PII.

    GDPR Art 5(1)(c) minimisation: labels like ``chat:{username}`` used to put
    a raw username into every LLM-call log line. The hash is deterministic
    (same identifier -> same label, useful for grepping one user's calls
    across a log file) but not reversible to the original value.
    """
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"

# ── Model registry — change a model in ONE place ─────────────────────────────
MODEL_UPDATE = "claude-opus-4-8"             # learning-module config update (quality step)
MODEL_DEFAULT = "claude-sonnet-4-6"          # extraction, merge, CV generation, bootstrap, chat
MODEL_CHEAP = "claude-haiku-4-5-20251001"    # profile patcher (mechanical, tool-driven)

# ── Client tuning ────────────────────────────────────────────────────────────
# The client default must fit the deployed 60s CloudFront/ALB gateway budget,
# because /api/chat and /api/chat/profile-patch (backend/cv_assistant/) still
# call the model synchronously on the request path — a longer timeout there
# just lets the edge answer 504 before the work completes, and a big retry
# budget amplifies that by extending the total wall time.
_MAX_RETRIES = 1        # one retry only; fail fast before the edge times out
_TIMEOUT_SECONDS = 30.0

# Durable workflow handlers (backend/cv_importer/, backend/cv_pipeline/) have
# no such deadline — P-006 moved them off the request path for exactly this
# reason, so a slow model response should be retried generously, not raced
# against an edge timeout that no longer applies. Measured: a profile-merge
# call against a full existing profile (max_tokens=8000) genuinely exceeded
# 30s and was cut off mid-response rather than completing. Call sites running
# inside a workflow handler must pass `timeout=WORKFLOW_TIMEOUT_SECONDS`
# explicitly to `create_message()` rather than relying on the client default.
WORKFLOW_TIMEOUT_SECONDS = 170.0

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    """Return the shared AsyncAnthropic client, created on first use."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=_MAX_RETRIES, timeout=_TIMEOUT_SECONDS)
    return _client


async def create_message(*, model: str, label: str, **kwargs: Any) -> "anthropic.types.Message":
    """
    Thin wrapper over `messages.create` that logs token usage uniformly.

    `label` identifies the call site in logs (e.g. "cv_generate", "config_update").
    All other keyword args (system, messages, max_tokens, tools, ...) pass through.

    By design this wrapper does NOT parse or validate the model's output. Response
    parsing/validation is schema-specific (cv-content, profile and matching-config are
    different shapes), so it stays at each call site. Replacing that with enforced
    structured output / tool-use is the planned Phase 3 change — intentionally out of
    scope for this shared client.
    """
    client = get_client()
    message = await client.messages.create(model=model, **kwargs)
    usage = message.usage
    logger.info(
        "LLM call [%s] model=%s stop=%s in=%s out=%s cache_read=%s cache_write=%s",
        label,
        model,
        message.stop_reason,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
        getattr(usage, "cache_read_input_tokens", None),
        getattr(usage, "cache_creation_input_tokens", None),
    )
    return message
