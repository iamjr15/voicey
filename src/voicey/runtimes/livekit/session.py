"""Per-call LiveKit AgentSession assembly, observation, and terminalization."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast

from livekit import rtc
from livekit.agents import (
    AgentSession,
    CloseEvent,
    CloseReason,
    ErrorEvent,
    RunContext,
    function_tool,
)
from livekit.agents import llm as lk_llm
from livekit.agents.beta import tools as beta_tools
from livekit.agents.beta.workflows import WarmTransferTask
from livekit.agents.voice.room_io import RoomOptions

from voicey import results
from voicey.config.models import Agent
from voicey.errors import VoiceyError
from voicey.obs.records import TimelineEvent
from voicey.runtimes.livekit.flow import load_native_agent
from voicey.runtimes.livekit.lifecycle import (
    LiveKitCall,
    LiveKitCallLifecycle,
    LiveKitRepository,
)
from voicey.runtimes.livekit.mapping import LiveKitPolicy, detector_mode
from voicey.runtimes.livekit.observability import LiveKitObservationBridge
from voicey.runtimes.livekit.providers import (
    DefaultLiveKitProviderFactory,
    LiveKitProviderFactory,
    LiveKitServices,
    build_livekit_services,
)
from voicey.runtimes.livekit.tools import shared_livekit_tools
from voicey.storage.models import EndedReason, PersistedEvent
from voicey.tools import RepositoryToolObservationSink, ToolExecutor


class LiveKitCallControl(Protocol):
    """Call-local SIP controls exposed as native tools."""

    async def cold_transfer(self, number: str) -> None: ...

    async def send_dtmf(self, digits: str) -> None: ...


class SessionReportFactory(Protocol):
    """Installed JobContext report surface kept injectable for tests."""

    def __call__(self, session: AgentSession[Any]) -> object: ...


@dataclass(slots=True)
class LiveKitSession:
    """One native AgentSession joined to a fenced durable call lifecycle."""

    agent: Agent
    call: LiveKitCall
    lifecycle: LiveKitCallLifecycle
    services: LiveKitServices
    policy: LiveKitPolicy
    native_agent: Any
    native_session: AgentSession[Any]
    observations: LiveKitObservationBridge
    room_options: RoomOptions
    global_tools: tuple[lk_llm.Tool | lk_llm.Toolset, ...]
    language: LiveKitLanguageController
    _closed: asyncio.Future[CloseEvent]
    _ended_reason: EndedReason | None = None
    _duration_task: asyncio.Task[None] | None = None
    _started: bool = False

    @property
    def ended_reason(self) -> EndedReason | None:
        return self._ended_reason

    @property
    def started(self) -> bool:
        return self._started

    def set_reason(self, reason: EndedReason) -> None:
        if self._ended_reason is None or reason in _FAILURE_REASONS:
            self._ended_reason = reason

    async def start(self, room: rtc.Room) -> None:
        """Start native media only after lifecycle/admission already exist."""
        if self._started:
            return
        try:
            with results.result_context(self.lifecycle.buffer):
                await self.native_session.start(
                    self.native_agent,
                    room=room,
                    room_options=cast(Any, self.room_options),
                    record=self.policy.record,
                )
            self._started = True
            await self.observations.timeline("runtime.session_started")
            self._duration_task = asyncio.create_task(
                self._duration_limit(),
                name=f"voicey-livekit-duration-{self.call.call_id}",
            )
        except Exception:
            self.set_reason("setup_error")
            with suppress(Exception):
                await self.native_session.aclose()
            raise

    async def wait(
        self,
        *,
        report_factory: SessionReportFactory | None = None,
    ) -> PersistedEvent:
        """Await native close, flush incremental work, then terminalize once."""
        if not self._started and self.ended_reason != "setup_error":
            raise VoiceyError(
                "VY-RUN-006",
                detail=f"LiveKit session {self.call.call_id} was not started.",
            )
        if self._started:
            await self._closed
        await self._cancel_duration_timer()
        observation_error: Exception | None = None
        try:
            await self.observations.drain()
            if report_factory is not None:
                report = report_factory(self.native_session)
                await self.observations.timeline(
                    "runtime.session_report",
                    report_type=type(report).__name__,
                )
        except Exception as exc:
            observation_error = exc
            self.set_reason("provider_error")
        reason = self._ended_reason or (
            "caller_hangup" if self.call.channel == "phone" else "agent_hangup"
        )
        event = await self.lifecycle.finish(
            reason,
            interruptions=self.observations.interruptions,
            provider_state="completed" if reason in _NORMAL_REASONS else "failed",
        )
        if observation_error is not None:
            raise observation_error
        return event

    async def end(self, reason: EndedReason) -> None:
        self.set_reason(reason)
        if self._started:
            await self.native_session.aclose()

    def mark_closed(self, event: CloseEvent) -> None:
        """Resolve the native close event once."""
        if not self._closed.done():
            self._closed.set_result(event)

    async def _duration_limit(self) -> None:
        await asyncio.sleep(self.policy.max_duration_s)
        await self.end("duration_limit")

    async def _cancel_duration_timer(self) -> None:
        if self._duration_task is None:
            return
        self._duration_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._duration_task
        self._duration_task = None


class LiveKitLanguageController:
    """Apply fallback language through each provider's native update surface."""

    def __init__(
        self,
        *,
        language: str | None,
        services: LiveKitServices,
        repository: LiveKitRepository,
        call_id: str,
    ) -> None:
        self.language = language
        self._services = services
        self._repository = repository
        self._call_id = call_id
        self._active = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def activate(self) -> None:
        if self.language is None:
            return
        async with self._lock:
            if self._active:
                return
            updated = 0
            for service in (*self._services.stt_members, *self._services.tts_members):
                update = getattr(service, "update_options", None)
                if update is None or "language" not in inspect.signature(update).parameters:
                    continue
                result = update(language=self.language)
                if inspect.isawaitable(result):
                    await result
                updated += 1
            if updated == 0:
                raise VoiceyError(
                    "VY-RUN-002",
                    detail="configured LiveKit providers cannot update language at runtime.",
                )
            self._active = True
            await self._repository.append_timeline(
                self._call_id,
                TimelineEvent(
                    event_type="runtime.language_fallback",
                    details={"language": self.language, "services": updated},
                ),
            )


class LiveKitSessionBuilder:
    """Assemble only installed-pin native LiveKit objects from canonical config."""

    def __init__(
        self,
        repository: LiveKitRepository,
        *,
        provider_factory: LiveKitProviderFactory | None = None,
        call_control: LiveKitCallControl | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self.repository = repository
        self.provider_factory = provider_factory or DefaultLiveKitProviderFactory()
        self.call_control = call_control
        self.tool_executor = tool_executor or ToolExecutor()

    async def build(
        self,
        *,
        agent: Agent,
        call: LiveKitCall,
        lifecycle: LiveKitCallLifecycle,
    ) -> LiveKitSession:
        if agent.runtime != "livekit":
            raise VoiceyError(
                "VY-RUN-001",
                detail=f"cannot build LiveKit session for runtime {agent.runtime!r}.",
            )
        services = build_livekit_services(agent, factory=self.provider_factory)
        policy = LiveKitPolicy.from_agent(agent)
        native_session: AgentSession[Any] = AgentSession(
            stt=services.stt,
            vad=services.vad,
            llm=services.llm,
            tts=services.tts,
            turn_handling=policy.turn_handling(detector_mode(services.turn_detection)),
            user_away_timeout=float(policy.silence_hangup_s),
            max_tool_steps=3,
            ivr_detection=bool(agent.phone),
        )
        closed: asyncio.Future[CloseEvent] = asyncio.get_running_loop().create_future()
        holder: dict[str, LiveKitSession] = {}

        async def on_idle() -> None:
            await holder["session"].end("silence_timeout")

        async def on_end_phrase() -> None:
            await holder["session"].end("agent_hangup")

        observations = LiveKitObservationBridge(
            call_id=call.call_id,
            store=self.repository,
            end_call_phrases=policy.end_call_phrases,
            on_user_idle=on_idle,
            on_end_phrase=on_end_phrase,
        )
        observations.attach(native_session)
        language = LiveKitLanguageController(
            language=policy.fallback_language,
            services=services,
            repository=self.repository,
            call_id=call.call_id,
        )
        tools: list[lk_llm.Tool | lk_llm.Toolset] = list(
            shared_livekit_tools(
                agent.tools,
                call_id=call.call_id,
                buffer=lifecycle.buffer,
                sink=RepositoryToolObservationSink(self.repository),
                executor=self.tool_executor,
            )
        )
        if policy.fallback_language is not None:
            tools.append(_language_tool(policy.fallback_language, language.activate))
        if policy.transfer_number is not None:
            if self.call_control is None:
                raise VoiceyError(
                    "VY-RUN-002",
                    detail="behavior.transfer_number requires LiveKit SIP call control.",
                )
            tools.append(
                _cold_transfer_tool(
                    policy.transfer_number,
                    self.call_control,
                    lambda: holder["session"].set_reason("transferred"),
                )
            )
            tools.append(
                _warm_transfer_tool(
                    policy.transfer_number,
                    lambda: holder["session"].set_reason("transferred"),
                )
            )
        if policy.dtmf:
            tools.append(cast(lk_llm.Tool, beta_tools.send_dtmf_events))
        native_agent = await load_native_agent(agent.flow, shared_tools=tools)
        room_options = RoomOptions(
            text_input=True,
            audio_input=True,
            video_input=False,
            audio_output=True,
            text_output=True,
            close_on_disconnect=True,
            delete_room_on_close=False,
        )
        session = LiveKitSession(
            agent=agent,
            call=call,
            lifecycle=lifecycle,
            services=services,
            policy=policy,
            native_agent=native_agent,
            native_session=native_session,
            observations=observations,
            room_options=room_options,
            global_tools=tuple(tools),
            language=language,
            _closed=closed,
        )
        holder["session"] = session
        self._wire_events(session)
        return session

    def _wire_events(self, session: LiveKitSession) -> None:
        def on_close(event: CloseEvent) -> None:
            session.set_reason(_close_reason(event))
            session.mark_closed(event)

        def on_error(event: ErrorEvent) -> None:
            session.set_reason(_error_reason(event.error))
            session.observations.schedule_timeline(
                "runtime.session_error",
                error_type=type(event.error).__name__,
            )

        session.native_session.on("close", on_close)
        session.native_session.on("error", on_error)


def _language_tool(
    language: str,
    activate: Callable[[], Awaitable[None]],
) -> lk_llm.Tool:
    async def switch_to_fallback_language() -> dict[str, object]:
        """Switch recognition and synthesis to the configured fallback language."""
        await activate()
        return {"ok": True, "language": language}

    return function_tool(switch_to_fallback_language)


def _cold_transfer_tool(
    number: str,
    control: LiveKitCallControl,
    transferred: Callable[[], None],
) -> lk_llm.Tool:
    async def transfer_to_human() -> dict[str, object]:
        """Cold-transfer this SIP call to the configured human destination."""
        await control.cold_transfer(number)
        transferred()
        return {"ok": True, "status": "transferred"}

    return function_tool(transfer_to_human)


def _warm_transfer_tool(
    number: str,
    transferred: Callable[[], None],
) -> lk_llm.Tool:
    async def warm_transfer_to_human(ctx: RunContext[Any]) -> dict[str, object]:
        """Warm-transfer the caller after briefing and connecting the human."""
        ctx.disallow_interruptions()
        result = await WarmTransferTask(sip_call_to=number)
        transferred()
        ctx.session.shutdown(drain=True)
        return {
            "ok": True,
            "status": "transferred",
            "human_agent_identity": result.human_agent_identity,
        }

    return function_tool(warm_transfer_to_human)


def _close_reason(event: CloseEvent) -> EndedReason:
    if event.error is not None:
        return _error_reason(event.error)
    reason = cast(
        EndedReason,
        {
            CloseReason.ERROR: "provider_error",
            CloseReason.JOB_SHUTDOWN: "provider_hangup",
            CloseReason.PARTICIPANT_DISCONNECTED: "caller_hangup",
            CloseReason.USER_INITIATED: "agent_hangup",
            CloseReason.TASK_COMPLETED: "agent_hangup",
        }.get(event.reason, "unknown"),
    )
    return reason


def _error_reason(error: object) -> EndedReason:
    normalized = type(error).__name__.casefold()
    if "stt" in normalized:
        return "stt_unavailable"
    if "llm" in normalized:
        return "llm_unavailable"
    if "tts" in normalized:
        return "tts_unavailable"
    return "provider_error"


_NORMAL_REASONS: frozenset[EndedReason] = frozenset(
    {
        "caller_hangup",
        "agent_hangup",
        "duration_limit",
        "silence_timeout",
        "transferred",
        "voicemail",
        "provider_hangup",
    }
)
_FAILURE_REASONS: frozenset[EndedReason] = frozenset(
    {
        "stt_unavailable",
        "llm_unavailable",
        "tts_unavailable",
        "carrier_error",
        "provider_error",
        "worker_crash",
        "setup_error",
        "recovery_unknown",
        "unknown",
    }
)
