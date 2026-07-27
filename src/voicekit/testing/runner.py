"""Unified scenario selection, flake policy, and native runtime execution."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from voicekit import results
from voicekit.config.manifest import ManifestStore
from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.obs.records import ToolCallObservation
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.testing.discovery import discover_scenarios
from voicekit.testing.livekit import assert_native_turn, compile_livekit
from voicekit.testing.models import (
    JudgeConfig,
    ScenarioDefinition,
    ScenarioTurn,
    matches_expected_data,
)
from voicekit.testing.pipecat import compile_pipecat
from voicekit.testing.reporting import AttemptResult, CaseResult, SuiteResult
from voicekit.testing.sim_caller import (
    OpenAICompatibleClient,
    SimCaller,
    TranscriptJudge,
    load_testing_config,
)

AttemptExecutor = Callable[[str, int], Awaitable[AttemptResult]]


class CaseExecutor(Protocol):
    """Injectable native runtime execution surface."""

    async def execute(
        self,
        case_name: str,
        definition: ScenarioDefinition,
        turns: tuple[ScenarioTurn, ...],
        *,
        attempt: int,
    ) -> AttemptResult: ...


async def run_project_tests(
    root: Path,
    *,
    filter_text: str | None = None,
    audio: bool = False,
    live: bool = False,
    executor: CaseExecutor | None = None,
    environment: dict[str, str] | None = None,
) -> SuiteResult:
    """Discover, compile, execute, and strictly report one project's scenarios."""
    if live:
        raise VoicekitError(
            "VK-TST-003",
            detail="PSTN loopback is not installed until P3; no lower tier was substituted.",
        )
    manifest = ManifestStore(root / "voicekit.jsonc").load()
    definitions = discover_scenarios(root)
    if filter_text:
        definitions = tuple(
            definition
            for definition in definitions
            if filter_text.casefold() in definition.name.casefold()
        )
    if not definitions:
        raise VoicekitError("VK-TST-001", detail="the scenario filter matched no cases.")

    config = load_testing_config(root)
    env = environment if environment is not None else dict(os.environ)
    planner = SimCaller(OpenAICompatibleClient(config.sim_caller, environment=env))
    planned: dict[tuple[str, str], tuple[ScenarioTurn, ...]] = {}
    for definition in definitions:
        for profile in definition.profiles:
            planned[(definition.name, profile.name)] = await planner.plan(definition, profile)

    active_executor = executor
    if active_executor is None:
        active_executor = (
            PipecatExecutor(
                root,
                audio=audio,
                judge=config.judge,
                environment=env,
            )
            if manifest.runtime == "pipecat"
            else LiveKitExecutor(
                root,
                audio=audio,
                judge=config.judge,
                environment=env,
            )
        )
    cases: list[CaseResult] = []
    for definition in definitions:
        for profile in definition.profiles:
            case_name = f"{definition.name}[{profile.name}]"
            turns = planned[(definition.name, profile.name)]
            narrowed = definition.model_copy(update={"profiles": (profile,)})

            async def run_attempt(
                attempt: int,
                *,
                _name: str = case_name,
                _definition: ScenarioDefinition = narrowed,
                _turns: tuple[ScenarioTurn, ...] = turns,
            ) -> AttemptResult:
                return await active_executor.execute(
                    _name,
                    _definition,
                    _turns,
                    attempt=attempt,
                )

            attempts = await _with_flake_policy(run_attempt)
            cases.append(
                CaseResult(
                    name=case_name,
                    runtime=manifest.runtime,
                    tier="audio" if audio else "text",
                    attempts=attempts,
                )
            )
    return SuiteResult(
        runtime=manifest.runtime,
        tier="audio" if audio else "text",
        cases=tuple(cases),
    )


async def _with_flake_policy(
    execute: Callable[[int], Awaitable[AttemptResult]],
) -> tuple[AttemptResult, ...]:
    first = await execute(1)
    if first.passed:
        return (first,)
    reruns = [await execute(attempt) for attempt in range(2, 5)]
    return (first, *reruns)


class PipecatExecutor:
    """Run generated native Pipecat EvalSuite inputs and inspect durable results."""

    def __init__(
        self,
        root: Path,
        *,
        audio: bool,
        judge: JudgeConfig,
        environment: dict[str, str],
    ) -> None:
        self.root = root
        self.audio = audio
        self.judge = judge
        self.environment = environment

    async def execute(
        self,
        case_name: str,
        definition: ScenarioDefinition,
        turns: tuple[ScenarioTurn, ...],
        *,
        attempt: int,
    ) -> AttemptResult:
        from pipecat.evals.suite import EvalManifest, EvalSuite

        safe_name = _safe_name(case_name)
        run_dir = self.root / ".voicekit" / "test-runs" / f"{safe_name}-{attempt}"
        compiled = compile_pipecat(
            (definition.model_copy(update={"profiles": (definition.profiles[0],)}),),
            output_dir=run_dir,
            bot=None,
            project_root=self.root,
            audio=self.audio,
            judge=self.judge,
            planned_turns={(definition.name, definition.profiles[0].name): turns},
        )
        suite = EvalSuite(EvalManifest.load(compiled.manifest))
        await suite.run(run_dir / "logs")
        native = suite.runs[0]
        failures: list[str] = []
        transcript: list[str] = []
        if native.error:
            failures.append(native.error)
        if native.result is None:
            failures.append("Pipecat Evals returned no result")
        else:
            if getattr(native.result, "skipped", False):
                failures.append("Pipecat Evals skipped the native scenario")
            failures.extend(str(failure) for failure in native.result.failures)
            transcript.extend(
                pipecat_transcript(cast(list[dict[Any, Any]], native.result.events_seen))
            )
        snapshot = await result_snapshot(
            run_dir / "results.sqlite3",
            f"call_eval_{_safe_name(definition.name + '_' + definition.profiles[0].name)}",
        )
        failures.extend(hard_result_failures(definition, snapshot))
        duration = native.duration_ms or (native.result.duration_ms if native.result else 0)
        if duration > definition.max_duration_ms:
            failures.append(f"duration {duration}ms exceeds {definition.max_duration_ms}ms budget")
        decision = await TranscriptJudge(
            OpenAICompatibleClient(self.judge, environment=self.environment)
        ).evaluate(definition.judge, tuple(transcript), seed=definition.seed)
        if not decision.passed:
            failures.append(f"judge: {decision.reason}")
        return AttemptResult(
            passed=not failures,
            failures=tuple(failures),
            duration_ms=duration,
            turn_count=len(turns),
            transcript=tuple(transcript),
        )


class MemorySink:
    def __init__(self) -> None:
        self.observations: list[ToolCallObservation] = []

    async def record(self, call_id: str, observation: ToolCallObservation) -> None:
        del call_id
        self.observations.append(observation)


class LiveKitExecutor:
    """Run native LiveKit sessions; audio uses the dedicated PCM bridge."""

    def __init__(
        self,
        root: Path,
        *,
        audio: bool,
        judge: JudgeConfig,
        environment: dict[str, str],
    ) -> None:
        self.root = root
        self.audio = audio
        self.judge = judge
        self.environment = environment

    async def execute(
        self,
        case_name: str,
        definition: ScenarioDefinition,
        turns: tuple[ScenarioTurn, ...],
        *,
        attempt: int,
    ) -> AttemptResult:
        del attempt
        if self.audio:
            from voicekit.testing.livekit_audio import execute_audio_case

            return await execute_audio_case(
                self.root,
                definition,
                turns,
                judge=self.judge,
                environment=self.environment,
            )
        from livekit.agents import AgentSession
        from livekit.plugins import openai

        from voicekit.runtimes.livekit.flow import load_native_agent
        from voicekit.runtimes.livekit.providers import (
            DefaultLiveKitProviderFactory,
            build_livekit_services,
        )
        from voicekit.runtimes.livekit.tools import shared_livekit_tools

        started = time.monotonic()
        with project_modules(self.root, self.environment):
            agent = load_project_agent()
            services = build_livekit_services(
                agent,
                factory=DefaultLiveKitProviderFactory(self.environment),
            )
            buffer = results.CallResultBuffer(call_id=f"call_test_{_safe_name(case_name)}")
            sink = MemorySink()
            tools = shared_livekit_tools(
                agent.tools,
                call_id=buffer.call_id,
                buffer=buffer,
                sink=sink,
            )
            native = await load_native_agent(agent.flow, shared_tools=list(tools))
            session: AgentSession[Any] = AgentSession(
                llm=services.llm,
                max_tool_steps=3,
            )
            judge_llm = (
                openai.LLM.with_ollama(
                    model=self.judge.model,
                    base_url=self.judge.base_url,
                    temperature=0,
                )
                if self.judge.service == "ollama"
                else openai.LLM(
                    model=self.judge.model,
                    base_url=self.judge.base_url,
                    api_key=self.environment.get(self.judge.api_key_env or "", ""),
                    temperature=0,
                )
            )
            failures: list[str] = []
            transcript: list[str] = []
            try:
                opening = cast(
                    Any,
                    await session.start(native, capture_run=True, record=False),
                )
                transcript.extend(livekit_transcript(opening.events))
                compiled = compile_livekit(
                    (definition,),
                    planned_turns={(definition.name, definition.profiles[0].name): turns},
                )[0]
                for turn in compiled.turns:
                    if turn.user is None:
                        continue
                    turn_started = time.monotonic()
                    result = cast(Any, await session.run(user_input=turn.user))
                    turn_duration_ms = int((time.monotonic() - turn_started) * 1000)
                    transcript.extend(livekit_transcript(result.events))
                    try:
                        await assert_native_turn(result, turn, judge_llm=judge_llm)
                    except AssertionError as exc:
                        failures.append(str(exc))
                    expectation = turn.source.expect
                    if (
                        expectation is not None
                        and expectation.within_ms is not None
                        and turn_duration_ms > expectation.within_ms
                    ):
                        failures.append(
                            f"turn duration {turn_duration_ms}ms exceeds "
                            f"{expectation.within_ms}ms budget"
                        )
            finally:
                await session.aclose()
        duration = int((time.monotonic() - started) * 1000)
        failures.extend(hard_result_failures(definition, buffer.snapshot()))
        if duration > definition.max_duration_ms:
            failures.append(f"duration {duration}ms exceeds {definition.max_duration_ms}ms budget")
        decision = await TranscriptJudge(
            OpenAICompatibleClient(self.judge, environment=self.environment)
        ).evaluate(definition.judge, tuple(transcript), seed=definition.seed)
        if not decision.passed:
            failures.append(f"judge: {decision.reason}")
        return AttemptResult(
            passed=not failures,
            failures=tuple(failures),
            duration_ms=duration,
            turn_count=len(turns),
            transcript=tuple(transcript),
        )


async def result_snapshot(path: Path, call_id: str) -> dict[str, Any]:
    if not await asyncio.to_thread(path.exists):
        return {"outcome": None, "data": {}}
    async with SQLiteRepository(path) as repository:
        try:
            snapshot = await repository.get_result_snapshot(call_id)
        except VoicekitError as exc:
            if exc.code == "VK-OBS-003":
                return {"outcome": None, "data": {}}
            raise
    return snapshot.model_dump(mode="python")


def hard_result_failures(
    definition: ScenarioDefinition,
    snapshot: Mapping[str, Any],
) -> list[str]:
    expected = definition.expect
    if expected is None:
        return []
    value = dict(snapshot)
    failures: list[str] = []
    if expected.outcome is not None and value.get("outcome") != expected.outcome:
        failures.append(f"outcome expected {expected.outcome!r}, got {value.get('outcome')!r}")
    data = value.get("data")
    if not isinstance(data, dict):
        failures.append("result data is not an object")
    else:
        failures.extend(matches_expected_data(expected.data, cast(dict[str, Any], data)))
    return failures


def pipecat_transcript(events: list[dict[Any, Any]]) -> list[str]:
    transcript: list[str] = []
    for event in events:
        name = str(event.get("event") or event.get("type") or "")
        text = event.get("text") or event.get("transcript")
        if isinstance(text, str) and text:
            role = "caller" if "user" in name else "agent"
            transcript.append(f"{role}: {text}")
    return transcript


def livekit_transcript(events: list[Any]) -> list[str]:
    transcript: list[str] = []
    for event in events:
        if getattr(event, "type", None) != "message":
            continue
        item = event.item
        text = item.text_content
        if text:
            transcript.append(f"{item.role}: {text}")
    return transcript


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "_-" else "_" for char in value.casefold())


def load_project_agent() -> Agent:
    module = importlib.import_module("agent")
    agent = getattr(module, "agent", None)
    if not isinstance(agent, Agent):
        raise VoicekitError("VK-TST-002", detail="agent.py must export voicekit.Agent as agent.")
    return agent


@contextmanager
def project_modules(root: Path, environment: dict[str, str]):
    text = str(root)
    original = {name: os.environ.get(name) for name in environment}
    sys.path.insert(0, text)
    os.environ.update(environment)
    try:
        yield
    finally:
        for module_name in ("agent", "flow", "tools"):
            sys.modules.pop(module_name, None)
        if text in sys.path:
            sys.path.remove(text)
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
