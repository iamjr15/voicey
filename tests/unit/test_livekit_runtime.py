from __future__ import annotations

import sys
import types
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from livekit import api, rtc
from livekit.agents import Agent as NativeAgent
from livekit.agents import (
    CloseEvent,
    CloseReason,
    ConversationItemAddedEvent,
    RunContext,
    ToolError,
    function_tool,
    llm,
    stt,
    tts,
    vad,
)
from livekit.agents.inference import TurnDetector
from livekit.agents.llm import ChatMessage
from livekit.plugins import anthropic, cartesia, deepgram, elevenlabs, openai

from voicekit import Agent, Behavior, Models, Phone, Results, Web, results, tool
from voicekit.errors import VoicekitError
from voicekit.obs import LatencySample
from voicekit.obs.records import TimelineEvent, ToolCallObservation, TranscriptTurn
from voicekit.runtimes.livekit import (
    LiveKitAdmissionGate,
    LiveKitCall,
    LiveKitLifecycleManager,
    LiveKitTokenIssuer,
    shared_livekit_tools,
)
from voicekit.runtimes.livekit.flow import load_native_agent
from voicekit.runtimes.livekit.mapping import LIVEKIT_CONFIG_MAPPINGS, LiveKitPolicy
from voicekit.runtimes.livekit.observability import LiveKitObservationBridge
from voicekit.runtimes.livekit.providers import (
    DefaultLiveKitProviderFactory,
    LiveKitServices,
    build_livekit_services,
)
from voicekit.runtimes.livekit.session import LiveKitLanguageController, LiveKitSessionBuilder
from voicekit.runtimes.pipecat.admission import AdmissionController
from voicekit.storage.sqlite import SQLiteRepository


class MemoryToolSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, ToolCallObservation]] = []

    async def record(self, call_id: str, observation: ToolCallObservation) -> None:
        self.items.append((call_id, observation))


@tool
def livekit_test_lookup(query: str) -> str:
    """Look up one deterministic value for runtime assembly tests."""
    return query


class FakeRunContext:
    def __init__(self) -> None:
        self.interruptions_disabled = False
        self.fillers: list[str] = []

    def disallow_interruptions(self) -> None:
        self.interruptions_disabled = True

    @asynccontextmanager
    async def with_filler(self, text: str) -> AsyncGenerator[None]:
        self.fillers.append(text)
        yield


class MemoryObservationStore:
    def __init__(self) -> None:
        self.timeline: list[TimelineEvent] = []
        self.transcript: list[TranscriptTurn] = []
        self.latency: list[LatencySample] = []

    async def append_timeline(self, _call_id: str, event: TimelineEvent) -> None:
        self.timeline.append(event)

    async def append_transcript(self, _call_id: str, turn: TranscriptTurn) -> None:
        self.transcript.append(turn)

    async def record_latency(self, _call_id: str, sample: LatencySample) -> None:
        self.latency.append(sample)


def _agent() -> Agent:
    return Agent(
        name="livekit-test",
        runtime="livekit",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Help callers test the LiveKit runtime.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


@pytest.mark.asyncio
async def test_shared_tool_uses_native_raw_schema_filler_and_mutation_boundary() -> None:
    sink = MemoryToolSink()
    buffer = results.CallResultBuffer(call_id="call_livekit_tool")
    context = FakeRunContext()

    @tool(say_while_running="I am reserving that.", mutating=True)
    def reserve(slot: str) -> dict[str, str]:
        """Reserve one caller-confirmed slot."""
        results.set("slot", slot)
        return {"slot": slot}

    native = shared_livekit_tools(
        [reserve],
        call_id=buffer.call_id,
        buffer=buffer,
        sink=sink,
    )[0]
    value = await native(
        cast("RunContext[Any]", context),
        {"slot": "2030-01-02T10:00:00Z"},
    )

    assert value == {"slot": "2030-01-02T10:00:00Z"}
    assert native.info.raw_schema == {
        "name": "reserve",
        "description": "Reserve one caller-confirmed slot.",
        "parameters": {
            "type": "object",
            "properties": {"slot": {"type": "string"}},
            "additionalProperties": False,
            "required": ["slot"],
        },
    }
    assert context.interruptions_disabled is True
    assert context.fillers == ["I am reserving that."]
    assert buffer.data == {"slot": "2030-01-02T10:00:00Z"}
    assert sink.items[0][0] == buffer.call_id
    assert sink.items[0][1].status == "succeeded"


@pytest.mark.asyncio
async def test_shared_tool_maps_failures_to_safe_native_tool_error() -> None:
    sink = MemoryToolSink()
    buffer = results.CallResultBuffer(call_id="call_livekit_failed_tool")
    context = FakeRunContext()

    @tool
    def fail_lookup(query: str) -> str:
        """Fail without exposing the provider exception."""
        raise RuntimeError(f"private credential in {query}")

    native = shared_livekit_tools(
        [fail_lookup],
        call_id=buffer.call_id,
        buffer=buffer,
        sink=sink,
    )[0]
    with pytest.raises(ToolError) as caught:
        await native(cast("RunContext[Any]", context), {"query": "secret"})

    assert "tool_failed" in caught.value.message
    assert "credential" not in caught.value.message
    assert "secret" not in caught.value.message
    assert context.interruptions_disabled is False
    assert sink.items[0][1].status == "failed"


@pytest.mark.asyncio
async def test_livekit_lifecycle_stamps_runtime_and_uses_shared_fencing(
    tmp_path: Path,
) -> None:
    repository = await SQLiteRepository(tmp_path / "calls.sqlite3").open()
    admission = AdmissionController(1)
    call = LiveKitCall(
        call_id="call_livekit_lifecycle",
        channel="web",
        direction="inbound",
        provider="livekit",
    )
    lease = await admission.acquire(call.call_id)
    lifecycle = await LiveKitLifecycleManager(
        repository,
        admission,
        owner_id="livekit_worker",
    ).begin(_agent(), call, lease)

    event = await lifecycle.finish("caller_hangup")
    record = await repository.get_call(call.call_id)
    await repository.close()

    assert event.event_type == "call.completed"
    assert record.runtime == "livekit"
    assert record.status == "completed"
    assert admission.active_count == 0


@pytest.mark.asyncio
async def test_livekit_observations_flush_incrementally_with_native_metrics() -> None:
    store = MemoryObservationStore()
    end_reasons: list[str] = []
    bridge = LiveKitObservationBridge(
        call_id="call_livekit_observations",
        store=cast(Any, store),
        end_call_phrases=("goodbye",),
        on_user_idle=lambda: _append(end_reasons, "idle"),
        on_end_phrase=lambda: _append(end_reasons, "end_phrase"),
    )
    user = ChatMessage(
        role="user",
        content=["Please help me."],
        metrics={"transcription_delay": 0.120},
    )
    assistant = ChatMessage(
        role="assistant",
        content=["Goodbye for now."],
        interrupted=True,
        metrics={
            "llm_node_ttft": 0.210,
            "tts_node_ttfb": 0.095,
            "e2e_latency": 0.640,
        },
    )

    await bridge.on_conversation_item(ConversationItemAddedEvent(item=user))
    await bridge.on_conversation_item(ConversationItemAddedEvent(item=assistant))
    await bridge.drain()

    assert [(turn.role, turn.text) for turn in store.transcript] == [
        ("user", "Please help me."),
        ("assistant", "Goodbye for now."),
    ]
    assert {(sample.metric, sample.duration_ms) for sample in store.latency} == {
        ("stt_final", 120),
        ("llm_ttft", 210),
        ("tts_ttfb", 95),
        ("e2e", 640),
    }
    assert bridge.interruptions == 1
    assert end_reasons == ["end_phrase"]


async def _append(target: list[str], value: str) -> None:
    target.append(value)


def test_livekit_provider_catalog_uses_installed_plugins() -> None:
    factory = DefaultLiveKitProviderFactory(
        {
            "DEEPGRAM_API_KEY": "dg-test",  # pragma: allowlist secret
            "OPENAI_API_KEY": "openai-test",  # pragma: allowlist secret
            "ANTHROPIC_API_KEY": "anthropic-test",  # pragma: allowlist secret
            "GEMINI_API_KEY": "gemini-test",  # pragma: allowlist secret
            "CARTESIA_API_KEY": "cartesia-test",  # pragma: allowlist secret
            "ELEVENLABS_API_KEY": "eleven-test",  # pragma: allowlist secret
        },
        vad_model=cast(vad.VAD, object()),
    )
    voice = _agent().voice

    assert isinstance(factory.create_stt("deepgram/nova-3", voice), deepgram.STT)
    assert isinstance(factory.create_stt("openai/gpt-4o-transcribe", voice), openai.STT)
    assert isinstance(factory.create_llm("anthropic/claude-sonnet-5"), anthropic.LLM)
    assert isinstance(factory.create_llm("openai/gpt-5"), openai.LLM)
    assert isinstance(factory.create_tts("cartesia/sonic-3.5", voice), cartesia.TTS)
    assert isinstance(factory.create_tts("elevenlabs/flash-2.5", voice), elevenlabs.TTS)
    assert isinstance(factory.create_turn_detector(), TurnDetector)


def test_livekit_provider_fallbacks_use_native_adapters() -> None:
    agent = _agent().model_copy(
        update={
            "models": Models(
                stt="deepgram/nova-3",
                llm="anthropic/claude-sonnet-5",
                tts="cartesia/sonic-3.5",
                fallbacks={
                    "stt": "openai/gpt-4o-transcribe",
                    "llm": "openai/gpt-5",
                    "tts": "elevenlabs/flash-2.5",
                },
            )
        }
    )
    factory = DefaultLiveKitProviderFactory(
        {
            "DEEPGRAM_API_KEY": "dg-test",  # pragma: allowlist secret
            "OPENAI_API_KEY": "openai-test",  # pragma: allowlist secret
            "ANTHROPIC_API_KEY": "anthropic-test",  # pragma: allowlist secret
            "CARTESIA_API_KEY": "cartesia-test",  # pragma: allowlist secret
            "ELEVENLABS_API_KEY": "eleven-test",  # pragma: allowlist secret
        },
        vad_model=cast(vad.VAD, object()),
    )
    services = build_livekit_services(agent, factory=factory)

    assert isinstance(services.stt, stt.FallbackAdapter)
    assert isinstance(services.llm, llm.FallbackAdapter)
    assert isinstance(services.tts, tts.FallbackAdapter)
    assert len(services.stt_members) == len(services.llm_members) == len(services.tts_members) == 2


@pytest.mark.asyncio
async def test_native_livekit_flow_loader_accepts_only_agent_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("voicekit_test_livekit_flow")

    async def authored_tool() -> str:
        """Return authored workflow evidence."""
        return "authored"

    async def shared_tool() -> str:
        """Return engine-injected evidence."""
        return "shared"

    authored = function_tool(authored_tool)
    shared = function_tool(shared_tool)

    def entry(tools: list[llm.Tool | llm.Toolset]) -> NativeAgent:
        assert tools == [shared]
        return NativeAgent(instructions="Use native LiveKit workflows.", tools=[authored])

    module.entry = entry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    native = await load_native_agent(
        f"{module.__name__}:entry",
        shared_tools=[shared],
    )
    names = [tool.info.name for tool in native.tools if isinstance(tool, llm.FunctionTool)]

    assert names == ["authored_tool", "shared_tool"]


def test_livekit_policy_uses_consolidated_turn_handling_and_complete_matrix() -> None:
    policy = LiveKitPolicy.from_agent(_agent())
    handling = policy.turn_handling(cast(Any, TurnDetector(version="v1-mini")))
    interruption = handling.get("interruption")
    endpointing = handling.get("endpointing")

    assert interruption is not None
    assert interruption.get("enabled") is True
    assert endpointing is not None
    assert endpointing.get("mode") == "dynamic"
    assert handling.get("turn_detection") is not None
    assert {mapping.field for mapping in LIVEKIT_CONFIG_MAPPINGS} == {
        "models.fallbacks.stt",
        "models.fallbacks.llm",
        "models.fallbacks.tts",
        "limits.max_duration_s",
        "limits.max_concurrent",
        "limits.silence_hangup_s",
        "limits.daily_spend_alert_usd",
        "behavior.allow_interruptions",
        "behavior.voicemail",
        "behavior.dtmf",
        "behavior.transfer_number",
        "behavior.end_call_phrases",
        "voice.fallback_language",
        "phone.record",
        "observability.prometheus_enabled",
        "observability.prometheus_bind",
        "observability.prometheus_port",
        "observability.prometheus_path",
        "observability.otlp_endpoint",
        "observability.otlp_headers_env",
    }


@pytest.mark.asyncio
async def test_livekit_language_fallback_updates_all_compatible_members() -> None:
    class LanguageService:
        def __init__(self) -> None:
            self.languages: list[str] = []

        def update_options(self, *, language: str) -> None:
            self.languages.append(language)

    stt_member = LanguageService()
    tts_member = LanguageService()
    store = MemoryObservationStore()
    services = LiveKitServices(
        stt=cast(stt.STT[Any], stt_member),
        llm=cast(llm.LLM[Any], object()),
        tts=cast(tts.TTS[Any], tts_member),
        vad=cast(vad.VAD, object()),
        turn_detection=cast(TurnDetector, object()),
        stt_members=(cast(stt.STT[Any], stt_member),),
        llm_members=(cast(llm.LLM[Any], object()),),
        tts_members=(cast(tts.TTS[Any], tts_member),),
    )
    controller = LiveKitLanguageController(
        language="es",
        services=services,
        repository=cast(Any, store),
        call_id="call_language",
    )

    await controller.activate()
    await controller.activate()

    assert stt_member.languages == ["es"]
    assert tts_member.languages == ["es"]
    assert store.timeline[0].event_type == "runtime.language_fallback"


@pytest.mark.asyncio
async def test_livekit_token_reserves_before_dispatch_and_mints_agent_claim() -> None:
    gate = LiveKitAdmissionGate(1, reservation_ttl_s=1)
    await gate.reserve("call_token")
    with pytest.raises(VoicekitError) as full:
        await gate.reserve("call_other")
    assert getattr(full.value, "code", None) == "VK-RUN-004"
    assert await gate.admit("job_1", "call_token") is True
    await gate.release("job_1")

    issuer = LiveKitTokenIssuer(
        server_url="wss://project.livekit.cloud",
        api_key="test-key",  # pragma: allowlist secret
        api_secret="test-secret-that-is-more-than-long-enough",  # pragma: allowlist secret
        agent_name="livekit-test",
    )
    issued = issuer.issue(
        call_id="call_token",
        room_name="call-token-room",
        participant_identity="caller-token",
        metadata={"channel": "web", "direction": "inbound"},
    )
    claims = api.TokenVerifier(
        "test-key",
        "test-secret-that-is-more-than-long-enough",  # pragma: allowlist secret
    ).verify(issued.participant_token)

    assert claims.identity == "caller-token"
    assert claims.video is not None
    assert claims.room_config is not None
    assert claims.video.room == "call-token-room"
    assert claims.video.room_join is True
    assert claims.room_config.agents[0].agent_name == "livekit-test"


@pytest.mark.asyncio
async def test_livekit_policy_reaches_native_session_and_transfer_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    callbacks: dict[str, object] = {}

    class FakeNativeSession:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def on(self, event: str, callback: object) -> None:
            callbacks[event] = callback

        async def start(self, _agent: object, **_kwargs: object) -> None:
            return None

        async def aclose(self) -> None:
            return None

    class FakeProviderFactory:
        def create_stt(self, _model_id: str, _voice: object) -> stt.STT[Any]:
            return cast(stt.STT[Any], object())

        def create_llm(self, _model_id: str) -> llm.LLM[Any]:
            return cast(llm.LLM[Any], object())

        def create_tts(self, _model_id: str, _voice: object) -> tts.TTS[Any]:
            return cast(tts.TTS[Any], object())

        def create_vad(self) -> vad.VAD:
            return cast(vad.VAD, object())

        def create_turn_detector(self) -> TurnDetector:
            return cast(TurnDetector, object())

    class FakeControl:
        async def cold_transfer(self, number: str) -> None:
            del number
            return None

        async def send_dtmf(self, digits: str) -> None:
            del digits
            return None

    flow_module = types.ModuleType("voicekit_test_session_flow")

    def flow_entry() -> NativeAgent:
        return NativeAgent(instructions="Use the native session.")

    flow_module.__dict__["entry"] = flow_entry
    monkeypatch.setitem(sys.modules, flow_module.__name__, flow_module)
    monkeypatch.setattr(
        "voicekit.runtimes.livekit.session.AgentSession",
        FakeNativeSession,
    )
    agent = _agent().model_copy(
        update={
            "flow": f"{flow_module.__name__}:entry",
            "tools": [livekit_test_lookup],
            "phone": Phone(
                provider="twilio",
                number="+14155550100",
                inbound=True,
                outbound=True,
                record=True,
            ),
            "behavior": Behavior(
                allow_interruptions=False,
                dtmf=True,
                transfer_number="+14155550101",
            ),
        }
    )
    repository = await SQLiteRepository(tmp_path / "calls.sqlite3").open()
    admission = AdmissionController(1)
    call = LiveKitCall(
        call_id="call_native_mapping",
        channel="phone",
        direction="inbound",
        provider="twilio",
    )
    lease = await admission.acquire(call.call_id)
    lifecycle = await LiveKitLifecycleManager(repository, admission).begin(
        agent,
        call,
        lease,
    )

    session = await LiveKitSessionBuilder(
        repository,
        provider_factory=cast(Any, FakeProviderFactory()),
        call_control=FakeControl(),
    ).build(agent=agent, call=call, lifecycle=lifecycle)
    await session.start(cast(rtc.Room, object()))
    cast(Any, callbacks["close"])(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))

    def report_factory(session: object) -> object:
        del session
        return {"supplement": True}

    event = await session.wait(report_factory=cast(Any, report_factory))
    await repository.close()

    handling = cast(dict[str, Any], captured["turn_handling"])
    names = [
        cast(Any, native).info.name for native in session.global_tools if hasattr(native, "info")
    ]
    assert captured["user_away_timeout"] == 30.0
    assert captured["ivr_detection"] is True
    assert handling["interruption"]["enabled"] is False
    assert session.policy.record is True
    assert event.event_type == "call.completed"
    assert session.ended_reason == "caller_hangup"
    assert {
        "livekit_test_lookup",
        "transfer_to_human",
        "warm_transfer_to_human",
        "send_dtmf_events",
    }.issubset(names)
    assert {"close", "error"}.issubset(callbacks)
