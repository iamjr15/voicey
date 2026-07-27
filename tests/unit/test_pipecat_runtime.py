import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.flows import FlowManager, NodeConfig
from pipecat.frames.frames import CancelFrame, EndFrame, ErrorFrame, Frame, InputDTMFFrame
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    TTFBBreakdownMetrics,
)
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.service_switcher import ServiceSwitcher
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    UserTurnMessageAddedMessage,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import ServiceSettings
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_mute import AlwaysUserMuteStrategy

from voicekit import Agent, Behavior, Limits, Models, Phone, Results, Voice, Web, tool
from voicekit.errors import VoicekitError
from voicekit.obs.latency import LatencySample
from voicekit.obs.records import TimelineEvent, ToolCallObservation, TranscriptTurn
from voicekit.results import CallResultBuffer
from voicekit.runtimes.pipecat import session as session_module
from voicekit.runtimes.pipecat.admission import AdmissionController
from voicekit.runtimes.pipecat.host import LongLivedRunner
from voicekit.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatCallLifecycle,
    PipecatLifecycleManager,
    PipecatRepository,
)
from voicekit.runtimes.pipecat.mapping import PIPECAT_CONFIG_MAPPINGS
from voicekit.runtimes.pipecat.providers import ProviderFactory
from voicekit.runtimes.pipecat.session import (
    DTMFPolicyProcessor,
    PipecatSession,
    PipecatSessionBuilder,
)
from voicekit.storage.sqlite import SQLiteRepository


@tool
async def lookup_room(name: str) -> str:
    """Look up the room assigned to a person."""
    return f"room:{name}"


def entry(_flow_manager: FlowManager) -> NodeConfig:
    return NodeConfig(
        name="entry",
        role_message="Be concise.",
        task_messages=[{"role": "developer", "content": "Help the caller."}],
        respond_immediately=False,
    )


class _PassThrough(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class _LLM(LLMService[Any]):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


@dataclass
class _LanguageSettings(ServiceSettings):
    language: str | None = None


class _Factory(ProviderFactory):
    def create_stt(self, model_id: str, voice: Voice, sample_rate: int) -> FrameProcessor:
        del model_id, voice, sample_rate
        return _PassThrough()

    def create_llm(self, model_id: str, persona: str) -> LLMService[Any]:
        del model_id, persona
        return _LLM()

    def create_tts(self, model_id: str, voice: Voice, sample_rate: int) -> FrameProcessor:
        del model_id, voice, sample_rate
        return _PassThrough()

    def language_delta(self, service: FrameProcessor, language: str) -> ServiceSettings:
        del service
        return _LanguageSettings(language=language)


class _Transport(BaseTransport):
    def __init__(self) -> None:
        super().__init__()
        self._input = _PassThrough()
        self._output = _PassThrough()
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    def input(self) -> FrameProcessor:
        return self._input

    def output(self) -> FrameProcessor:
        return self._output


class _MemoryRepository:
    def __init__(self) -> None:
        self.timeline: list[TimelineEvent] = []
        self.transcript: list[TranscriptTurn] = []
        self.tools: list[ToolCallObservation] = []
        self.latency: list[LatencySample] = []

    async def append_timeline(self, _call_id: str, event: TimelineEvent) -> None:
        self.timeline.append(event)

    async def append_transcript(self, _call_id: str, turn: TranscriptTurn) -> None:
        self.transcript.append(turn)

    async def record_tool_call(
        self,
        _call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        self.tools.append(observation)

    async def record_latency(self, _call_id: str, sample: LatencySample) -> None:
        self.latency.append(sample)


class _Transfer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, call_id: str, number: str) -> None:
        self.calls.append((call_id, number))


class _LifecycleStub:
    def __init__(self) -> None:
        self.buffer = CallResultBuffer(call_id="call_runtime")
        self.finishes: list[tuple[str, int, str | None]] = []

    async def finish(
        self,
        reason: str,
        *,
        interruptions: int = 0,
        provider_state: str | None = None,
    ) -> str:
        self.finishes.append((reason, interruptions, provider_state))
        return reason


def _agent(
    *,
    fallbacks: dict[str, str] | None = None,
    behavior: Behavior | None = None,
    limits: Limits | None = None,
    voice: Voice | None = None,
) -> Agent:
    return Agent(
        name="runtime-test",
        runtime="pipecat",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
            fallbacks=cast("Any", fallbacks or {}),
        ),
        voice=voice or Voice(),
        persona="Helpful.",
        flow=f"{__name__}:entry",
        tools=[lookup_room],
        phone=(
            Phone(provider="twilio", number="+14155550123")
            if behavior is not None and behavior.transfer_number is not None
            else None
        ),
        web=Web(enabled=True, allowed_origins=["https://app.example"]),
        results=Results(
            webhook="https://receiver.example/results",
            secret_env="RESULT_SECRET",  # pragma: allowlist secret
        ),
        limits=limits or Limits(max_duration_s=60, silence_hangup_s=10),
        behavior=behavior or Behavior(),
    )


def _session(
    agent: Agent,
    *,
    repository: _MemoryRepository | None = None,
    transfer: _Transfer | None = None,
) -> tuple[PipecatSession, _MemoryRepository]:
    store = repository or _MemoryRepository()
    builder = PipecatSessionBuilder(
        cast(PipecatRepository, store),
        provider_factory=_Factory(),
        transfer_handler=transfer,
    )
    session = builder.build(
        agent=agent,
        call=PipecatCall(call_id="call_runtime", channel="web", direction="inbound"),
        lifecycle=cast(PipecatCallLifecycle, _LifecycleStub()),
        transport=_Transport(),
        sample_rate=16000,
    )
    return session, store


def test_config_mapping_file_matches_runtime_contract() -> None:
    matrix_path = Path(__file__).parents[2] / "docs" / "runtime-config-matrix.json"
    matrix = json.loads(matrix_path.read_text())
    rows = matrix["rows"]

    assert [row["field"] for row in rows] == [mapping.field for mapping in PIPECAT_CONFIG_MAPPINGS]
    assert [row["pipecat"] for row in rows] == [
        mapping.mechanism for mapping in PIPECAT_CONFIG_MAPPINGS
    ]
    assert [row["pipecat_test"] for row in rows] == [
        mapping.test for mapping in PIPECAT_CONFIG_MAPPINGS
    ]
    assert all(row["livekit"] == "P2 pending" for row in rows)


@pytest.mark.parametrize(
    ("axis", "fallback"),
    [
        ("stt", "openai/gpt-4o-transcribe"),
        ("llm", "openai/gpt-5"),
        ("tts", "elevenlabs/flash-2.5"),
    ],
)
def test_model_fallback_axis_uses_native_switcher(axis: str, fallback: str) -> None:
    session, _ = _session(_agent(fallbacks={axis: fallback}))

    processor = getattr(session.services, axis)
    if axis == "llm":
        assert isinstance(processor, LLMSwitcher)
    else:
        assert isinstance(processor, ServiceSwitcher)
    assert len(processor.services) == 2


def test_policy_fields_reach_native_pipecat_objects() -> None:
    agent = _agent(
        limits=Limits(
            max_duration_s=90,
            max_concurrent=7,
            silence_hangup_s=12,
            daily_spend_alert_usd=4.25,
        ),
        behavior=Behavior(
            allow_interruptions=False,
            voicemail="leave_message",
            dtmf=False,
            end_call_phrases=["finish now"],
        ),
        voice=Voice(language="en", fallback_language="es", speed=1.1),
    )
    session, _ = _session(agent)

    assert session.policy.max_duration_s == 90
    assert session.policy.max_concurrent == 7
    assert session.policy.silence_hangup_s == 12
    assert session.policy.daily_spend_alert_usd == 4.25
    assert session.policy.voicemail == "leave_message"
    assert session.policy.end_call_phrases == ("finish now",)
    assert session.user_params.user_idle_timeout == 12
    assert session.pipeline_params.enable_metrics
    assert session.pipeline_params.enable_usage_metrics


def test_interruption_policy_uses_native_user_mute_strategy() -> None:
    session, _ = _session(_agent(behavior=Behavior(allow_interruptions=False)))

    assert len(session.user_params.user_mute_strategies) == 1
    assert isinstance(
        session.user_params.user_mute_strategies[0],
        AlwaysUserMuteStrategy,
    )


@pytest.mark.parametrize(("enabled", "expected"), [(False, 0), (True, 1)])
async def test_dtmf_policy_filters_native_frame(
    enabled: bool,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = DTMFPolicyProcessor(enabled=enabled)
    pushed: list[Frame] = []

    async def collect(frame: Frame, _direction: FrameDirection) -> None:
        pushed.append(frame)

    monkeypatch.setattr(processor, "push_frame", collect)
    await processor.process_frame(
        InputDTMFFrame(KeypadEntry.ONE),
        FrameDirection.DOWNSTREAM,
    )

    assert len(pushed) == expected


def test_transfer_config_adds_native_flow_tool() -> None:
    transfer = _Transfer()
    session, _ = _session(
        _agent(behavior=Behavior(transfer_number="+14155550123")),
        transfer=transfer,
    )

    assert {schema.name for schema in session.global_tools} == {
        "lookup_room",
        "transfer_to_human",
    }


async def test_language_fallback_uses_typed_service_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store = _session(_agent(voice=Voice(language="en", fallback_language="es")))
    frames: list[Frame] = []

    async def collect(frame: Frame, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        frames.append(frame)

    monkeypatch.setattr(session.worker, "queue_frame", collect)
    await session.language.activate()
    await session.language.activate()

    assert session.language.active
    assert len(frames) == 2
    assert all(cast(Any, frame).delta.language == "es" for frame in frames)
    assert [event.event_type for event in store.timeline] == ["runtime.language_fallback"]


async def test_end_phrase_event_requests_agent_hangup() -> None:
    session, store = _session(_agent(behavior=Behavior(end_call_phrases=["finish now"])))

    await session.aggregators.assistant()._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_assistant_turn_stopped",
        AssistantTurnStoppedMessage(
            content="Okay, finish now.",
            interrupted=False,
            timestamp="2026-07-27T00:00:00Z",
        ),
    )
    await session.aggregators.assistant().cleanup()

    assert session.ended_reason == "agent_hangup"
    assert store.transcript[0].text == "Okay, finish now."
    assert any(event.event_type == "runtime.end_phrase" for event in store.timeline)


async def test_duration_limit_queues_end_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _ = _session(_agent(limits=Limits(max_duration_s=10, silence_hangup_s=5)))
    slept: list[float] = []
    ended: list[str] = []

    async def no_wait(seconds: float) -> None:
        slept.append(seconds)

    async def end(_session: PipecatSession, reason: str) -> None:
        ended.append(reason)

    monkeypatch.setattr(session_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(PipecatSession, "end", end)
    await session_module._duration_limit(session)  # pyright: ignore[reportPrivateUsage]

    assert slept == [10]
    assert ended == ["duration_limit"]


async def test_native_flow_entry_initializes_flow_manager() -> None:
    session, _ = _session(_agent())

    await session.initialize_flow()
    await session.initialize_flow()

    assert session.flow_manager.current_node == "entry"


async def test_native_flow_tool_executes_with_call_context_and_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store = _session(_agent())
    queued: list[Frame] = []

    async def collect(
        frame: Frame,
        _direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        queued.append(frame)

    monkeypatch.setattr(session.worker, "queue_frame", collect)
    schema = next(item for item in session.global_tools if item.name == "lookup_room")
    result, next_node = await cast(Any, schema.handler)(
        {"name": "Ada"},
        session.flow_manager,
    )

    assert result == {"ok": True, "value": "room:Ada"}
    assert next_node is None
    assert queued == []
    assert store.tools[0].tool_name == "lookup_room"
    assert store.tools[0].arguments == {"name": "Ada"}


async def test_transfer_tool_invokes_carrier_and_queues_native_end_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer = _Transfer()
    session, _ = _session(
        _agent(behavior=Behavior(transfer_number="+14155550123")),
        transfer=transfer,
    )
    queued: list[Frame] = []

    async def collect(
        frame: Frame,
        _direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        queued.append(frame)

    monkeypatch.setattr(session.worker, "queue_frame", collect)
    schema = next(item for item in session.global_tools if item.name == "transfer_to_human")
    result, next_node = await cast(Any, schema.handler)({}, session.flow_manager)

    assert transfer.calls == [("call_runtime", "+14155550123")]
    assert isinstance(queued[0], EndFrame)
    assert queued[0].reason == "transferred"
    assert result == {"ok": True, "status": "transferred"}
    assert next_node is None


async def test_fallback_language_tool_activates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _session(_agent(voice=Voice(fallback_language="es")))
    queued: list[Frame] = []

    async def collect(
        frame: Frame,
        _direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        queued.append(frame)

    monkeypatch.setattr(session.worker, "queue_frame", collect)
    schema = next(
        item for item in session.global_tools if item.name == "switch_to_fallback_language"
    )
    result, next_node = await cast(Any, schema.handler)({}, session.flow_manager)

    assert result == {"ok": True, "language": "es"}
    assert next_node is None
    assert len(queued) == 2


async def test_observation_bridge_persists_turns_interruptions_and_latency() -> None:
    session, store = _session(_agent())

    await session.aggregators.user()._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_user_turn_message_added",
        UserTurnMessageAddedMessage(
            content="Hello",
            timestamp="2026-07-27T00:00:00Z",
        ),
    )
    await session.aggregators.assistant()._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_assistant_turn_stopped",
        AssistantTurnStoppedMessage(
            content="Hi there",
            interrupted=True,
            timestamp="2026-07-27T00:00:01Z",
        ),
    )
    await session.observations.latency_observer._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_latency_measured",
        0.42,
    )
    await session.observations.latency_observer._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_latency_breakdown",
        LatencyBreakdown(
            ttfb=[
                TTFBBreakdownMetrics(
                    processor="DeepgramSTTService",
                    start_time=1,
                    duration_secs=0.1,
                ),
                TTFBBreakdownMetrics(
                    processor="AnthropicLLMService",
                    start_time=2,
                    duration_secs=0.2,
                ),
                TTFBBreakdownMetrics(
                    processor="CartesiaTTSService",
                    start_time=3,
                    duration_secs=0.3,
                ),
                TTFBBreakdownMetrics(
                    processor="Transport",
                    start_time=4,
                    duration_secs=0.4,
                ),
            ]
        ),
    )
    await session.aggregators.user().cleanup()
    await session.aggregators.assistant().cleanup()

    assert [(turn.role, turn.text) for turn in store.transcript] == [
        ("user", "Hello"),
        ("assistant", "Hi there"),
    ]
    assert session.observations.interruptions == 1
    assert [sample.metric for sample in store.latency] == [
        "e2e",
        "stt_final",
        "llm_ttft",
        "tts_ttfb",
    ]
    assert any(event.event_type == "runtime.interrupted" for event in store.timeline)


async def test_session_wait_terminalizes_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _LifecycleStub()
    session, _ = _session(_agent())
    session.lifecycle = cast(PipecatCallLifecycle, lifecycle)

    async def fail_wait(_worker: object) -> None:
        raise RuntimeError("worker detail")

    monkeypatch.setattr(type(session.worker), "wait", fail_wait)
    event = await session.wait()

    assert event == "worker_crash"
    assert lifecycle.finishes == [("worker_crash", 0, "failed")]


async def test_real_worker_runner_completes_local_pipeline() -> None:
    lifecycle = _LifecycleStub()
    session, store = _session(_agent())
    session.lifecycle = cast(PipecatCallLifecycle, lifecycle)
    runner = LongLivedRunner()

    @session.worker.event_handler("on_pipeline_started")
    async def end_local_pipeline(  # pyright: ignore[reportUnusedFunction]
        _worker: object,
        _frame: Frame,
    ) -> None:
        await session.initialize_flow()
        await session.end("agent_hangup")

    await runner.start()
    try:
        await session.start(runner.runner)
        event = await asyncio.wait_for(session.wait(), timeout=5)
    finally:
        await runner.stop()

    assert event == "agent_hangup"
    assert lifecycle.finishes == [("agent_hangup", 0, "completed")]
    assert any(event.event_type == "runtime.pipeline_started" for event in store.timeline)
    assert any(event.event_type == "runtime.flow_initialized" for event in store.timeline)


async def test_worker_events_map_failures_and_idle_timeout() -> None:
    session, store = _session(_agent())

    await session.worker._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_pipeline_finished",
        CancelFrame(reason="cancelled"),
    )
    await asyncio.sleep(0)
    session._ended_reason = None  # pyright: ignore[reportPrivateUsage]
    await session.worker._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_pipeline_error",
        ErrorFrame(
            error="provider failed",
            fatal=True,
            processor=session.services.llm,
        ),
    )
    await asyncio.sleep(0)
    await session.worker._call_event_handler(  # pyright: ignore[reportPrivateUsage]
        "on_idle_timeout",
    )
    await asyncio.sleep(0)

    assert session.ended_reason == "llm_unavailable"
    assert [event.event_type for event in store.timeline] == [
        "runtime.pipeline_finished",
        "runtime.pipeline_error",
        "runtime.worker_idle_timeout",
    ]


async def test_language_controller_noop_and_unbound_error() -> None:
    without_fallback, _ = _session(_agent())
    await without_fallback.language.activate()
    assert not without_fallback.language.active

    with_fallback, _ = _session(_agent(voice=Voice(fallback_language="es")))
    with_fallback.language._worker = None  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(VoicekitError, match="VK-RUN-002"):
        await with_fallback.language.activate()


def test_session_builder_rejects_other_runtime_and_missing_transfer_handler() -> None:
    other_runtime = _agent().model_copy(update={"runtime": "livekit"})
    builder = PipecatSessionBuilder(cast(PipecatRepository, _MemoryRepository()))
    with pytest.raises(VoicekitError, match="VK-RUN-001"):
        builder.build(
            agent=other_runtime,
            call=PipecatCall("call_other", "web", "inbound"),
            lifecycle=cast(PipecatCallLifecycle, _LifecycleStub()),
            transport=_Transport(),
            sample_rate=16000,
        )

    with pytest.raises(VoicekitError, match="VK-RUN-002"):
        _session(_agent(behavior=Behavior(transfer_number="+14155550123")))


async def test_admission_is_atomic_and_busy_at_limit() -> None:
    admission = AdmissionController(2)

    leases = await asyncio.gather(
        admission.acquire("one"),
        admission.acquire("two"),
    )
    with pytest.raises(VoicekitError, match="VK-RUN-004"):
        await admission.acquire("three")
    assert admission.active_count == 2
    assert await admission.release(leases[0])
    assert not await admission.release(leases[0])
    await admission.acquire("three")
    assert admission.active_count == 2


async def test_lifecycle_terminalizes_once_and_releases_capacity(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    await repository.open()
    admission = AdmissionController(1)
    manager = PipecatLifecycleManager(repository, admission)
    call = PipecatCall(call_id="call_lifecycle", channel="web", direction="inbound")
    lease = await admission.acquire(call.call_id)
    lifecycle = await manager.begin(_agent(), call, lease)
    lifecycle.buffer.data["slot"] = "10:00"
    lifecycle.buffer.outcome = "booked"

    first = await lifecycle.finish("agent_hangup", interruptions=2)
    second = await lifecycle.finish("worker_crash")
    record = await repository.get_call(call.call_id)

    assert first == second
    assert record.status == "completed"
    assert record.terminal_reason == "agent_hangup"
    assert admission.active_count == 0
    assert len(await repository.list_deliveries()) == 1
    await repository.close()


async def test_lifecycle_setup_failure_is_terminal_and_observed(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "setup-failure.sqlite3")
    await repository.open()
    admission = AdmissionController(1)
    manager = PipecatLifecycleManager(repository, admission)
    call = PipecatCall(call_id="call_setup", channel="web", direction="inbound")
    lifecycle = await manager.begin(
        _agent(),
        call,
        await admission.acquire(call.call_id),
    )

    event = await lifecycle.fail_setup()
    record = await repository.get_call(call.call_id)

    assert event.event_type == "call.failed"
    assert record.terminal_reason == "setup_error"
    assert record.timeline[-1].event_type == "runtime.setup_failed"
    assert admission.active_count == 0
    await repository.close()


async def test_lifecycle_rejects_mismatched_admission_lease(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "mismatch.sqlite3")
    await repository.open()
    admission = AdmissionController(1)
    manager = PipecatLifecycleManager(repository, admission)
    lease = await admission.acquire("call_one")

    with pytest.raises(VoicekitError, match="VK-RUN-005"):
        await manager.begin(
            _agent(),
            PipecatCall(call_id="call_two", channel="web", direction="inbound"),
            lease,
        )

    assert admission.active_count == 0
    await repository.close()


async def test_lifecycle_rejects_non_json_result_without_releasing_fence(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "invalid-result.sqlite3")
    await repository.open()
    admission = AdmissionController(1)
    manager = PipecatLifecycleManager(repository, admission)
    call = PipecatCall(call_id="call_invalid", channel="web", direction="inbound")
    lease = await admission.acquire(call.call_id)
    lifecycle = await manager.begin(_agent(), call, lease)
    lifecycle.buffer.data["opaque"] = object()

    with pytest.raises(VoicekitError, match="VK-RUN-006"):
        await lifecycle.finish("agent_hangup")

    assert admission.active_count == 1
    await admission.release(lease)
    await repository.close()


async def test_lifecycle_wraps_unexpected_terminal_persistence_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "persistence-error.sqlite3")
    await repository.open()
    admission = AdmissionController(1)
    manager = PipecatLifecycleManager(repository, admission)
    call = PipecatCall(call_id="call_persist", channel="web", direction="inbound")
    lease = await admission.acquire(call.call_id)
    lifecycle = await manager.begin(_agent(), call, lease)

    async def fail_flush(*_args: object, **_kwargs: object) -> None:
        raise OSError("private storage detail")

    monkeypatch.setattr(repository, "flush_results", fail_flush)
    with pytest.raises(VoicekitError) as caught:
        await lifecycle.finish("agent_hangup")

    assert caught.value.code == "VK-RUN-006"
    assert "private storage detail" not in str(caught.value)
    assert admission.active_count == 1
    await admission.release(lease)
    await repository.close()
