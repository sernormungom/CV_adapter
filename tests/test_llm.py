"""
Unit tests for the shared LLM client wrapper. No real API calls — the Anthropic
client is mocked, so these run without ANTHROPIC_API_KEY and spend no tokens.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend import llm


def test_model_constants_present_and_distinct():
    assert llm.MODEL_UPDATE and llm.MODEL_DEFAULT and llm.MODEL_CHEAP
    # The update step intentionally uses a stronger model than the default.
    assert llm.MODEL_UPDATE != llm.MODEL_DEFAULT


def test_create_message_delegates_with_model_and_passthrough(caplog):
    fake_msg = MagicMock()
    fake_msg.stop_reason = "end_turn"
    fake_msg.usage = MagicMock(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_msg)

    with patch.object(llm, "get_client", return_value=fake_client), caplog.at_level("INFO"):
        result = asyncio.run(llm.create_message(
            model="model-x", label="unit",
            max_tokens=7, system="s", messages=[{"role": "user", "content": "x"}],
        ))

    assert result is fake_msg
    fake_client.messages.create.assert_awaited_once()
    _, kwargs = fake_client.messages.create.call_args
    # model is forwarded; label is the wrapper's own arg, NOT sent to the SDK
    assert kwargs["model"] == "model-x"
    assert kwargs["max_tokens"] == 7
    assert "label" not in kwargs
    # usage is logged under the call-site label
    assert any("unit" in r.getMessage() for r in caplog.records)


def test_llm_timeout_budget_is_below_the_gateway_deadline():
    assert llm._MAX_RETRIES <= 1
    assert llm._TIMEOUT_SECONDS < 60.0
    assert llm._TIMEOUT_SECONDS <= 30.0


def test_workflow_timeout_is_generously_above_the_gateway_budget():
    # Durable workflow handlers have no CloudFront/ALB deadline (P-006), so their
    # per-call timeout must be well above the client default the two remaining
    # synchronous /api/chat* routes are held to.
    assert llm.WORKFLOW_TIMEOUT_SECONDS > llm._TIMEOUT_SECONDS
    assert llm.WORKFLOW_TIMEOUT_SECONDS >= 120.0


def _fake_message(text: str = "{}"):
    fake_msg = MagicMock()
    fake_msg.stop_reason = "end_turn"
    fake_msg.content = [MagicMock(text=text)]
    fake_msg.usage = MagicMock(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return fake_msg


def test_cv_extraction_uses_the_workflow_timeout_not_the_gateway_default():
    from backend.cv_importer import experience_extractor

    with patch.object(
        experience_extractor, "create_message",
        AsyncMock(return_value=_fake_message()),
    ) as mock_create:
        asyncio.run(experience_extractor.extract_experience("some CV text"))

    assert mock_create.call_args.kwargs["timeout"] == llm.WORKFLOW_TIMEOUT_SECONDS


def test_profile_merge_uses_the_workflow_timeout_not_the_gateway_default():
    from backend.cv_importer import profile_merger

    with patch.object(
        profile_merger, "create_message",
        AsyncMock(return_value=_fake_message()),
    ) as mock_create:
        asyncio.run(profile_merger.merge_profiles({}, {}))

    assert mock_create.call_args.kwargs["timeout"] == llm.WORKFLOW_TIMEOUT_SECONDS


def test_cv_generation_uses_the_workflow_timeout_not_the_gateway_default():
    from backend.cv_pipeline import cv_content_generator

    context = {
        "profile_yaml": "identity: {}",
        "style_md": "",
        "job_description": "Senior Engineer",
        "consultant_id": "norberto.munoz",
    }
    with patch.object(
        cv_content_generator, "create_message",
        AsyncMock(return_value=_fake_message()),
    ) as mock_create:
        asyncio.run(cv_content_generator.generate_cv_content(context))

    assert mock_create.call_args.kwargs["timeout"] == llm.WORKFLOW_TIMEOUT_SECONDS


def test_config_bootstrap_uses_the_workflow_timeout_not_the_gateway_default():
    from backend.opportunity_pipeline.learning_module import config_bootstrapper

    with patch.object(
        config_bootstrapper, "create_message",
        AsyncMock(return_value=_fake_message()),
    ) as mock_create:
        asyncio.run(config_bootstrapper._generate_config("alice", "identity: {}"))

    assert mock_create.call_args.kwargs["timeout"] == llm.WORKFLOW_TIMEOUT_SECONDS
