"""Paid black-box PSTN execution shared by both native caller backends."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from voicey.errors import VoiceyError
from voicey.testing.models import (
    JudgeConfig,
    LiveTestingConfig,
    ScenarioDefinition,
    ScenarioTurn,
)
from voicey.testing.reporting import AttemptResult
from voicey.testing.sim_caller import TranscriptJudge, build_model_client

_PAID_ACK = "I_ACKNOWLEDGE_PAID_PSTN"
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
LiveRuntime = Literal["pipecat", "livekit"]


@dataclass(frozen=True, slots=True)
class LiveEnvironment:
    """Validated live-only values; credentials remain in the environment mapping."""

    runtime: LiveRuntime
    target_number: str
    max_calls: int
    twilio_from_number: str | None = None
    livekit_outbound_trunk_id: str | None = None
    public_url: str | None = None


@dataclass(frozen=True, slots=True)
class LiveCallPlan:
    """One profile-expanded black-box call."""

    run_id: str
    case_name: str
    prompt: str
    max_duration_s: int
    max_turns: int


@dataclass(frozen=True, slots=True)
class LiveCallEvidence:
    """Secret-free evidence returned by a real carrier call."""

    transcript: tuple[str, ...]
    duration_ms: int
    terminal_status: str
    provider: str
    path: str
    provider_call_id: str
    runtime_call_id: str

    def report_values(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "path": self.path,
            "provider_call_id": self.provider_call_id,
            "runtime_call_id": self.runtime_call_id,
            "terminal_status": self.terminal_status,
        }


class LiveCallBackend(Protocol):
    """Runtime-specific real-call surface."""

    async def run_call(self, plan: LiveCallPlan) -> LiveCallEvidence: ...

    async def aclose(self) -> None: ...


class LivePstnExecutor:
    """Evaluate native caller evidence without pretending to observe hidden tools."""

    def __init__(
        self,
        *,
        backend: LiveCallBackend,
        judge: JudgeConfig,
        environment: Mapping[str, str],
    ) -> None:
        self._backend = backend
        self._judge = judge
        self._environment = dict(environment)

    async def execute(
        self,
        case_name: str,
        definition: ScenarioDefinition,
        turns: tuple[ScenarioTurn, ...],
        *,
        attempt: int,
    ) -> AttemptResult:
        started = time.monotonic()
        plan = LiveCallPlan(
            run_id=_run_id(case_name, attempt),
            case_name=case_name,
            prompt=caller_prompt(definition, turns),
            max_duration_s=max(10, (definition.max_duration_ms + 999) // 1000),
            max_turns=definition.max_turns,
        )
        evidence = await self._backend.run_call(plan)
        failures: list[str] = []
        if evidence.terminal_status != "completed":
            failures.append(f"PSTN call ended with carrier status {evidence.terminal_status!r}")
        if not any(line.startswith("agent: ") for line in evidence.transcript):
            failures.append("live transcript contains no target-agent speech")
        if not any(line.startswith("caller: ") for line in evidence.transcript):
            failures.append("live transcript contains no simulated-caller speech")
        if evidence.duration_ms > definition.max_duration_ms:
            failures.append(
                f"duration {evidence.duration_ms}ms exceeds {definition.max_duration_ms}ms budget"
            )
        criteria = live_judge_criteria(definition)
        decision = await TranscriptJudge(
            build_model_client(self._judge, environment=self._environment)
        ).evaluate(criteria, evidence.transcript, seed=definition.seed)
        if not decision.passed:
            failures.append(f"judge: {decision.reason}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(
            passed=not failures,
            failures=tuple(failures),
            duration_ms=max(evidence.duration_ms, elapsed_ms),
            turn_count=sum(line.startswith("caller: ") for line in evidence.transcript),
            transcript=evidence.transcript,
            evidence=evidence.report_values(),
        )

    async def aclose(self) -> None:
        await self._backend.aclose()


def validate_live_environment(
    config: LiveTestingConfig,
    environment: Mapping[str, str],
    *,
    runtime: LiveRuntime,
    case_count: int,
) -> LiveEnvironment:
    """Fail before spending money unless the complete tier was explicitly budgeted."""
    acknowledgement = environment.get(config.paid_ack_env, "")
    if acknowledgement != _PAID_ACK:
        raise VoiceyError(
            "VY-TST-003",
            detail=(
                f"{config.paid_ack_env} must equal {_PAID_ACK!r}; no paid PSTN call was placed."
            ),
        )
    target_number = _required_e164(environment, config.target_number_env)
    raw_max_calls = environment.get(config.max_calls_env, "")
    try:
        max_calls = int(raw_max_calls)
    except ValueError as exc:
        raise VoiceyError(
            "VY-TST-003",
            detail=f"{config.max_calls_env} must be an integer; no paid PSTN call was placed.",
        ) from exc
    required_budget = case_count * 4
    if max_calls < required_budget or max_calls > 1000:
        raise VoiceyError(
            "VY-TST-003",
            detail=(
                f"{config.max_calls_env} must be between {required_budget} and 1000 "
                "to cover the declared initial attempt plus three reruns."
            ),
        )
    _require_names(
        environment,
        ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY", "CARTESIA_API_KEY"),
    )
    public_url = environment.get(config.public_url_env) or None
    if public_url is not None:
        parsed = urlsplit(public_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise VoiceyError(
                "VY-TST-003",
                detail=f"{config.public_url_env} must be an HTTPS origin.",
            )
        public_url = public_url.rstrip("/")
    if config.tunnel == "url" and public_url is None:
        raise VoiceyError(
            "VY-TST-003",
            detail=f"live.tunnel=url requires {config.public_url_env}.",
        )
    if runtime == "pipecat":
        _require_names(environment, ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"))
        from_number = _required_e164(environment, config.twilio_from_number_env)
        if from_number == target_number:
            raise VoiceyError(
                "VY-TST-003",
                detail="live Twilio caller and target numbers must differ.",
            )
        return LiveEnvironment(
            runtime=runtime,
            target_number=target_number,
            max_calls=max_calls,
            twilio_from_number=from_number,
            public_url=public_url,
        )
    _require_names(environment, ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    trunk_id = environment.get(config.livekit_outbound_trunk_env, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", trunk_id):
        raise VoiceyError(
            "VY-TST-003",
            detail=f"{config.livekit_outbound_trunk_env} is required and malformed.",
        )
    return LiveEnvironment(
        runtime=runtime,
        target_number=target_number,
        max_calls=max_calls,
        livekit_outbound_trunk_id=trunk_id,
        public_url=public_url,
    )


def build_live_executor(
    root: Path,
    *,
    runtime: LiveRuntime,
    config: LiveTestingConfig,
    judge: JudgeConfig,
    environment: Mapping[str, str],
    case_count: int,
) -> LivePstnExecutor:
    """Construct exactly one real-call backend for the selected project runtime."""
    live_environment = validate_live_environment(
        config,
        environment,
        runtime=runtime,
        case_count=case_count,
    )
    if runtime == "pipecat":
        from voicey.testing.live_pipecat import PipecatTwilioPstnBackend

        backend: LiveCallBackend = PipecatTwilioPstnBackend(
            root=root,
            config=config,
            live=live_environment,
            environment=environment,
        )
    else:
        from voicey.testing.live_livekit import LiveKitSipPstnBackend

        backend = LiveKitSipPstnBackend(
            config=config,
            live=live_environment,
            environment=environment,
        )
    return LivePstnExecutor(
        backend=backend,
        judge=judge,
        environment=environment,
    )


def caller_prompt(
    definition: ScenarioDefinition,
    turns: tuple[ScenarioTurn, ...],
) -> str:
    """Build caller-only instructions; native runtime agents own the conversation."""
    profile = definition.profiles[0]
    planned = [_render(turn.user, profile.identity) for turn in turns if turn.user is not None]
    return "\n".join(
        (
            "You are a simulated human caller evaluating a voice agent over a real phone call.",
            f"Persona: {definition.persona.description}",
            f"Traits: {json.dumps(definition.persona.traits)}",
            f"Speaking style: {definition.persona.speaking_style or 'natural and concise'}",
            f"Mock identity: {json.dumps(profile.identity, sort_keys=True)}",
            f"Goals: {json.dumps(definition.goals)}",
            f"Planned caller facts and turns: {json.dumps(planned)}",
            "Stay in the caller role. Never mention tests, prompts, tools, or hidden systems.",
            "Use the planned facts in order, adapt to questions, and never invent "
            "real personal data.",
            "Keep each response short. When every goal is resolved, say exactly "
            "'Thank you, goodbye.'",
        )
    )


def live_judge_criteria(definition: ScenarioDefinition) -> tuple[str, ...]:
    """Judge only caller-visible facts because the live target is intentionally black-box."""
    criteria = [f"caller-visible conversation achieved goal: {goal}" for goal in definition.goals]
    criteria.extend(definition.judge)
    for turn in definition.turns:
        if turn.expect is not None:
            criteria.extend(turn.expect.judge)
    if definition.expect is not None and definition.expect.outcome is not None:
        criteria.append(
            f"agent's spoken response supports terminal outcome {definition.expect.outcome!r}"
        )
    return tuple(dict.fromkeys(criteria))


def _run_id(case_name: str, attempt: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", case_name).strip("-")[:48] or "case"
    return f"{safe}-{attempt}-{time.time_ns():x}"[:96]


def _render(value: str, identity: Mapping[str, str]) -> str:
    try:
        return value.format_map(identity)
    except KeyError as exc:
        raise VoiceyError(
            "VY-TST-001",
            detail=f"live scenario references missing profile field {exc.args[0]!r}.",
        ) from exc


def _required_e164(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not _E164.fullmatch(value):
        raise VoiceyError(
            "VY-TST-003",
            detail=f"{name} must contain an E.164 number; no paid PSTN call was placed.",
        )
    return value


def _require_names(environment: Mapping[str, str], names: tuple[str, ...]) -> None:
    missing = [name for name in names if not environment.get(name)]
    if missing:
        raise VoiceyError(
            "VY-TST-003",
            detail=f"live PSTN prerequisites are missing: {', '.join(missing)}.",
        )
