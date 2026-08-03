"""Fenced durable lifecycle owned by each Pipecat call."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, cast

from pydantic import JsonValue, TypeAdapter, ValidationError

from voicey import results
from voicey.config.models import Agent, RuntimeName
from voicey.errors import VoiceyError
from voicey.obs.latency import LatencySample
from voicey.obs.records import (
    Channel,
    Direction,
    NewCall,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
)
from voicey.runtimes.pipecat.admission import AdmissionController, AdmissionLease
from voicey.storage.models import (
    CallLease,
    EndedReason,
    PersistedEvent,
    RecordingReady,
    RecordingSnapshot,
    ResultDeliveryConfig,
    ResultSnapshot,
    TerminalEventType,
    TerminalRequest,
)

_COMPLETED_REASONS: frozenset[EndedReason] = frozenset(
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


class PipecatRepository(Protocol):
    """Minimal fenced call-write contract implemented locally or by the relay."""

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease: ...

    async def renew_lease(
        self,
        lease: CallLease,
        *,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease: ...

    async def handoff_call(
        self,
        call_id: str,
        *,
        expected_owner_id: str,
        owner_id: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease: ...

    async def flush_results(self, lease: CallLease, snapshot: ResultSnapshot) -> None: ...

    async def update_provider_state(self, lease: CallLease, state: str) -> None: ...

    async def terminalize(
        self,
        lease: CallLease,
        request: TerminalRequest,
    ) -> PersistedEvent: ...

    async def mark_recording_ready(self, update: RecordingReady) -> PersistedEvent: ...

    async def mark_recording_failed(self, call_id: str) -> None: ...

    async def get_recording_for_call(self, call_id: str) -> RecordingSnapshot | None: ...

    async def get_recording(self, recording_id: str) -> RecordingSnapshot: ...

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None: ...

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None: ...

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None: ...

    async def record_latency(self, call_id: str, sample: LatencySample) -> None: ...


@dataclass(frozen=True, slots=True)
class PipecatCall:
    """Stable metadata known before a call is accepted."""

    call_id: str
    channel: Channel
    direction: Direction
    provider: str | None = None
    provider_call_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None


class PipecatCallLifecycle:
    """Own one lease, result buffer, heartbeat, and terminal transaction."""

    def __init__(
        self,
        *,
        repository: PipecatRepository,
        admission: AdmissionController,
        admission_lease: AdmissionLease,
        lease: CallLease,
        lease_ttl: timedelta,
    ) -> None:
        self.repository = repository
        self.admission = admission
        self.admission_lease = admission_lease
        self.lease = lease
        self.buffer = results.CallResultBuffer(call_id=lease.call_id)
        self._lease_ttl = lease_ttl
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._terminal_event: PersistedEvent | None = None
        self._finish_lock = asyncio.Lock()

    @property
    def call_id(self) -> str:
        return self.lease.call_id

    @property
    def terminal_event(self) -> PersistedEvent | None:
        return self._terminal_event

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat(),
                name=f"voicey-lease-{self.call_id}",
            )

    async def finish(
        self,
        reason: EndedReason,
        *,
        interruptions: int = 0,
        provider_state: str | None = None,
    ) -> PersistedEvent:
        """Flush runtime results and create exactly one immutable terminal event."""
        async with self._finish_lock:
            if self._terminal_event is not None:
                return self._terminal_event
            await self._stop_heartbeat()
            snapshot = _result_snapshot(self.buffer, interruptions)
            try:
                await self.repository.flush_results(self.lease, snapshot)
                event_type: TerminalEventType = (
                    "call.completed" if reason in _COMPLETED_REASONS else "call.failed"
                )
                event = await self.repository.terminalize(
                    self.lease,
                    TerminalRequest(
                        event_type=event_type,
                        ended_reason=reason,
                        provider_state=provider_state,
                    ),
                )
            except VoiceyError:
                raise
            except Exception as exc:
                raise VoiceyError(
                    "VY-RUN-006",
                    detail=f"terminal persistence failed for {self.call_id}.",
                ) from exc
            self._terminal_event = event
            await self.admission.release(self.admission_lease)
            return event

    async def fail_setup(self) -> PersistedEvent:
        await self.repository.append_timeline(
            self.call_id,
            TimelineEvent(event_type="runtime.setup_failed"),
        )
        return await self.finish("setup_error")

    async def _heartbeat(self) -> None:
        interval = max(1.0, self._lease_ttl.total_seconds() / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                self.lease = await self.repository.renew_lease(
                    self.lease,
                    lease_ttl=self._lease_ttl,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise VoiceyError(
                "VY-RUN-006",
                detail=f"lease renewal failed for {self.call_id}.",
            ) from exc

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._heartbeat_task
        self._heartbeat_task = None


class PipecatLifecycleManager:
    """Begin lifecycle storage before transport visibility."""

    def __init__(
        self,
        repository: PipecatRepository,
        admission: AdmissionController,
        *,
        owner_id: str | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
        runtime: RuntimeName = "pipecat",
    ) -> None:
        self.repository = repository
        self.admission = admission
        self.runtime: RuntimeName = runtime
        self.owner_id = owner_id or f"{runtime}_{uuid.uuid4().hex}"
        self.lease_ttl = lease_ttl

    async def begin(
        self,
        agent: Agent,
        call: PipecatCall,
        admission_lease: AdmissionLease,
    ) -> PipecatCallLifecycle:
        """Create the durable active row under the answer-time reservation."""
        if admission_lease.call_id != call.call_id:
            await self.admission.release(admission_lease)
            raise VoiceyError(
                "VY-RUN-005",
                detail="admission lease does not match call metadata.",
            )
        try:
            lease = await self.repository.begin_call(
                NewCall(
                    call_id=call.call_id,
                    agent_name=agent.name,
                    runtime=self.runtime,
                    channel=call.channel,
                    direction=call.direction,
                    provider=call.provider,
                    provider_call_id=call.provider_call_id,
                    from_number=call.from_number,
                    to_number=call.to_number,
                    config_hash=agent.config_hash,
                ),
                owner_id=self.owner_id,
                delivery=ResultDeliveryConfig(
                    endpoint=agent.results.webhook,
                    include=tuple(agent.results.include),
                    redact=tuple(agent.results.redact),
                    purge_after_days=agent.results.purge_after_days,
                    recording_enabled=bool(agent.phone and agent.phone.record),
                ),
                lease_ttl=self.lease_ttl,
            )
        except Exception:
            await self.admission.release(admission_lease)
            raise
        return await self._activate(call, admission_lease, lease)

    async def claim_reserved(
        self,
        call: PipecatCall,
        admission_lease: AdmissionLease,
        *,
        expected_owner_id: str,
    ) -> PipecatCallLifecycle:
        """Fence a durable browser reservation into the dispatched worker."""
        if admission_lease.call_id != call.call_id:
            await self.admission.release(admission_lease)
            raise VoiceyError(
                "VY-RUN-005",
                detail="admission lease does not match reserved call metadata.",
            )
        try:
            lease = await self.repository.handoff_call(
                call.call_id,
                expected_owner_id=expected_owner_id,
                owner_id=self.owner_id,
                lease_ttl=self.lease_ttl,
            )
        except Exception:
            await self.admission.release(admission_lease)
            raise
        return await self._activate(call, admission_lease, lease)

    async def _activate(
        self,
        call: PipecatCall,
        admission_lease: AdmissionLease,
        lease: CallLease,
    ) -> PipecatCallLifecycle:
        lifecycle = PipecatCallLifecycle(
            repository=self.repository,
            admission=self.admission,
            admission_lease=admission_lease,
            lease=lease,
            lease_ttl=self.lease_ttl,
        )
        lifecycle.start_heartbeat()
        await self.repository.append_timeline(
            call.call_id,
            TimelineEvent(event_type="runtime.admitted"),
        )
        return lifecycle


def _result_snapshot(
    buffer: results.CallResultBuffer,
    interruptions: int,
) -> ResultSnapshot:
    try:
        data = {
            key: cast(JsonValue, TypeAdapter(JsonValue).validate_python(value))
            for key, value in buffer.data.items()
        }
        return ResultSnapshot(
            outcome=buffer.outcome,
            data=data,
            interruptions=interruptions,
        )
    except ValidationError as exc:
        raise VoiceyError(
            "VY-RUN-006",
            detail="results buffer contains a non-JSON value.",
        ) from exc
