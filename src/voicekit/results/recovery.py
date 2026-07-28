"""Provider-reconciled recovery for stale fenced calls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from voicekit.obs.records import CallRecord
from voicekit.storage.models import EndedReason, ProviderCallState, TerminalRequest
from voicekit.storage.repository import StorageRepository


class ProviderReconciliation(BaseModel):
    """Provider truth observed before any recovery terminalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ProviderCallState
    ended_reason: EndedReason | None = None


class ProviderReconciler(Protocol):
    """Carrier/runtime lookup used by the stale-call sweeper."""

    async def reconcile(self, call: CallRecord) -> ProviderReconciliation: ...


class ProviderObservationRepository(Protocol):
    """Repository read needed to reconcile the last durable provider observation."""

    async def get_provider_state(self, call_id: str) -> str | None: ...


class DurableProviderObservationReconciler:
    """Normalize the latest authenticated provider observation after a crash.

    Active observations are preserved only while their worker lease is valid.
    Once recovery owns an expired generation, a non-terminal or unknown
    observation becomes ``unknown`` so the call cannot remain silently active.
    """

    _ACTIVE = frozenset(
        {
            "active",
            "answered",
            "connected",
            "in-progress",
            "in_progress",
            "initiated",
            "queued",
            "ringing",
        }
    )
    _COMPLETED = frozenset(
        {
            "complete",
            "completed",
            "disconnected",
            "ended",
            "hangup",
        }
    )
    _FAILED = frozenset(
        {
            "busy",
            "canceled",
            "cancelled",
            "error",
            "failed",
            "no-answer",
            "no_answer",
        }
    )

    def __init__(self, repository: ProviderObservationRepository) -> None:
        self._repository = repository

    async def reconcile(self, call: CallRecord) -> ProviderReconciliation:
        """Map one durable observation without inventing provider success."""
        raw = await self._repository.get_provider_state(call.call_id)
        normalized = "" if raw is None else raw.strip().casefold()
        if normalized in self._COMPLETED:
            return ProviderReconciliation(
                state="completed",
                ended_reason="provider_hangup",
            )
        if normalized in self._FAILED:
            return ProviderReconciliation(
                state="failed",
                ended_reason="provider_error",
            )
        if normalized in self._ACTIVE or not normalized:
            return ProviderReconciliation(state="unknown")
        return ProviderReconciliation(state="unknown")


@dataclass(frozen=True, slots=True)
class RecoveryRun:
    """Outcome counts for a bounded stale-call sweep."""

    stale: int
    active: int
    terminalized: int
    deferred: int


class RecoveryCoordinator:
    """Take over expired generations and reconcile provider state before CAS."""

    def __init__(
        self,
        repository: StorageRepository,
        reconciler: ProviderReconciler,
        *,
        owner_id: str,
        lease_ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._reconciler = reconciler
        self._owner_id = owner_id
        self._lease_ttl = lease_ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> RecoveryRun:
        """Sweep calls that were stale at the start of this pass."""
        current = self._clock()
        call_ids = await self._repository.list_stale_calls(now=current)
        active = 0
        terminalized = 0
        deferred = 0
        for call_id in call_ids:
            try:
                lease = await self._repository.takeover_expired_call(
                    call_id,
                    owner_id=self._owner_id,
                    lease_ttl=self._lease_ttl,
                    now=current,
                )
                call = await self._repository.get_call(call_id)
                reconciliation = await self._reconciler.reconcile(call)
                await self._repository.update_provider_state(
                    lease,
                    reconciliation.state,
                )
                if reconciliation.state == "active":
                    active += 1
                    continue
                if reconciliation.state == "completed":
                    event_type = "call.completed"
                    reason: EndedReason = reconciliation.ended_reason or "provider_hangup"
                elif reconciliation.state == "failed":
                    event_type = "call.failed"
                    reason = reconciliation.ended_reason or "provider_error"
                else:
                    event_type = "call.failed"
                    reason = "recovery_unknown"
                await self._repository.terminalize(
                    lease,
                    TerminalRequest(
                        event_type=event_type,
                        ended_reason=reason,
                        ended_at=current,
                        provider_state=reconciliation.state,
                    ),
                )
                terminalized += 1
            except Exception:
                deferred += 1
        return RecoveryRun(
            stale=len(call_ids),
            active=active,
            terminalized=terminalized,
            deferred=deferred,
        )
