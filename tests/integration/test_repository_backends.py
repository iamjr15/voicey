from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import pytest

from voicekit.errors import VoicekitError
from voicekit.obs import (
    LatencySample,
    NewCall,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
)
from voicekit.storage import (
    RecordingReady,
    ResultDeliveryConfig,
    ResultSnapshot,
    SQLiteRepository,
    StorageRepository,
    TerminalRequest,
)

pytestmark = pytest.mark.integration
CONFIG_HASH = f"sha256:{'b' * 64}"


class _ContractRepository(StorageRepository, Protocol):
    async def open(self) -> object: ...

    async def close(self) -> None: ...

    async def ready(self) -> bool: ...

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None: ...

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None: ...

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None: ...

    async def record_latency(self, call_id: str, sample: LatencySample) -> None: ...


def _call(prefix: str, now: datetime) -> NewCall:
    call_id = f"{prefix}_{uuid.uuid4().hex}"
    return NewCall(
        call_id=call_id,
        agent_name="backend-contract",
        runtime="livekit",
        channel="phone",
        direction="outbound",
        provider="twilio",
        provider_call_id=f"CA_{call_id}",
        from_number="+14155550100",
        to_number="+14155550101",
        config_hash=CONFIG_HASH,
        started_at=now,
    )


async def _open_repository(backend: str, tmp_path: Path) -> _ContractRepository:
    if backend == "sqlite":
        repository = cast("_ContractRepository", SQLiteRepository(tmp_path / "contract.sqlite3"))
    else:
        dsn = os.environ.get("VOICEKIT_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("VOICEKIT_TEST_POSTGRES_DSN is not configured")
        from voicekit.storage.postgres import PostgresRepository

        repository = cast("_ContractRepository", PostgresRepository(dsn, max_size=4))
    await repository.open()
    return repository


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.asyncio
async def test_repository_backend_contract(backend: str, tmp_path: Path) -> None:
    repository = await _open_repository(backend, tmp_path)
    now = datetime.now(UTC)
    call = _call("call_backend_contract", now)
    try:
        assert await repository.ready()
        lease = await repository.begin_call(
            call,
            owner_id="worker-a",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
                recording_enabled=True,
            ),
            lease_ttl=timedelta(seconds=30),
            now=now,
        )
        await repository.append_timeline(
            call.call_id,
            TimelineEvent(event_type="runtime.admitted", occurred_at=now),
        )
        await repository.append_transcript(
            call.call_id,
            TranscriptTurn(turn_id="turn-1", role="user", text="hello", t_ms=25),
        )
        await repository.record_tool_call(
            call.call_id,
            ToolCallObservation(
                invocation_id=f"{call.call_id}-tool-1",
                tool_name="lookup",
                arguments={"query": "hello"},
                result={"found": True},
                duration_ms=4.5,
                status="succeeded",
                occurred_at=now,
            ),
        )
        await repository.record_latency(
            call.call_id,
            LatencySample(
                turn_id="turn-1",
                turn_index=1,
                metric="e2e",
                duration_ms=320,
                observed_at=now,
            ),
        )
        await repository.flush_results(
            lease,
            ResultSnapshot(outcome="resolved", data={"ticket": "T-1"}, interruptions=1),
        )
        event = await repository.terminalize(
            lease,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="caller_hangup",
                ended_at=now,
            ),
        )
        claims = await repository.claim_deliveries(
            owner_id="delivery-a",
            limit=100,
            lease_ttl=timedelta(seconds=30),
            now=now,
        )
        own_claims = [claim for claim in claims if claim.call_id == call.call_id]
        assert len(own_claims) == 1
        await repository.acknowledge_delivery(own_claims[0], now=now)
        pending = await repository.get_recording_for_call(call.call_id)
        assert pending is not None
        recording_event = await repository.mark_recording_ready(
            RecordingReady(
                recording_id=pending.recording_id,
                access_url=f"https://relay.example.test/recordings/{pending.recording_id}",
                storage_key=f"recordings/{pending.recording_id}.wav",
                ready_at=now,
            )
        )
        record = await repository.get_call(call.call_id)
        result = await repository.get_result_snapshot(call.call_id)
    finally:
        await repository.close()

    assert event.event_type == "call.completed"
    assert recording_event.event_type == "call.recording.ready"
    assert record.status == "completed"
    assert [item.event_type for item in record.timeline] == ["runtime.admitted"]
    assert [item.text for item in record.transcript] == ["hello"]
    assert [item.tool_name for item in record.tool_calls] == ["lookup"]
    assert [item.duration_ms for item in record.latency] == [320]
    assert result == ResultSnapshot(
        outcome="resolved",
        data={"ticket": "T-1"},
        interruptions=1,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.asyncio
async def test_repository_backend_chaos_invariants(backend: str, tmp_path: Path) -> None:
    repository = await _open_repository(backend, tmp_path)
    started = datetime.now(UTC) - timedelta(seconds=5)
    call = _call("call_backend_chaos", started)
    try:
        stale = await repository.begin_call(
            call,
            owner_id="crashed-worker",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
            ),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        current = await repository.takeover_expired_call(
            call.call_id,
            owner_id="recovery-worker",
            lease_ttl=timedelta(seconds=30),
            now=datetime.now(UTC),
        )
        with pytest.raises(VoicekitError) as fenced:
            await repository.flush_results(stale, ResultSnapshot(outcome="late-write"))
        request = TerminalRequest(
            event_type="call.failed",
            ended_reason="worker_crash",
        )
        first, duplicate = await asyncio.gather(
            repository.terminalize(current, request),
            repository.terminalize(current, request),
        )
        claims_a, claims_b = await asyncio.gather(
            repository.claim_deliveries(
                owner_id="delivery-a",
                limit=100,
                lease_ttl=timedelta(seconds=30),
            ),
            repository.claim_deliveries(
                owner_id="delivery-b",
                limit=100,
                lease_ttl=timedelta(seconds=30),
            ),
        )
        stored = await repository.get_terminal_event_for_call(call.call_id)
    finally:
        await repository.close()

    assert current.generation == 2
    assert fenced.value.code == "VK-RES-006"
    assert first == duplicate == stored
    own_claims = [claim for claim in (*claims_a, *claims_b) if claim.call_id == call.call_id]
    assert len(own_claims) == 1
    assert not ({claim.event_id for claim in claims_a} & {claim.event_id for claim in claims_b})
