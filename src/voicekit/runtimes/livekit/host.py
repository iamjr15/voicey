"""Production AgentServer host for native LiveKit room and SIP jobs."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, cast

from livekit import rtc
from livekit.agents import AgentServer, JobContext, JobProcess, JobRequest
from livekit.plugins import silero

from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.obs.records import Channel, Direction, NewCall, TimelineEvent
from voicekit.runtimes.livekit.lifecycle import (
    LiveKitCall,
    LiveKitCallLifecycle,
    LiveKitLifecycleManager,
    LiveKitRepository,
)
from voicekit.runtimes.livekit.providers import DefaultLiveKitProviderFactory
from voicekit.runtimes.livekit.session import (
    LiveKitCallControl,
    LiveKitSession,
    LiveKitSessionBuilder,
    SessionReportFactory,
)
from voicekit.runtimes.pipecat.admission import AdmissionController
from voicekit.storage.models import ResultDeliveryConfig, ResultSnapshot, TerminalRequest


class LiveKitRepositoryFactory(Protocol):
    """Open a process-local repository connection for one dispatched job."""

    def __call__(self) -> Awaitable[LiveKitRepository]: ...


class LiveKitRecordingReconciler(Protocol):
    async def wait_until_ready(
        self,
        *,
        call_id: str,
        twilio_call_sid: str,
        timeout_s: float,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LiveKitHostSettings:
    """Non-secret AgentServer process and drain settings."""

    num_idle_processes: int = 2
    drain_timeout_s: int = 3600
    session_end_timeout_s: float = 300.0
    health_port: int = 8081
    browser_reservation_ttl_s: float = 120.0

    def __post_init__(self) -> None:
        if self.num_idle_processes < 0:
            raise VoicekitError("VK-RUN-002", detail="num_idle_processes cannot be negative.")
        if (
            self.drain_timeout_s <= 0
            or self.session_end_timeout_s <= 0
            or self.browser_reservation_ttl_s < 30
        ):
            raise VoicekitError(
                "VK-RUN-002",
                detail=(
                    "LiveKit drain/session timeouts must be positive and the browser "
                    "reservation TTL must be at least 30 seconds."
                ),
            )
        if not 1 <= self.health_port <= 65535:
            raise VoicekitError("VK-RUN-002", detail="LiveKit health port is invalid.")


class LiveKitAdmissionGate:
    """Atomic parent-process dispatch capacity with token reservations."""

    def __init__(self, capacity: int, *, reservation_ttl_s: float = 30.0) -> None:
        if capacity <= 0 or reservation_ttl_s <= 0:
            raise VoicekitError(
                "VK-RUN-004",
                detail="LiveKit admission capacity and reservation TTL must be positive.",
            )
        self.capacity = capacity
        self.reservation_ttl_s = reservation_ttl_s
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def occupied(self) -> int:
        return len(self._pending) + len(self._active)

    async def reserve(self, call_id: str) -> None:
        """Reserve browser capacity before a dispatch token is exposed."""
        async with self._lock:
            if call_id in self._pending or call_id in self._active:
                raise VoicekitError("VK-RUN-005", detail=f"duplicate call id {call_id!r}.")
            if self.occupied >= self.capacity:
                raise VoicekitError("VK-RUN-004", detail="LiveKit dispatch capacity is full.")
            loop = asyncio.get_running_loop()
            self._pending[call_id] = loop.call_later(
                self.reservation_ttl_s,
                lambda: asyncio.create_task(self.release(call_id)),
            )

    async def admit(self, job_id: str, call_id: str) -> bool:
        """Consume a token reservation or directly admit a SIP job."""
        async with self._lock:
            pending = self._pending.pop(call_id, None)
            if pending is not None:
                pending.cancel()
            elif self.occupied >= self.capacity:
                return False
            self._active.add(job_id)
            return True

    async def release(self, identifier: str) -> None:
        async with self._lock:
            pending = self._pending.pop(identifier, None)
            if pending is not None:
                pending.cancel()
            self._active.discard(identifier)


class JobCallControl(LiveKitCallControl):
    """Native cold transfer and DTMF operations for the active room."""

    def __init__(self, context: JobContext, *, participant_identity: str) -> None:
        self._context = context
        self._participant_identity = participant_identity

    async def cold_transfer(self, number: str) -> None:
        try:
            await self._context.transfer_sip_participant(
                self._participant_identity,
                f"tel:{number}",
                play_dialtone=True,
            )
        except Exception as exc:
            raise VoicekitError(
                "VK-TEL-004",
                detail="LiveKit SIP cold transfer was rejected.",
            ) from exc

    async def send_dtmf(self, digits: str) -> None:
        for digit in digits:
            try:
                code = _DTMF_CODES[digit.upper()]
            except KeyError as exc:
                raise VoicekitError(
                    "VK-TEL-002",
                    detail=f"{digit!r} is not a valid DTMF digit.",
                ) from exc
            await self._context.room.local_participant.publish_dtmf(code=code, digit=digit)
            await asyncio.sleep(0.10)


class LiveKitHost:
    """Coordinate AgentServer dispatch, process-local storage, and terminal events."""

    def __init__(
        self,
        *,
        agent: Agent,
        repository_factory: LiveKitRepositoryFactory,
        settings: LiveKitHostSettings | None = None,
        server: AgentServer | None = None,
        session_builder_factory: (
            Callable[
                [LiveKitRepository, LiveKitCallControl, JobContext],
                LiveKitSessionBuilder,
            ]
            | None
        ) = None,
        recording_reconciler_factory: (
            Callable[[LiveKitRepository], LiveKitRecordingReconciler] | None
        ) = None,
    ) -> None:
        if agent.runtime != "livekit":
            raise VoicekitError("VK-RUN-001", detail="LiveKitHost requires runtime='livekit'.")
        self.agent = agent
        self.repository_factory = repository_factory
        self.settings = settings or LiveKitHostSettings()
        self.gate = LiveKitAdmissionGate(
            agent.limits.max_concurrent,
            reservation_ttl_s=self.settings.browser_reservation_ttl_s,
        )
        self._builder_factory = session_builder_factory
        self._recording_reconciler_factory = recording_reconciler_factory
        self.server = server or AgentServer(
            num_idle_processes=self.settings.num_idle_processes,
            drain_timeout=self.settings.drain_timeout_s,
            session_end_timeout=self.settings.session_end_timeout_s,
            port=self.settings.health_port,
            setup_fnc=_prewarm_process,
        )
        self.server.rtc_session(
            self.entrypoint,
            agent_name=agent.name,
            on_request=self.on_request,
            on_session_end=self.on_session_end,
        )

    async def on_request(self, request: JobRequest) -> None:
        """Reject over-capacity jobs before AgentServer forks call work."""
        metadata = _metadata(request.job.metadata)
        call_id = metadata.get("call_id") or f"lk_{request.id}"
        if not await self.gate.admit(request.id, call_id):
            await request.reject(terminate=True)
            return
        await request.accept(
            identity=f"voicekit-{self.agent.name}",
            name=self.agent.name,
            metadata=json.dumps(
                {"call_id": call_id},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    async def on_session_end(self, context: JobContext) -> None:
        await self.gate.release(context.job.id)

    async def entrypoint(self, context: JobContext) -> None:
        """Run one native call and close its fenced lifecycle on every path."""
        repository = await self.repository_factory()
        call = _call_from_context(context)
        admission = AdmissionController(1)
        lifecycle: LiveKitCallLifecycle | None = None
        session: LiveKitSession | None = None
        try:
            lease = await admission.acquire(call.call_id)
            lifecycle_manager = LiveKitLifecycleManager(
                repository,
                admission,
                owner_id=f"livekit_{context.job.id}_{uuid.uuid4().hex}",
            )
            if call.channel == "web":
                lifecycle = await lifecycle_manager.claim_reserved(
                    call,
                    lease,
                    expected_owner_id=_reservation_owner(call.call_id),
                )
            else:
                lifecycle = await lifecycle_manager.begin(self.agent, call, lease)
            control = JobCallControl(
                context,
                participant_identity=_participant_identity(context),
            )
            builder = self._make_builder(repository, control, context)
            session = await builder.build(
                agent=self.agent,
                call=call,
                lifecycle=lifecycle,
            )
            self._attach_dtmf(context, session)
            await session.start(context.room)
            await session.wait(
                report_factory=cast(SessionReportFactory, context.make_session_report)
            )
            await self._reconcile_recording(repository, context, session)
        except asyncio.CancelledError:
            if session is not None:
                await session.end("worker_crash")
                await session.wait(
                    report_factory=cast(SessionReportFactory, context.make_session_report)
                )
            elif lifecycle is not None:
                await lifecycle.finish("worker_crash", provider_state="failed")
            raise
        except Exception:
            if lifecycle is not None and lifecycle.terminal_event is None:
                if session is not None:
                    session.set_reason("setup_error" if not session.started else "provider_error")
                    with suppress(Exception):
                        await session.end(session.ended_reason or "provider_error")
                    with suppress(Exception):
                        await session.wait(
                            report_factory=cast(
                                SessionReportFactory,
                                context.make_session_report,
                            )
                        )
                else:
                    with suppress(Exception):
                        await lifecycle.fail_setup()
            raise
        finally:
            close = getattr(repository, "close", None)
            if close is not None:
                result = close()
                if isinstance(result, Awaitable):
                    await result

    def _make_builder(
        self,
        repository: LiveKitRepository,
        control: LiveKitCallControl,
        context: JobContext,
    ) -> LiveKitSessionBuilder:
        if self._builder_factory is not None:
            return self._builder_factory(repository, control, context)
        prewarmed = context.proc.userdata.get("voicekit_vad")
        vad_model = prewarmed if isinstance(prewarmed, silero.VAD) else None
        return LiveKitSessionBuilder(
            repository,
            provider_factory=DefaultLiveKitProviderFactory(vad_model=vad_model),
            call_control=control,
        )

    def _attach_dtmf(self, context: JobContext, session: LiveKitSession) -> None:
        if not session.policy.dtmf:
            return

        def received(event: rtc.SipDTMF) -> None:
            session.observations.schedule_timeline(
                "runtime.dtmf_received",
                digit=event.digit,
                code=event.code,
            )

        context.room.on("sip_dtmf_received", received)

    async def _reconcile_recording(
        self,
        repository: LiveKitRepository,
        context: JobContext,
        session: LiveKitSession,
    ) -> None:
        if not session.policy.record or self._recording_reconciler_factory is None:
            return
        call_sid = _twilio_call_sid(context)
        if call_sid is None:
            await session.observations.timeline(
                "runtime.recording_pending",
                reason="twilio_call_sid_missing",
            )
            return
        reconciler = self._recording_reconciler_factory(repository)
        try:
            ready = await reconciler.wait_until_ready(
                call_id=session.call.call_id,
                twilio_call_sid=call_sid,
                timeout_s=min(120.0, self.settings.session_end_timeout_s),
            )
        except VoicekitError as exc:
            await session.observations.timeline(
                "runtime.recording_pending",
                reason=exc.code,
            )
            return
        await session.observations.timeline(
            "runtime.recording_ready" if ready else "runtime.recording_pending",
            twilio_call_sid=call_sid,
        )

    async def run(self, *, devmode: bool = False) -> None:
        """Run the installed AgentServer; ``start`` mode owns SIGTERM drain."""
        await self.server.run(devmode=devmode)

    async def reload_agent(self, agent: Agent, *, restart_runner: bool) -> bool:
        """Apply the next-call revision once process-per-call work is idle."""
        del restart_runner
        if self.gate.occupied:
            return False
        if agent.runtime != "livekit" or agent.name != self.agent.name:
            raise VoicekitError(
                "VK-WEB-005",
                detail="LiveKit reload cannot change the runtime or registered agent name.",
            )
        self.agent = agent
        return True

    async def drain(self) -> None:
        """Stop dispatch, wait for active jobs, and close the native worker."""
        await self.server.drain(timeout=self.settings.drain_timeout_s)
        await self.server.aclose()

    async def reserve_web_call(self) -> str:
        """Persist a browser call before any Voicekit or LiveKit token is exposed."""
        call_id = f"call_web_{uuid.uuid4().hex}"
        await self.gate.reserve(call_id)
        repository = await self.repository_factory()
        try:
            await repository.begin_call(
                NewCall(
                    call_id=call_id,
                    agent_name=self.agent.name,
                    runtime="livekit",
                    channel="web",
                    direction="inbound",
                    config_hash=self.agent.config_hash,
                ),
                owner_id=_reservation_owner(call_id),
                delivery=_delivery_config(self.agent),
                lease_ttl=timedelta(seconds=self.settings.browser_reservation_ttl_s),
            )
            await repository.append_timeline(
                call_id,
                TimelineEvent(event_type="runtime.reserved"),
            )
        except Exception:
            await self.gate.release(call_id)
            raise
        finally:
            await _close_repository(repository)
        return call_id

    async def fail_web_reservation(self, call_id: str) -> None:
        """Terminalize a token-exchange failure without waiting for stale recovery."""
        repository = await self.repository_factory()
        try:
            owner_id = f"livekit_reservation_cleanup_{uuid.uuid4().hex}"
            lease = await repository.handoff_call(
                call_id,
                expected_owner_id=_reservation_owner(call_id),
                owner_id=owner_id,
                lease_ttl=timedelta(seconds=30),
            )
            await repository.append_timeline(
                call_id,
                TimelineEvent(event_type="runtime.setup_failed"),
            )
            await repository.flush_results(lease, ResultSnapshot())
            await repository.terminalize(
                lease,
                TerminalRequest(
                    event_type="call.failed",
                    ended_reason="setup_error",
                ),
            )
        finally:
            await self.gate.release(call_id)
            await _close_repository(repository)


def _prewarm_process(process: JobProcess) -> None:
    process.userdata["voicekit_vad"] = silero.VAD.load()


def _reservation_owner(call_id: str) -> str:
    return f"livekit_reservation_{call_id}"


def _delivery_config(agent: Agent) -> ResultDeliveryConfig:
    return ResultDeliveryConfig(
        endpoint=agent.results.webhook,
        include=tuple(agent.results.include),
        redact=tuple(agent.results.redact),
        purge_after_days=agent.results.purge_after_days,
        recording_enabled=False,
    )


async def _close_repository(repository: LiveKitRepository) -> None:
    close = getattr(repository, "close", None)
    if close is None:
        return
    result = close()
    if isinstance(result, Awaitable):
        await result


def _metadata(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VoicekitError("VK-RUN-007", detail="LiveKit job metadata is invalid JSON.") from exc
    if not isinstance(value, Mapping):
        raise VoicekitError(
            "VK-RUN-007",
            detail="LiveKit job metadata must be a string-to-string object.",
        )
    mapping = cast("Mapping[object, object]", value)
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in mapping.items()):
        raise VoicekitError(
            "VK-RUN-007",
            detail="LiveKit job metadata must be a string-to-string object.",
        )
    return dict(cast("Mapping[str, str]", mapping))


def _call_from_context(context: JobContext) -> LiveKitCall:
    metadata = _metadata(context.job.metadata)
    channel = metadata.get("channel", "phone" if _is_sip(context) else "web")
    direction = metadata.get("direction", "inbound")
    if channel not in {"phone", "web"} or direction not in {"inbound", "outbound"}:
        raise VoicekitError(
            "VK-RUN-007",
            detail="LiveKit metadata channel or direction is invalid.",
        )
    return LiveKitCall(
        call_id=metadata.get("call_id") or f"lk_{context.job.id}",
        channel=cast(Channel, channel),
        direction=cast(Direction, direction),
        provider=metadata.get("provider") or ("livekit-sip" if channel == "phone" else "livekit"),
        provider_call_id=(
            metadata.get("provider_call_id")
            or _twilio_call_sid(context)
            or _participant_identity(context)
        ),
        from_number=metadata.get("from_number"),
        to_number=metadata.get("to_number"),
    )


def _participant_identity(context: JobContext) -> str:
    participant = context.job.participant
    return participant.identity or "sip-caller"


def _twilio_call_sid(context: JobContext) -> str | None:
    attributes = getattr(context.job.participant, "attributes", None)
    if not isinstance(attributes, Mapping):
        return None
    value = cast("Mapping[object, object]", attributes).get("sip.twilio.callSid")
    if not isinstance(value, str) or not value.startswith("CA"):
        return None
    return value


def _is_sip(context: JobContext) -> bool:
    participant = context.job.participant
    return participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP


_DTMF_CODES: dict[str, int] = {
    **{str(value): value for value in range(10)},
    "*": 10,
    "#": 11,
    "A": 12,
    "B": 13,
    "C": 14,
    "D": 15,
}
