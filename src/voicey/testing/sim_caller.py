"""Deterministic local/cloud sim-caller and transcript judge clients."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import json5
from pydantic import ValidationError

from voicey.errors import VoiceyError
from voicey.testing.models import (
    JudgeConfig,
    ScenarioDefinition,
    ScenarioTurn,
    TestingConfig,
    TestProfile,
    TurnExpectation,
)


def load_testing_config(root: Path) -> TestingConfig:
    """Load an optional secret-free test config; local Ollama remains the default."""
    path = root / "tests" / "voicey-test.jsonc"
    if not path.exists():
        return TestingConfig()
    try:
        return TestingConfig.model_validate(json5.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        raise VoiceyError("VY-TST-001", detail=f"{path} is invalid: {exc}.") from exc


class OpenAICompatibleClient:
    """Small strict client shared by the sim caller and cited judge."""

    def __init__(
        self,
        config: JudgeConfig,
        *,
        environment: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._environment = environment if environment is not None else dict(os.environ)
        self._client = client

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        seed: int,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        del response_schema
        headers: dict[str, str] = {}
        if self.config.api_key_env is not None:
            key = self._environment.get(self.config.api_key_env)
            if not key:
                raise VoiceyError(
                    "VY-TST-003",
                    detail=f"{self.config.api_key_env} is required by the configured test model.",
                )
            headers["Authorization"] = f"Bearer {key}"
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=60)
        try:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": self.config.model,
                    "messages": messages,
                    "temperature": 0,
                    "seed": seed,
                    **({"think": False} if self.config.service == "ollama" else {}),
                },
            )
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
            choices = cast(list[dict[str, Any]], payload.get("choices"))
            content = cast(dict[str, Any], choices[0]["message"]).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("completion contains no message content")
            return content
        except VoiceyError:
            raise
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise VoiceyError(
                "VY-TST-003",
                detail=(
                    f"test model {self.config.service}/{self.config.model} at "
                    f"{self.config.base_url} is unavailable or returned invalid output."
                ),
            ) from exc
        finally:
            if owned:
                await client.aclose()


class CompletionClient(Protocol):
    """Minimal completion surface shared by model-specific test adapters."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        seed: int,
        response_schema: dict[str, Any] | None = None,
    ) -> str: ...


class AnthropicMessagesClient:
    """Strict native Anthropic Messages API client for cloud test models."""

    def __init__(
        self,
        config: JudgeConfig,
        *,
        environment: dict[str, str] | None = None,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self._environment = environment if environment is not None else dict(os.environ)
        self._client = client

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        seed: int,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        del seed  # Anthropic's Messages API does not expose a seed parameter.
        key_name = self.config.api_key_env or ""
        key = self._environment.get(key_name)
        if not key:
            raise VoiceyError(
                "VY-TST-003",
                detail=f"{key_name or 'an API key'} is required by the configured test model.",
            )

        owned = self._client is None
        client: Any = self._client
        try:
            if owned:
                from anthropic import AsyncAnthropic

                client = AsyncAnthropic(
                    api_key=key,
                    base_url=self.config.base_url,
                    timeout=60,
                    max_retries=0,
                )
            system = "\n\n".join(
                item["content"] for item in messages if item.get("role") == "system"
            )
            provider_messages = [
                {
                    "role": "assistant" if item.get("role") == "assistant" else "user",
                    "content": item["content"],
                }
                for item in messages
                if item.get("role") != "system"
            ]
            if not provider_messages:
                provider_messages.append({"role": "user", "content": "."})
            request: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": 2048,
                "messages": provider_messages,
                **({"system": system} if system else {}),
            }
            if response_schema is not None:
                request["output_config"] = {
                    "format": {"type": "json_schema", "schema": response_schema}
                }
            response: Any = await client.messages.create(
                **request,
            )
            blocks = cast(list[Any], response.content)
            content = "".join(
                block.text
                for block in blocks
                if getattr(block, "type", None) == "text" and isinstance(block.text, str)
            ).strip()
            if not content:
                raise ValueError("completion contains no message content")
            return content
        except VoiceyError:
            raise
        except Exception as exc:
            raise VoiceyError(
                "VY-TST-003",
                detail=(
                    f"test model {self.config.service}/{self.config.model} at "
                    f"{self.config.base_url} is unavailable or returned invalid output."
                ),
            ) from exc
        finally:
            if owned and client is not None:
                await cast(Awaitable[Any], client.close())


def build_model_client(
    config: JudgeConfig,
    *,
    environment: dict[str, str] | None = None,
) -> CompletionClient:
    """Build the exact configured local or cloud completion adapter."""
    if config.service == "anthropic":
        return AnthropicMessagesClient(config, environment=environment)
    return OpenAICompatibleClient(config, environment=environment)


class SimCaller:
    """Turn planner for persona-only scenarios compiled by native evaluators."""

    def __init__(self, client: CompletionClient) -> None:
        self._client = client

    async def plan(
        self,
        definition: ScenarioDefinition,
        profile: TestProfile,
    ) -> tuple[ScenarioTurn, ...]:
        if definition.turns:
            return definition.turns
        identity = json.dumps(profile.identity, sort_keys=True)
        content = await self._client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Plan only the caller side of a voice-agent evaluation. Return a JSON "
                        "array of short caller utterance strings. Include corrections and final "
                        "confirmation needed by the goals. Never write agent responses."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Persona: {definition.persona.description}\n"
                        f"Traits: {list(definition.persona.traits)}\n"
                        f"Mock identity: {identity}\n"
                        f"Goals: {list(definition.goals)}\n"
                        f"Maximum caller turns: {definition.max_turns}"
                    ),
                },
            ],
            seed=definition.seed,
            response_schema={"type": "array", "items": {"type": "string"}},
        )
        raw = _json_value(content)
        if not isinstance(raw, list) or not raw:
            raise VoiceyError("VY-TST-003", detail="sim caller returned no planned turns.")
        utterances = [
            item.strip() for item in cast(list[Any], raw) if isinstance(item, str) and item.strip()
        ]
        if not utterances or len(utterances) > definition.max_turns:
            raise VoiceyError(
                "VY-TST-003",
                detail="sim caller returned an invalid turn count.",
            )
        turns = [ScenarioTurn(user=utterance) for utterance in utterances[:-1]]
        turns.append(
            ScenarioTurn(
                user=utterances[-1],
                expect=TurnExpectation(
                    judge=definition.judge
                    or (f"advances the caller goal: {definition.goals[-1]}",),
                ),
            )
        )
        return tuple(turns)


@dataclass(frozen=True, slots=True)
class JudgeDecision:
    """A strict cited transcript decision."""

    passed: bool
    reason: str
    citations: tuple[int, ...]


class TranscriptJudge:
    """Apply scenario-level criteria and require valid transcript-line citations."""

    def __init__(self, client: CompletionClient) -> None:
        self._client = client

    async def evaluate(
        self,
        criteria: tuple[str, ...],
        transcript: tuple[str, ...],
        *,
        seed: int,
    ) -> JudgeDecision:
        if not criteria:
            return JudgeDecision(passed=True, reason="no scenario-level criteria", citations=())
        numbered = "\n".join(f"{index + 1}. {line}" for index, line in enumerate(transcript))
        content = await self._client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Evaluate all criteria against the transcript. Return JSON only: "
                        '{"passed":bool,"reason":str,"citations":[line_numbers]}. '
                        "A passing decision must cite at least one exact transcript line."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Criteria: {list(criteria)}\nTranscript:\n{numbered}",
                },
            ],
            seed=seed,
            response_schema={
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["passed", "reason", "citations"],
                "additionalProperties": False,
            },
        )
        raw = _json_value(content)
        if not isinstance(raw, dict):
            raise VoiceyError("VY-TST-003", detail="judge output is not a JSON object.")
        result = cast(dict[str, Any], raw)
        passed = result.get("passed")
        reason = result.get("reason")
        citations = result.get("citations")
        if (
            not isinstance(passed, bool)
            or not isinstance(reason, str)
            or not isinstance(citations, list)
            or any(not isinstance(line, int) for line in cast(list[Any], citations))
        ):
            raise VoiceyError("VY-TST-003", detail="judge output has an invalid shape.")
        valid = tuple(line for line in cast(list[int], citations) if 1 <= line <= len(transcript))
        if passed and not valid:
            return JudgeDecision(
                passed=False,
                reason="judge passed without a valid transcript citation",
                citations=(),
            )
        return JudgeDecision(passed=passed, reason=reason, citations=valid)


def _json_value(content: str) -> Any:
    match = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    candidate = match.group(1) if match else content
    try:
        return json.loads(candidate.strip())
    except json.JSONDecodeError as exc:
        raise VoiceyError("VY-TST-003", detail="test model did not return valid JSON.") from exc
