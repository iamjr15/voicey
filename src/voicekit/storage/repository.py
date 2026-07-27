"""Storage repository Protocol shared by local and managed backends."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from voicekit.obs.records import CallRecord, NewCall
from voicekit.storage.models import (
    CallLease,
    DeliveryClaim,
    DeliveryRecord,
    PersistedEvent,
    PurgeItem,
    RecordingReady,
    RecordingSnapshot,
    ResultDeliveryConfig,
    ResultSnapshot,
    TerminalRequest,
)


class StorageRepository(Protocol):
    """Durable lifecycle/outbox operations required by both runtimes."""

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

    async def takeover_expired_call(
        self,
        call_id: str,
        *,
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

    async def get_event(self, event_id: str) -> PersistedEvent: ...

    async def get_terminal_event_for_call(self, call_id: str) -> PersistedEvent: ...

    async def get_result_snapshot(self, call_id: str) -> ResultSnapshot: ...

    async def get_recording_for_call(self, call_id: str) -> RecordingSnapshot | None: ...

    async def claim_deliveries(
        self,
        *,
        owner_id: str,
        limit: int,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[DeliveryClaim, ...]: ...

    async def acknowledge_delivery(
        self,
        claim: DeliveryClaim,
        *,
        now: datetime | None = None,
    ) -> None: ...

    async def fail_delivery(
        self,
        claim: DeliveryClaim,
        *,
        error: str,
        jitter: Callable[[float], float],
        now: datetime | None = None,
    ) -> DeliveryRecord: ...

    async def redeliver(
        self,
        event_id: str,
        *,
        now: datetime | None = None,
    ) -> DeliveryRecord: ...

    async def list_deliveries(
        self,
        *,
        undelivered_only: bool = False,
    ) -> tuple[DeliveryRecord, ...]: ...

    async def list_stale_calls(self, *, now: datetime | None = None) -> tuple[str, ...]: ...

    async def get_call(self, call_id: str) -> CallRecord: ...

    async def queue_retention(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[PurgeItem, ...]: ...

    async def acknowledge_purge(self, storage_key: str) -> None: ...
