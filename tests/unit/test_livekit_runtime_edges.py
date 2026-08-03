from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any, cast

import pytest
from livekit import rtc
from livekit.agents import (
    Agent as NativeAgent,
)
from livekit.agents import (
    AgentStateChangedEvent,
    CloseEvent,
    CloseReason,
    FunctionToolsExecutedEvent,
    UserStateChangedEvent,
    llm,
    stt,
    tts,
    vad,
)
from livekit.agents.inference import TurnDetector
from livekit.agents.llm import FunctionCall
from livekit.agents.voice.room_io import RoomOptions

from voicey import Agent, Models, Results, Voice, Web
from voicey.errors import VoiceyError
from voicey.runtimes.livekit.flow import load_native_agent
from voicey.runtimes.livekit.mapping import LiveKitPolicy
from voicey.runtimes.livekit.observability import LiveKitObservationBridge
from voicey.runtimes.livekit.providers import (
    DefaultLiveKitProviderFactory,
    LiveKitServices,
)
from voicey.runtimes.livekit.session import (
    LiveKitLanguageController,
    LiveKitSession,
    _close_reason,  # pyright: ignore[reportPrivateUsage]
    _cold_transfer_tool,  # pyright: ignore[reportPrivateUsage]
    _error_reason,  # pyright: ignore[reportPrivateUsage]
    _language_tool,  # pyright: ignore[reportPrivateUsage]
    _warm_transfer_tool,  # pyright: ignore[reportPrivateUsage]
)
from voicey.runtimes.livekit.token import LiveKitTokenIssuer


def _agent() -> Agent:
    return Agent(
        name="livekit-edge-test",
        runtime="livekit",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Test edge behavior.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEY_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


class MemoryStore:
    def __init__(self) -> None:
        self.timeline: list[Any] = []
        self.transcript: list[Any] = []
        self.latency: list[Any] = []

    async def append_timeline(self, _call_id: str, event: object) -> None:
        self.timeline.append(event)

    async def append_transcript(self, _call_id: str, turn: object) -> None:
        self.transcript.append(turn)

    async def record_latency(self, _call_id: str, sample: object) -> None:
        self.latency.append(sample)


@pytest.mark.asyncio
async def test_observation_bridge_attaches_all_native_events_and_drains() -> None:
    callbacks: dict[str, Any] = {}
    store = MemoryStore()
    endings: list[str] = []

    class Emitter:
        def on(self, event: str, callback: object) -> None:
            callbacks[event] = callback

    async def idle() -> None:
        endings.append("idle")

    bridge = LiveKitObservationBridge(
        call_id="call-events",
        store=cast(Any, store),
        end_call_phrases=(),
        on_user_idle=idle,
        on_end_phrase=idle,
    )
    bridge.attach(cast(Any, Emitter()))
    callbacks["user_state_changed"](UserStateChangedEvent(old_state="listening", new_state="away"))
    callbacks["agent_state_changed"](
        AgentStateChangedEvent(old_state="thinking", new_state="speaking")
    )
    callbacks["function_tools_executed"](
        FunctionToolsExecutedEvent(
            function_calls=[FunctionCall(call_id="tool-1", arguments="{}", name="lookup")],
            function_call_outputs=[None],
        )
    )
    await bridge.timeline("runtime.explicit", ok=True)
    await bridge.drain()

    assert endings == ["idle"]
    assert {event.event_type for event in store.timeline} >= {
        "runtime.user_state",
        "runtime.user_idle",
        "runtime.agent_state",
        "runtime.tools_executed",
        "runtime.explicit",
    }


@pytest.mark.asyncio
async def test_observation_bridge_surfaces_background_store_failure() -> None:
    class BrokenStore(MemoryStore):
        async def append_timeline(self, _call_id: str, event: object) -> None:
            del event
            raise OSError("disk failed")

    bridge = LiveKitObservationBridge(
        call_id="call-broken-events",
        store=cast(Any, BrokenStore()),
        end_call_phrases=(),
        on_user_idle=_noop,
        on_end_phrase=_noop,
    )
    bridge.schedule_timeline("runtime.will_fail")
    with pytest.raises(VoiceyError) as caught:
        await bridge.drain()
    assert caught.value.code == "VY-RUN-006"


async def _noop() -> None:
    return None


def test_livekit_provider_factory_all_catalog_edges() -> None:
    environment = {
        "DEEPGRAM_API_KEY": "deepgram-test",  # pragma: allowlist secret
        "OPENAI_API_KEY": "openai-test",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "anthropic-test",  # pragma: allowlist secret
        "GEMINI_API_KEY": "gemini-test",  # pragma: allowlist secret
        "CARTESIA_API_KEY": "cartesia-test",  # pragma: allowlist secret
        "ELEVENLABS_API_KEY": "eleven-test",  # pragma: allowlist secret
    }
    factory = DefaultLiveKitProviderFactory(
        environment,
        vad_model=cast(vad.VAD, object()),
    )
    voice = Voice(id="voice-id", language="en-US", speed=1.2)
    assert factory.create_vad() is not None
    assert factory.create_llm("google/gemini-2.5-flash") is not None
    assert factory.create_tts("openai/gpt-4o-mini-tts", voice) is not None
    assert factory.create_tts("cartesia/sonic-3.5", voice) is not None
    assert factory.create_tts("elevenlabs/flash-2.5", voice) is not None
    with pytest.raises(VoiceyError):
        factory.create_stt("google/unknown", voice)
    with pytest.raises(VoiceyError):
        factory.create_llm("deepgram/unknown")
    with pytest.raises(VoiceyError):
        factory.create_tts("anthropic/unknown", voice)
    with pytest.raises(VoiceyError):
        DefaultLiveKitProviderFactory({}).create_llm("anthropic/claude-sonnet-5")


@pytest.mark.asyncio
async def test_native_flow_loader_supports_async_object_and_rejects_invalid_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("voicey_edge_flow")
    native = NativeAgent(instructions="Native object.")
    module.native = native  # type: ignore[attr-defined]

    async def async_factory() -> NativeAgent:
        return NativeAgent(instructions="Native async factory.")

    def too_many(_one: object, _two: object) -> NativeAgent:
        return native

    module.async_factory = async_factory  # type: ignore[attr-defined]
    module.too_many = too_many  # type: ignore[attr-defined]
    module.not_callable = object()  # type: ignore[attr-defined]
    module.bad_result = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert (
        await load_native_agent(
            f"{module.__name__}:native",
            shared_tools=[],
        )
        is native
    )
    assert isinstance(
        await load_native_agent(
            f"{module.__name__}:async_factory",
            shared_tools=[],
        ),
        NativeAgent,
    )
    for reference in (
        f"{module.__name__}:too_many",
        f"{module.__name__}:not_callable",
        f"{module.__name__}:bad_result",
        "missing_livekit_flow:entry",
    ):
        with pytest.raises(VoiceyError) as caught:
            await load_native_agent(reference, shared_tools=[])
        assert caught.value.code == "VY-RUN-003"


@pytest.mark.parametrize(
    "values",
    [
        {"server_url": "https://project.livekit.cloud"},
        {"api_key": ""},
        {"ttl_s": 1},
        {"ttl_s": 4000},
    ],
)
def test_livekit_token_issuer_rejects_unsafe_configuration(
    values: dict[str, object],
) -> None:
    defaults: dict[str, object] = {
        "server_url": "wss://project.livekit.cloud",
        "api_key": "api-key",  # pragma: allowlist secret
        "api_secret": "api-secret",  # pragma: allowlist secret
        "agent_name": "agent",
    }
    with pytest.raises(VoiceyError) as caught:
        LiveKitTokenIssuer(**{**defaults, **values})  # type: ignore[arg-type]
    assert caught.value.code == "VY-RUN-002"


@pytest.mark.asyncio
async def test_language_controller_noop_and_unsupported_provider() -> None:
    services = LiveKitServices(
        stt=cast(stt.STT[Any], object()),
        llm=cast(llm.LLM[Any], object()),
        tts=cast(tts.TTS[Any], object()),
        vad=cast(vad.VAD, object()),
        turn_detection=cast(TurnDetector, object()),
        stt_members=(cast(stt.STT[Any], object()),),
        llm_members=(cast(llm.LLM[Any], object()),),
        tts_members=(cast(tts.TTS[Any], object()),),
    )
    store = MemoryStore()
    none = LiveKitLanguageController(
        language=None,
        services=services,
        repository=cast(Any, store),
        call_id="call-none",
    )
    await none.activate()
    assert none.active is False
    unsupported = LiveKitLanguageController(
        language="es",
        services=services,
        repository=cast(Any, store),
        call_id="call-unsupported",
    )
    with pytest.raises(VoiceyError) as caught:
        await unsupported.activate()
    assert caught.value.code == "VY-RUN-002"


class FakeLifecycle:
    def __init__(self) -> None:
        self.buffer = SimpleNamespace()
        self.finished: list[tuple[str, int, str]] = []

    async def finish(
        self,
        reason: str,
        *,
        interruptions: int,
        provider_state: str,
    ) -> object:
        self.finished.append((reason, interruptions, provider_state))
        return SimpleNamespace(event_type="call.completed")


class FakeNativeSession:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.starts = 0
        self.closes = 0

    async def start(self, _agent: object, **_kwargs: object) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("start failed")

    async def aclose(self) -> None:
        self.closes += 1


class FakeObservations:
    def __init__(self, *, fail_drain: bool = False) -> None:
        self.fail_drain = fail_drain
        self.interruptions = 2
        self.timeline_items: list[str] = []

    async def timeline(self, event: str, **_details: object) -> None:
        self.timeline_items.append(event)

    async def drain(self) -> None:
        if self.fail_drain:
            raise OSError("persistence failed")


def _session(
    *,
    native: FakeNativeSession,
    observations: FakeObservations,
    channel: str = "web",
) -> LiveKitSession:
    loop = asyncio.get_running_loop()
    services = LiveKitServices(
        stt=cast(stt.STT[Any], object()),
        llm=cast(llm.LLM[Any], object()),
        tts=cast(tts.TTS[Any], object()),
        vad=cast(vad.VAD, object()),
        turn_detection=cast(TurnDetector, object()),
        stt_members=(),
        llm_members=(),
        tts_members=(),
    )
    return LiveKitSession(
        agent=_agent(),
        call=cast(
            Any,
            SimpleNamespace(call_id="call-session-edge", channel=channel),
        ),
        lifecycle=cast(Any, FakeLifecycle()),
        services=services,
        policy=LiveKitPolicy.from_agent(_agent()),
        native_agent=object(),
        native_session=cast(Any, native),
        observations=cast(Any, observations),
        room_options=cast(RoomOptions, object()),
        global_tools=(),
        language=cast(Any, object()),
        _closed=loop.create_future(),
    )


@pytest.mark.asyncio
async def test_livekit_session_start_wait_end_and_failure_paths() -> None:
    native = FakeNativeSession()
    observations = FakeObservations()
    session = _session(native=native, observations=observations)
    with pytest.raises(VoiceyError):
        await session.wait()

    await session.start(cast(rtc.Room, object()))
    await session.start(cast(rtc.Room, object()))
    session.mark_closed(CloseEvent(reason=CloseReason.USER_INITIATED))
    session.mark_closed(CloseEvent(reason=CloseReason.ERROR))

    def report_factory(session: object) -> object:
        del session
        return {"ok": True}

    event = await session.wait(report_factory=cast(Any, report_factory))
    assert event.event_type == "call.completed"
    assert native.starts == 1
    assert observations.timeline_items == [
        "runtime.session_started",
        "runtime.session_report",
    ]
    await session.end("transferred")
    assert native.closes == 1

    failed_native = FakeNativeSession(fail_start=True)
    failed = _session(native=failed_native, observations=FakeObservations())
    with pytest.raises(RuntimeError):
        await failed.start(cast(rtc.Room, object()))
    assert failed.ended_reason == "setup_error"
    assert failed_native.closes == 1
    event = await failed.wait()
    assert event.event_type == "call.completed"

    broken_observations = FakeObservations(fail_drain=True)
    broken = _session(native=FakeNativeSession(), observations=broken_observations)
    await broken.start(cast(rtc.Room, object()))
    broken.mark_closed(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    with pytest.raises(OSError, match="persistence failed"):
        await broken.wait()
    assert broken.ended_reason == "provider_error"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (CloseEvent(reason=CloseReason.ERROR), "provider_error"),
        (CloseEvent(reason=CloseReason.JOB_SHUTDOWN), "provider_hangup"),
        (CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED), "caller_hangup"),
        (CloseEvent(reason=CloseReason.USER_INITIATED), "agent_hangup"),
        (CloseEvent(reason=CloseReason.TASK_COMPLETED), "agent_hangup"),
    ],
)
def test_livekit_close_reason_matrix(event: CloseEvent, expected: str) -> None:
    assert _close_reason(event) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (type("STTFailure", (Exception,), {})(), "stt_unavailable"),
        (type("LLMFailure", (Exception,), {})(), "llm_unavailable"),
        (type("TTSFailure", (Exception,), {})(), "tts_unavailable"),
        (RuntimeError(), "provider_error"),
    ],
)
def test_livekit_error_reason_matrix(error: Exception, expected: str) -> None:
    assert _error_reason(error) == expected


@pytest.mark.asyncio
async def test_livekit_language_cold_and_warm_tools_execute_native_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activated: list[bool] = []
    transfers: list[str] = []

    async def activate() -> None:
        activated.append(True)

    class Control:
        async def cold_transfer(self, number: str) -> None:
            transfers.append(number)

    reasons: list[str] = []
    language = _language_tool("es", activate)
    cold = _cold_transfer_tool(
        "+14155550101",
        cast(Any, Control()),
        lambda: reasons.append("cold"),
    )
    assert await cast(Any, language)() == {"ok": True, "language": "es"}
    assert await cast(Any, cold)() == {"ok": True, "status": "transferred"}

    class WarmResult:
        human_agent_identity = "human-agent"

    async def warm_task(*, sip_call_to: str) -> WarmResult:
        transfers.append(sip_call_to)
        return WarmResult()

    class NativeSession:
        def __init__(self) -> None:
            self.drains: list[bool] = []

        def shutdown(self, *, drain: bool) -> None:
            self.drains.append(drain)

    class Context:
        def __init__(self) -> None:
            self.interruptions_disabled = False
            self.session = NativeSession()

        def disallow_interruptions(self) -> None:
            self.interruptions_disabled = True

    monkeypatch.setattr(
        "voicey.runtimes.livekit.session.WarmTransferTask",
        warm_task,
    )
    context = Context()
    warm = _warm_transfer_tool(
        "+14155550102",
        lambda: reasons.append("warm"),
    )
    assert await cast(Any, warm)(cast(Any, context)) == {
        "ok": True,
        "status": "transferred",
        "human_agent_identity": "human-agent",
    }
    assert activated == [True]
    assert transfers == ["+14155550101", "+14155550102"]
    assert reasons == ["cold", "warm"]
    assert context.interruptions_disabled is True
    assert context.session.drains == [True]


def test_livekit_close_error_uses_provider_specific_reason() -> None:
    error = stt.STTError(
        timestamp=0,
        label="deepgram",
        error=RuntimeError("unavailable"),
        recoverable=False,
    )
    event = CloseEvent(reason=CloseReason.ERROR, error=error)
    assert _close_reason(event) == "stt_unavailable"
