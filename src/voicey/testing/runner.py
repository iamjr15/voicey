"""Unified scenario selection, flake policy, and native runtime execution."""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from voicey import results
from voicey.config.manifest import ManifestStore
from voicey.config.models import Agent
from voicey.errors import VoiceyError
from voicey.obs.records import ToolCallObservation
from voicey.storage.sqlite import SQLiteRepository
from voicey.testing.discovery import discover_scenarios
from voicey.testing.livekit import (
    LiveKitTurn,
    assert_native_turn_events,
    compile_livekit,
    judge_native_turn,
)
from voicey.testing.models import (
    JudgeConfig,
    ScenarioDefinition,
    ScenarioTurn,
    TestProfile,
    matches_expected_data,
)
from voicey.testing.pipecat import compile_pipecat
from voicey.testing.reporting import AttemptResult, CaseResult, SuiteResult
from voicey.testing.sim_caller import (
    SimCaller,
    TranscriptJudge,
    build_model_client,
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
    if live and audio:
        raise VoiceyError(
            "VY-TST-003",
            detail="--live and --audio are distinct paid and local tiers; select exactly one.",
        )
    manifest = ManifestStore(root / "voicey.jsonc").load()
    definitions = discover_scenarios(root)
    if filter_text:
        definitions = tuple(
            definition
            for definition in definitions
            if filter_text.casefold() in definition.name.casefold()
        )
    if not definitions:
        raise VoiceyError("VY-TST-001", detail="the scenario filter matched no cases.")

    config = load_testing_config(root)
    env = environment if environment is not None else dict(os.environ)
    active_executor = executor
    live_executor: Any | None = None
    if active_executor is None:
        if live:
            from voicey.testing.live import build_live_executor

            live_executor = build_live_executor(
                root,
                runtime=manifest.runtime,
                config=config.live,
                judge=config.judge,
                environment=env,
                case_count=sum(len(definition.profiles) for definition in definitions),
            )
            active_executor = live_executor
        else:
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
    tier = "live" if live else ("audio" if audio else "text")
    cases: list[CaseResult] = []
    try:
        planner = SimCaller(build_model_client(config.sim_caller, environment=env))
        planned: dict[tuple[str, str], tuple[ScenarioTurn, ...]] = {}
        for definition in definitions:
            for profile in definition.profiles:
                planned[(definition.name, profile.name)] = await planner.plan(definition, profile)

        for definition in definitions:
            for profile in definition.profiles:
                case_name = f"{definition.name}[{profile.name}]"
                turns = tuple(
                    turn
                    for turn in planned[(definition.name, profile.name)]
                    if manifest.runtime in turn.runtimes
                )
                if not turns:
                    raise VoiceyError(
                        "VY-TST-002",
                        detail=f"{case_name} has no turns for {manifest.runtime}.",
                    )
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
                        tier=tier,
                        attempts=attempts,
                    )
                )
    finally:
        if live_executor is not None:
            await live_executor.aclose()
    return SuiteResult(
        runtime=manifest.runtime,
        tier=tier,
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
        self.run_id = uuid.uuid4().hex

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
        run_dir = self.root / ".voicey" / "test-runs" / self.run_id / f"{safe_name}-{attempt}"
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
                pipecat_transcript(
                    cast(list[dict[Any, Any]], native.result.events_seen),
                    debug_log=native.result.debug_log,
                    turns=turns,
                    profile=definition.profiles[0],
                )
            )
        snapshot = await result_snapshot(
            run_dir / "results.sqlite3",
            f"call_eval_{_safe_name(definition.name + '_' + definition.profiles[0].name)}",
            wait_for_terminal_s=5.0,
        )
        failures.extend(hard_result_failures(definition, snapshot))
        duration = native.duration_ms or (native.result.duration_ms if native.result else 0)
        if duration > definition.max_duration_ms:
            failures.append(f"duration {duration}ms exceeds {definition.max_duration_ms}ms budget")
        try:
            decision = await TranscriptJudge(
                build_model_client(self.judge, environment=self.environment)
            ).evaluate(definition.judge, tuple(transcript), seed=definition.seed)
        except VoiceyError as exc:
            failures.append(f"judge failed with {exc.code}")
        else:
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
            from voicey.testing.livekit_audio import execute_audio_case

            return await execute_audio_case(
                self.root,
                definition,
                turns,
                judge=self.judge,
                environment=self.environment,
            )
        from livekit.agents import AgentSession, function_tool
        from livekit.plugins import openai

        from voicey.runtimes.livekit.flow import load_native_agent
        from voicey.runtimes.livekit.providers import (
            DefaultLiveKitProviderFactory,
            Sonnet5AnthropicLLM,
            build_livekit_services,
        )
        from voicey.runtimes.livekit.tools import shared_livekit_tools

        with project_modules(self.root, self.environment):
            agent = load_project_agent()
            services = build_livekit_services(
                agent,
                factory=DefaultLiveKitProviderFactory(self.environment),
            )
            buffer = results.CallResultBuffer(call_id=f"call_test_{_safe_name(case_name)}")
            sink = MemorySink()
            tools: list[Any] = list(
                shared_livekit_tools(
                    agent.tools,
                    call_id=buffer.call_id,
                    buffer=buffer,
                    sink=sink,
                )
            )

            async def transfer_to_human() -> dict[str, object]:
                """Simulate a completed transfer in the local text tier."""
                return {"ok": True, "status": "transferred"}

            async def warm_transfer_to_human() -> dict[str, object]:
                """Simulate a completed private-briefing warm transfer."""
                return {"ok": True, "status": "transferred"}

            expected_tool_names = {
                tool.name
                for turn in turns
                if turn.expect is not None
                for tool in turn.expect.tools
                if "livekit" in tool.runtimes
            }
            transfer_tool = (
                warm_transfer_to_human
                if "warm_transfer_to_human" in expected_tool_names
                else transfer_to_human
            )
            transfer_tool_name = transfer_tool.__name__
            if not any(
                getattr(getattr(tool, "info", None), "name", None) == transfer_tool_name
                for tool in tools
            ):
                tools.append(function_tool(transfer_tool))
            native = await load_native_agent(agent.flow, shared_tools=list(tools))
            session: AgentSession[Any] = AgentSession(
                llm=services.llm,
                max_tool_steps=3,
            )
            if self.judge.service == "ollama":
                judge_llm = openai.LLM(
                    model=self.judge.model,
                    base_url=self.judge.base_url,
                    api_key="ollama",
                    temperature=0,
                    timeout=httpx.Timeout(60.0),
                    max_retries=0,
                    extra_body={"think": False},
                )
            elif self.judge.service == "anthropic":
                judge_llm = Sonnet5AnthropicLLM(
                    model=self.judge.model,
                    base_url=self.judge.base_url,
                    api_key=self.environment.get(self.judge.api_key_env or "", ""),
                    max_tokens=256,
                    timeout=httpx.Timeout(60.0),
                )
            else:
                judge_llm = openai.LLM(
                    model=self.judge.model,
                    base_url=self.judge.base_url,
                    api_key=self.environment.get(self.judge.api_key_env or "", ""),
                    temperature=0,
                    timeout=httpx.Timeout(60.0),
                    max_retries=0,
                )
            failures: list[str] = []
            transcript: list[str] = []
            native_turns: list[tuple[Any, LiveKitTurn]] = []

            async def run_conversation() -> None:
                compiled = compile_livekit(
                    (definition,),
                    planned_turns={(definition.name, definition.profiles[0].name): turns},
                )[0]

                def arm_interrupt(marker: Any) -> tuple[asyncio.Event, Callable[[Any], None]]:
                    signal = asyncio.Event()

                    def on_agent_state(event: Any) -> None:
                        if marker.event == "llm_started" and event.new_state == "thinking":
                            signal.set()

                    session.on("agent_state_changed", on_agent_state)
                    if marker.event is None:
                        signal.set()
                    return signal, on_agent_state

                async def apply_interrupt(marker: Any, signal: asyncio.Event) -> None:
                    if marker.event not in {None, "llm_started"}:
                        failures.append(f"unsupported LiveKit send_after event {marker.event!r}")
                        return
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=5)
                    except TimeoutError:
                        failures.append(
                            f"LiveKit send_after event {marker.event!r} was not observed"
                        )
                        return
                    if marker.delay_ms:
                        await asyncio.sleep(marker.delay_ms / 1000)
                    await asyncio.wait_for(session.interrupt(force=True), timeout=5)

                opening_marker = compiled.turns[0].source.send_after if compiled.turns else None
                if opening_marker is None:
                    opening = cast(
                        Any,
                        await session.start(native, capture_run=True, record=False),
                    )
                else:

                    async def start_session() -> Any:
                        return cast(
                            Any,
                            await session.start(
                                native,
                                capture_run=True,
                                record=False,
                            ),
                        )

                    signal, callback = arm_interrupt(opening_marker)
                    start_task = asyncio.create_task(start_session())
                    try:
                        await apply_interrupt(opening_marker, signal)
                    finally:
                        with suppress(KeyError, ValueError):
                            session.off("agent_state_changed", callback)
                    opening = await start_task
                transcript.extend(livekit_transcript(opening.events))

                for index, turn in enumerate(compiled.turns):
                    if turn.user is None:
                        continue
                    transcript.append(f"caller: {turn.user}")
                    turn_started = time.monotonic()
                    next_marker = (
                        compiled.turns[index + 1].source.send_after
                        if index + 1 < len(compiled.turns)
                        else None
                    )
                    if next_marker is None:
                        result = cast(Any, await session.run(user_input=turn.user))
                    else:
                        signal, callback = arm_interrupt(next_marker)
                        pending = cast(Awaitable[Any], session.run(user_input=turn.user))
                        try:
                            await apply_interrupt(next_marker, signal)
                        finally:
                            with suppress(KeyError, ValueError):
                                session.off("agent_state_changed", callback)
                        result = await pending
                    turn_duration_ms = int((time.monotonic() - turn_started) * 1000)
                    transcript.extend(livekit_transcript(result.events))
                    native_turns.append((result, turn))
                    try:
                        assert_native_turn_events(result, turn)
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

            conversation_started = time.monotonic()
            try:
                await asyncio.wait_for(
                    run_conversation(),
                    timeout=definition.max_duration_ms / 1000,
                )
            except TimeoutError:
                failures.append(f"scenario exceeded {definition.max_duration_ms}ms hard timeout")
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                suffix = f" (status {status})" if isinstance(status, int) else ""
                failures.append(f"LiveKit native run failed with {type(exc).__name__}{suffix}")
            finally:
                duration = int((time.monotonic() - conversation_started) * 1000)
                try:
                    await asyncio.wait_for(session.aclose(), timeout=10)
                except TimeoutError:
                    failures.append("LiveKit session close exceeded 10s timeout")
                except Exception as exc:
                    failures.append(f"LiveKit session close failed with {type(exc).__name__}")
                for result, turn in native_turns:
                    try:
                        await judge_native_turn(result, turn, judge_llm=judge_llm)
                    except AssertionError as exc:
                        failures.append(str(exc))
                    except Exception as exc:
                        failures.append(f"LiveKit native judge failed with {type(exc).__name__}")
                await _close_livekit_test_resources(
                    (
                        judge_llm,
                        *getattr(services, "stt_members", ()),
                        *getattr(services, "llm_members", ()),
                        *getattr(services, "tts_members", ()),
                    ),
                    failures,
                )
        failures.extend(hard_result_failures(definition, buffer.snapshot()))
        if duration > definition.max_duration_ms:
            failures.append(f"duration {duration}ms exceeds {definition.max_duration_ms}ms budget")
        try:
            decision = await TranscriptJudge(
                build_model_client(self.judge, environment=self.environment)
            ).evaluate(definition.judge, tuple(transcript), seed=definition.seed)
        except VoiceyError as exc:
            failures.append(f"judge failed with {exc.code}")
        else:
            if not decision.passed:
                failures.append(f"judge: {decision.reason}")
        return AttemptResult(
            passed=not failures,
            failures=tuple(failures),
            duration_ms=duration,
            turn_count=len(turns),
            transcript=tuple(transcript),
        )


async def _close_livekit_test_resources(
    resources: tuple[Any, ...],
    failures: list[str],
) -> None:
    seen: set[int] = set()
    for resource in resources:
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "aclose", None)
        if not callable(close):
            continue
        try:
            await asyncio.wait_for(cast(Awaitable[Any], close()), timeout=10)
        except TimeoutError:
            failures.append(f"{type(resource).__name__} close exceeded 10s timeout")
        except Exception as exc:
            failures.append(f"{type(resource).__name__} close failed with {type(exc).__name__}")


async def result_snapshot(
    path: Path,
    call_id: str,
    *,
    wait_for_terminal_s: float = 0.0,
) -> dict[str, Any]:
    if not await asyncio.to_thread(path.exists):
        return {"outcome": None, "data": {}}
    deadline = time.monotonic() + wait_for_terminal_s
    async with SQLiteRepository(path) as repository:
        while True:
            try:
                snapshot = await repository.get_result_snapshot(call_id)
                call = await repository.get_call(call_id)
            except VoiceyError as exc:
                if exc.code != "VY-OBS-003":
                    raise
                if time.monotonic() >= deadline:
                    return {"outcome": None, "data": {}}
            else:
                if call.status != "active" or time.monotonic() >= deadline:
                    return snapshot.model_dump(mode="python")
            await asyncio.sleep(0.1)


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


def pipecat_transcript(
    events: list[dict[Any, Any]],
    *,
    debug_log: list[str] | None = None,
    turns: tuple[ScenarioTurn, ...] = (),
    profile: TestProfile | None = None,
) -> list[str]:
    if debug_log and turns and profile is not None:
        responses = [
            event.get("text") or event.get("transcript")
            for event in events
            if str(event.get("event") or event.get("type") or "") == "llm_response"
        ]
        response_index = 0
        emitted_turns: set[int] = set()
        transcript: list[str] = []
        for line in debug_log:
            match = re.search(r"\[\s*t(?P<turn>\d+)\]", line)
            if "send:" in line and match is not None:
                turn_index = int(match.group("turn"))
                if turn_index < len(turns) and turn_index not in emitted_turns:
                    user = turns[turn_index].user
                    if user is not None:
                        transcript.append(f"caller: {user.format_map(profile.identity)}")
                    emitted_turns.add(turn_index)
            if "event: llm_response" in line and response_index < len(responses):
                response = responses[response_index]
                response_index += 1
                if isinstance(response, str) and response:
                    transcript.append(f"agent: {response}")
        return transcript

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
        raise VoiceyError("VY-TST-002", detail="agent.py must export voicey.Agent as agent.")
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
