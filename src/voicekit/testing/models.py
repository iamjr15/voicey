"""Runtime-neutral simulated-caller scenario contracts."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeAlias, cast

from pydantic import Field, field_validator, model_validator

from voicekit.config.models import VoicekitModel

RuntimeSelector: TypeAlias = Literal["pipecat", "livekit"]
DataPredicate: TypeAlias = Callable[[Any], bool]
ExpectedValue: TypeAlias = Any | DataPredicate


class Persona(VoicekitModel):
    """Caller identity and behavior presented to the simulator."""

    description: str
    traits: tuple[str, ...] = ()
    speaking_style: str | None = None

    @field_validator("description")
    @classmethod
    def description_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("caller description must not be blank")
        return value


class TestProfile(VoicekitModel):
    """One deterministic mock identity used to expand a scenario."""

    name: str = "default"
    identity: dict[str, str] = Field(default_factory=dict[str, str])

    @field_validator("name")
    @classmethod
    def name_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("profile name must not be blank")
        return value


class JudgeConfig(VoicekitModel):
    """OpenAI-compatible judge endpoint; local Ollama is the explicit default."""

    service: Literal["ollama", "openai"] = "ollama"
    model: str = "gemma2:9b"
    base_url: str = "http://localhost:11434/v1"
    api_key_env: str | None = None

    @model_validator(mode="after")
    def cloud_judge_has_key_reference(self) -> JudgeConfig:
        if self.service == "openai" and not self.api_key_env:
            raise ValueError("cloud judge requires api_key_env")
        if self.service == "ollama" and self.api_key_env is not None:
            raise ValueError("local Ollama judge must not declare api_key_env")
        return self


class LiveTestingConfig(VoicekitModel):
    """Secret-free controls for the paid, black-box PSTN tier."""

    tunnel: Literal["auto", "ngrok", "cloudflared", "url"] = "auto"
    port: int = Field(default=18765, ge=1024, le=65535)
    answer_timeout_s: int = Field(default=45, ge=10, le=180)
    public_url_env: str = "VOICEKIT_LIVE_PUBLIC_URL"
    target_number_env: str = "VOICEKIT_LIVE_TARGET_NUMBER"
    twilio_from_number_env: str = "VOICEKIT_LIVE_TWILIO_FROM"
    livekit_outbound_trunk_env: str = "VOICEKIT_LIVEKIT_OUTBOUND_TRUNK_ID"
    paid_ack_env: str = "VOICEKIT_LIVE_PSTN_ACK"
    max_calls_env: str = "VOICEKIT_LIVE_PSTN_MAX_CALLS"

    @field_validator(
        "public_url_env",
        "target_number_env",
        "twilio_from_number_env",
        "livekit_outbound_trunk_env",
        "paid_ack_env",
        "max_calls_env",
    )
    @classmethod
    def valid_environment_reference(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is None:
            raise ValueError("live test environment references must be uppercase names")
        return value


class TestingConfig(VoicekitModel):
    """Secret-free project test configuration."""

    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    sim_caller: JudgeConfig = Field(default_factory=JudgeConfig)
    live: LiveTestingConfig = Field(default_factory=LiveTestingConfig)


class ToolExpectation(VoicekitModel):
    """A native function call expected during one simulated turn."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict[str, Any])
    runtimes: frozenset[RuntimeSelector] = Field(
        default_factory=lambda: frozenset({"pipecat", "livekit"})
    )

    model_config = VoicekitModel.model_config | {"arbitrary_types_allowed": True}

    @field_validator("name")
    @classmethod
    def name_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("tool expectation name must not be blank")
        return value


class SendAfter(VoicekitModel):
    """Optional pacing or interruption anchor for a caller turn."""

    event: str | None = None
    delay_ms: int = Field(default=0, ge=0, le=60_000)


class TurnExpectation(VoicekitModel):
    """Goal-based native assertions for one turn."""

    tools: tuple[ToolExpectation, ...] = ()
    text_contains: str | None = None
    judge: tuple[str, ...] = ()
    within_ms: int | None = Field(default=None, gt=0, le=300_000)
    handoff: str | None = None

    @model_validator(mode="after")
    def has_an_assertion(self) -> TurnExpectation:
        if not any((self.tools, self.text_contains, self.judge, self.handoff)):
            raise ValueError("turn expectation must contain at least one assertion")
        return self


class ScenarioTurn(VoicekitModel):
    """One scripted caller utterance or an observation-only opening turn."""

    user: str | None = None
    expect: TurnExpectation | None = None
    send_after: SendAfter | None = None

    @model_validator(mode="after")
    def turn_has_input_or_expectation(self) -> ScenarioTurn:
        if self.user is None and self.expect is None:
            raise ValueError("turn must contain user input or an expectation")
        if self.send_after is not None and self.user is None:
            raise ValueError("send_after requires user input")
        return self


class ResultExpectation(VoicekitModel):
    """Hard business-result assertions evaluated after the conversation."""

    outcome: str | None = None
    data: dict[str, ExpectedValue] = Field(default_factory=dict[str, ExpectedValue])

    model_config = VoicekitModel.model_config | {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def has_an_assertion(self) -> ResultExpectation:
        if self.outcome is None and not self.data:
            raise ValueError("result expectation must contain outcome or data")
        return self


class ScenarioMetrics(VoicekitModel):
    """Hard turn-count and wall-clock budgets."""

    max_turns: int = Field(default=24, ge=1, le=200)
    max_duration_ms: int = Field(default=120_000, gt=0, le=3_600_000)


class ScenarioDefinition(VoicekitModel):
    """Validated scenario returned by an owned ``@scenario`` function."""

    name: str
    caller: str | Persona
    goals: tuple[str, ...]
    expect: ResultExpectation | None = None
    judge: tuple[str, ...] = ()
    max_turns: int = Field(default=24, ge=1, le=200)
    max_duration_ms: int = Field(default=120_000, gt=0, le=3_600_000)
    seed: int = 7
    profiles: tuple[TestProfile, ...] = (TestProfile(),)
    turns: tuple[ScenarioTurn, ...] = ()

    model_config = VoicekitModel.model_config | {"arbitrary_types_allowed": True}

    @field_validator("name")
    @classmethod
    def name_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("scenario name must not be blank")
        return value

    @field_validator("goals", "judge")
    @classmethod
    def strings_are_present(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values):
            raise ValueError("scenario goals and judge criteria must not be blank")
        return values

    @model_validator(mode="after")
    def scenario_is_actionable(self) -> ScenarioDefinition:
        if not self.goals:
            raise ValueError("scenario requires at least one goal")
        profile_names = [profile.name for profile in self.profiles]
        if len(profile_names) != len(set(profile_names)):
            raise ValueError("scenario profile names must be unique")
        if len(self.turns) > self.max_turns:
            raise ValueError("scripted turns exceed max_turns")
        return self

    @property
    def persona(self) -> Persona:
        if isinstance(self.caller, Persona):
            return self.caller
        return Persona(description=self.caller)

    @property
    def metrics(self) -> ScenarioMetrics:
        return ScenarioMetrics(
            max_turns=self.max_turns,
            max_duration_ms=self.max_duration_ms,
        )


def matches_expected_data(
    expected: Mapping[str, ExpectedValue],
    actual: Mapping[str, Any],
) -> list[str]:
    """Return stable human-readable failures for nested result-data checks."""
    failures: list[str] = []
    for path, wanted in expected.items():
        found, value = _lookup(actual, path)
        if not found:
            failures.append(f"data.{path} is missing")
            continue
        if callable(wanted):
            try:
                matched = bool(wanted(value))
            except Exception as exc:
                failures.append(f"data.{path} predicate raised {type(exc).__name__}")
                continue
            if not matched:
                failures.append(f"data.{path} did not satisfy its predicate")
        elif value != wanted:
            failures.append(f"data.{path} expected {wanted!r}, got {value!r}")
    return failures


def _lookup(value: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = cast(Any, current[part])
    return True, current
