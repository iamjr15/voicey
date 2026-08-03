"""Installed Pipecat Evals factories for non-OpenAI cloud judges."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from voicey.errors import VoiceyError


def anthropic_service(config: Mapping[str, Any]) -> Any:
    """Build the pinned Pipecat Anthropic service for ``EvalJudge.factory``."""
    from anthropic import AsyncAnthropic
    from pipecat.services.anthropic.llm import AnthropicLLMService

    key_name = str(config.get("api_key_env") or "ANTHROPIC_API_KEY")
    api_key = os.environ.get(key_name)
    if not api_key:
        raise VoiceyError(
            "VY-TST-003",
            detail=f"{key_name} is required by the configured Pipecat judge.",
        )
    endpoint = str(config.get("endpoint") or "https://api.anthropic.com")
    client = AsyncAnthropic(
        api_key=api_key,
        base_url=endpoint,
        timeout=60,
        max_retries=0,
    )
    return AnthropicLLMService(
        api_key=api_key,
        client=client,
        settings=AnthropicLLMService.Settings(
            model=str(config.get("model") or "claude-sonnet-5"),
            max_tokens=1024,
            thinking=AnthropicLLMService.ThinkingConfig(type="disabled"),
        ),
        retry_on_timeout=False,
    )
