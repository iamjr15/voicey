"""Per-call native Pipecat pipeline assembly and lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, cast

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.flows import FlowManager, FlowsFunctionSchema
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputDTMFFrame,
    STTUpdateSettingsFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.service_switcher import ServiceSwitcher
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_mute import AlwaysUserMuteStrategy
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy
from pipecat.workers.runner import WorkerRunner

from voicey import results
from voicey.config.models import Agent
from voicey.errors import VoiceyError
from voicey.obs.records import TimelineEvent
from voicey.runtimes.pipecat.flows import (
    TransferHandler,
    WarmTransferHandler,
    initialize_native_flow,
    language_fallback_flow_tool,
    shared_flow_tools,
    transfer_flow_tool,
    warm_transfer_flow_tool,
)
from voicey.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatCallLifecycle,
    PipecatRepository,
)
from voicey.runtimes.pipecat.mapping import PipecatPolicy
from voicey.runtimes.pipecat.observability import PipecatObservationBridge
from voicey.runtimes.pipecat.providers import (
    DefaultProviderFactory,
    PipecatServices,
    ProviderFactory,
    build_services,
)
from voicey.storage.models import EndedReason, PersistedEvent
from voicey.tools import RepositoryToolObservationSink, ToolExecutor


class DTMFPolicyProcessor(FrameProcessor):
    """Drop native DTMF frames when behavior.dtmf is disabled."""

    def __init__(self, *, enabled: bool) -> None:
        super().__init__()
        self.enabled = enabled

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputDTMFFrame) and not self.enabled:
            return
        await self.push_frame(frame, direction)


class PipecatLanguageController:
    """Update every STT/TTS member using its current typed Settings class."""

    def __init__(
        self,
        *,
        language: str | None,
        services: PipecatServices,
        factory: ProviderFactory,
        repository: PipecatRepository,
        call_id: str,
    ) -> None:
        self.language = language
        self._services = services
        self._factory = factory
        self._repository = repository
        self._call_id = call_id
        self._worker: PipelineWorker | None = None
        self._active = False
        self._lock = asyncio.Lock()

    def bind(self, worker: PipelineWorker) -> None:
        self._worker = worker

    @property
    def active(self) -> bool:
        return self._active

    async def activate(self) -> None:
        if self.language is None:
            return
        async with self._lock:
            if self._active:
                return
            if self._worker is None:
                raise VoiceyError(
                    "VY-RUN-002",
                    detail="language fallback controller is not bound to a worker.",
                )
            for service in self._services.stt_members:
                await self._worker.queue_frame(
                    STTUpdateSettingsFrame(
                        delta=self._factory.language_delta(service, self.language),
                        service=service,
                    )
                )
            for service in self._services.tts_members:
                await self._worker.queue_frame(
                    TTSUpdateSettingsFrame(
                        delta=self._factory.language_delta(service, self.language),
                        service=service,
                    )
                )
            self._active = True
            await self._repository.append_timeline(
                self._call_id,
                TimelineEvent(
                    event_type="runtime.language_fallback",
                    details={"language": self.language},
                ),
            )


@dataclass(slots=True)
class PipecatSession:
    """One PipelineWorker and its durable lifecycle."""

    agent: Agent
    call: PipecatCall
    lifecycle: PipecatCallLifecycle
    transport: BaseTransport
    services: PipecatServices
    aggregators: LLMContextAggregatorPair
    worker: PipelineWorker
    flow_manager: FlowManager
    observations: PipecatObservationBridge
    language: PipecatLanguageController
    dtmf_policy: DTMFPolicyProcessor
    policy: PipecatPolicy
    user_params: LLMUserAggregatorParams
    pipeline_params: PipelineParams
    global_tools: tuple[FlowsFunctionSchema, ...]
    duration_task: asyncio.Task[None] | None = None
    provider_terminal_required: bool = False
    unattributed_cancel_reason: EndedReason = "worker_crash"
    _ended_reason: EndedReason | None = None
    _terminal_signal: asyncio.Event = field(default_factory=asyncio.Event)
    _flow_initialized: bool = False

    @property
    def ended_reason(self) -> EndedReason | None:
        return self._ended_reason

    async def start(self, runner: WorkerRunner) -> None:
        """Add this worker to a long-lived runner with call-local contexts."""
        with results.result_context(self.lifecycle.buffer):
            await runner.add_workers(self.worker)

    async def wait(self) -> PersistedEvent:
        """Wait for pipeline completion and close the fenced lifecycle."""
        try:
            try:
                with results.result_context(self.lifecycle.buffer):
                    await self.worker.wait()
            except Exception:
                self.set_reason("worker_crash")
            if self.provider_terminal_required and self._ended_reason is None:
                try:
                    await asyncio.wait_for(
                        self._terminal_signal.wait(),
                        timeout=self.agent.limits.max_duration_s + 30,
                    )
                except TimeoutError:
                    self.set_reason("duration_limit")
        finally:
            await self._cancel_duration_timer()
        reason = self._ended_reason or (
            "caller_hangup" if self.call.channel == "phone" else "agent_hangup"
        )
        return await self.lifecycle.finish(
            reason,
            interruptions=self.observations.interruptions,
            provider_state="completed" if reason in _NORMAL_REASONS else "failed",
        )

    async def initialize_flow(self) -> None:
        """Initialize the configured native NodeConfig once per connection."""
        if self._flow_initialized:
            return
        try:
            with results.result_context(self.lifecycle.buffer):
                await initialize_native_flow(self.agent.flow, self.flow_manager)
            self._flow_initialized = True
            await self.observations.timeline("runtime.flow_initialized")
        except Exception:
            self.set_reason("setup_error")
            await self.worker.queue_frame(EndFrame(reason="setup_error"))
            raise

    async def end(self, reason: EndedReason) -> None:
        self.set_reason(reason)
        await self.worker.queue_frame(EndFrame(reason=reason))

    def set_reason(self, reason: EndedReason) -> None:
        if self._ended_reason is None or reason in _FAILURE_REASONS:
            self._ended_reason = reason
        self._terminal_signal.set()

    def clear_reason(self, reason: EndedReason) -> None:
        """Undo only an uncommitted handoff marker after a definitive rejection."""
        if self._ended_reason == reason:
            self._ended_reason = None
            self._terminal_signal.clear()

    async def _cancel_duration_timer(self) -> None:
        if self.duration_task is None:
            return
        self.duration_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.duration_task
        self.duration_task = None


class PipecatSessionBuilder:
    """Assemble a native cascaded Pipecat pipeline from canonical Agent config."""

    def __init__(
        self,
        repository: PipecatRepository,
        *,
        provider_factory: ProviderFactory | None = None,
        transfer_handler: TransferHandler | None = None,
        warm_transfer_handler: WarmTransferHandler | None = None,
        warm_transfer_timeout_s: float = 45,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self.repository = repository
        self.provider_factory = provider_factory or DefaultProviderFactory()
        self.transfer_handler = transfer_handler
        self.warm_transfer_handler = warm_transfer_handler
        self.warm_transfer_timeout_s = warm_transfer_timeout_s
        self.tool_executor = tool_executor or ToolExecutor()

    def build(
        self,
        *,
        agent: Agent,
        call: PipecatCall,
        lifecycle: PipecatCallLifecycle,
        transport: BaseTransport,
        sample_rate: int,
    ) -> PipecatSession:
        if agent.runtime != "pipecat":
            raise VoiceyError(
                "VY-RUN-001",
                detail=f"cannot build Pipecat session for runtime {agent.runtime!r}.",
            )
        services = build_services(
            agent,
            sample_rate=sample_rate,
            factory=self.provider_factory,
        )
        user_mute: list[BaseUserMuteStrategy] = (
            [] if agent.behavior.allow_interruptions else [AlwaysUserMuteStrategy()]
        )
        user_params = LLMUserAggregatorParams(
            user_idle_timeout=float(agent.limits.silence_hangup_s),
            vad_analyzer=SileroVADAnalyzer(sample_rate=sample_rate),
            user_mute_strategies=user_mute,
        )
        aggregators = LLMContextAggregatorPair(
            LLMContext(messages=[]),
            user_params=user_params,
        )
        session_holder: dict[str, PipecatSession] = {}

        async def on_user_idle() -> None:
            await session_holder["session"].end("silence_timeout")

        async def on_end_phrase() -> None:
            await session_holder["session"].end("agent_hangup")

        observations = PipecatObservationBridge(
            call_id=call.call_id,
            store=self.repository,
            end_call_phrases=tuple(agent.behavior.end_call_phrases),
            on_user_idle=on_user_idle,
            on_end_phrase=on_end_phrase,
        )
        observations.attach(aggregators)
        dtmf_policy = DTMFPolicyProcessor(enabled=agent.behavior.dtmf)
        processors: list[FrameProcessor] = [
            transport.input(),
            dtmf_policy,
            services.stt,
            aggregators.user(),
            cast(FrameProcessor, services.llm),
            services.tts,
            transport.output(),
            aggregators.assistant(),
        ]
        pipeline = Pipeline(processors)
        pipeline_params = PipelineParams(
            audio_in_sample_rate=sample_rate,
            audio_out_sample_rate=sample_rate,
            enable_metrics=True,
            enable_usage_metrics=True,
        )
        worker = PipelineWorker(
            pipeline,
            params=pipeline_params,
            observers=[observations.latency_observer],
            idle_timeout_secs=float(agent.limits.silence_hangup_s + 15),
            cancel_on_idle_timeout=True,
            cancel_runner_on_idle_timeout=False,
            enable_turn_tracking=True,
            enable_rtvi=True,
        )
        language = PipecatLanguageController(
            language=agent.voice.fallback_language,
            services=services,
            factory=self.provider_factory,
            repository=self.repository,
            call_id=call.call_id,
        )
        language.bind(worker)
        global_tools = shared_flow_tools(
            agent.tools,
            call_id=call.call_id,
            buffer=lifecycle.buffer,
            sink=RepositoryToolObservationSink(self.repository),
            executor=self.tool_executor,
        )
        if agent.voice.fallback_language is not None:
            global_tools.append(
                language_fallback_flow_tool(
                    language=agent.voice.fallback_language,
                    activate=language.activate,
                )
            )
        if agent.behavior.transfer_number is not None:
            if self.warm_transfer_handler is not None:
                global_tools.append(
                    warm_transfer_flow_tool(
                        call_id=call.call_id,
                        number=agent.behavior.transfer_number,
                        transfer=self.warm_transfer_handler,
                        set_reason=lambda reason: (
                            session_holder["session"].clear_reason("transferred")
                            if reason is None
                            else session_holder["session"].set_reason(reason)
                        ),
                        timeout_s=self.warm_transfer_timeout_s,
                    )
                )
            elif self.transfer_handler is not None:
                global_tools.append(
                    transfer_flow_tool(
                        call_id=call.call_id,
                        number=agent.behavior.transfer_number,
                        transfer=self.transfer_handler,
                    )
                )
            else:
                raise VoiceyError(
                    "VY-RUN-002",
                    detail="behavior.transfer_number requires a runtime transfer handler.",
                )
        flow_manager = FlowManager(
            llm=services.llm,
            context_aggregator=aggregators,
            worker=worker,
            transport=transport,
            global_functions=cast(Any, global_tools),
        )
        session = PipecatSession(
            agent=agent,
            call=call,
            lifecycle=lifecycle,
            transport=transport,
            services=services,
            aggregators=aggregators,
            worker=worker,
            flow_manager=flow_manager,
            observations=observations,
            language=language,
            dtmf_policy=dtmf_policy,
            policy=PipecatPolicy.from_agent(agent),
            user_params=user_params,
            pipeline_params=pipeline_params,
            global_tools=tuple(global_tools),
        )
        session_holder["session"] = session
        self._wire_events(session)
        self._wire_failover_events(session)
        return session

    def _wire_events(self, session: PipecatSession) -> None:
        @session.transport.event_handler("on_client_connected")
        async def on_client_connected(  # pyright: ignore[reportUnusedFunction]
            _transport: BaseTransport,
            _client: object,
        ) -> None:
            await session.initialize_flow()

        @session.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(  # pyright: ignore[reportUnusedFunction]
            _transport: BaseTransport,
            _client: object,
        ) -> None:
            if not session.provider_terminal_required:
                await session.end("caller_hangup")

        @session.worker.event_handler("on_pipeline_started")
        async def on_pipeline_started(  # pyright: ignore[reportUnusedFunction]
            _worker: PipelineWorker,
            _frame: Frame,
        ) -> None:
            await session.observations.timeline("runtime.pipeline_started")
            session.duration_task = asyncio.create_task(
                _duration_limit(session),
                name=f"voicey-duration-{session.call.call_id}",
            )

        @session.worker.event_handler("on_pipeline_finished")
        async def on_pipeline_finished(  # pyright: ignore[reportUnusedFunction]
            _worker: PipelineWorker,
            frame: Frame,
        ) -> None:
            if isinstance(frame, CancelFrame) and session.ended_reason is None:
                session.set_reason(session.unattributed_cancel_reason)
            await session.observations.timeline(
                "runtime.pipeline_finished",
                frame=type(frame).__name__,
            )

        @session.worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(  # pyright: ignore[reportUnusedFunction]
            _worker: PipelineWorker,
            frame: ErrorFrame,
        ) -> None:
            processor = frame.processor.name if frame.processor else "unknown"
            await session.observations.timeline(
                "runtime.pipeline_error",
                processor=processor,
                fatal=frame.fatal,
            )
            if frame.fatal:
                session.set_reason(_service_failure_reason(processor))

        @session.worker.event_handler("on_idle_timeout")
        async def on_idle_timeout(  # pyright: ignore[reportUnusedFunction]
            _worker: PipelineWorker,
        ) -> None:
            session.set_reason("silence_timeout")
            await session.observations.timeline("runtime.worker_idle_timeout")

    def _wire_failover_events(self, session: PipecatSession) -> None:
        processors: tuple[tuple[str, FrameProcessor], ...] = (
            ("stt", session.services.stt),
            ("llm", cast(FrameProcessor, session.services.llm)),
            ("tts", session.services.tts),
        )
        for axis, processor in processors:
            if not isinstance(processor, ServiceSwitcher):
                continue

            @processor.strategy.event_handler(  # pyright: ignore[reportUntypedFunctionDecorator]
                "on_service_switched"
            )
            async def on_service_switched(  # pyright: ignore[reportUnusedFunction]
                _strategy: object,
                service: FrameProcessor,
                *,
                _axis: str = axis,
            ) -> None:
                await session.observations.timeline(
                    "runtime.service_failover",
                    axis=_axis,
                    service=service.name,
                )
                await session.language.activate()


async def _duration_limit(session: PipecatSession) -> None:
    await asyncio.sleep(session.agent.limits.max_duration_s)
    await session.end("duration_limit")


def _service_failure_reason(processor: str) -> EndedReason:
    normalized = processor.casefold()
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
