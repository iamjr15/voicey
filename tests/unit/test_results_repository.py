from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import ValidationError

from voicekit.errors import VoicekitError
from voicekit.obs import (
    CallRecord,
    LatencySample,
    NewCall,
    TimelineEvent,
    TranscriptTurn,
)
from voicekit.results import (
    DeliveryWorker,
    ProviderReconciliation,
    RecoveryCoordinator,
)
from voicekit.results.signing import WebhookSigner, encode_secret
from voicekit.storage import (
    MAX_DELIVERY_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    CallLease,
    LocalArtifactStore,
    PersistedEvent,
    RecordingReady,
    ResultDeliveryConfig,
    ResultSnapshot,
    RetentionWorker,
    SQLiteRepository,
    TerminalRequest,
)

CONFIG_HASH = f"sha256:{'b' * 64}"
WEBHOOK_SECRET = encode_secret(b"repository-webhook-test-key")
LEASE_TTL = timedelta(seconds=30)


def _call(
    call_id: str = "call_01",
    *,
    started_at: datetime,
    recording: bool = False,
) -> NewCall:
    return NewCall(
        call_id=call_id,
        agent_name="clinic-front-desk",
        runtime="pipecat",
        channel="phone",
        direction="inbound",
        provider="twilio",
        provider_call_id=f"CA_{call_id}",
        from_number="+14155550123",
        to_number="+14155550124",
        config_hash=CONFIG_HASH,
        started_at=started_at,
    )


def _delivery(
    *,
    recording: bool = False,
    purge_after_days: int = 30,
) -> ResultDeliveryConfig:
    return ResultDeliveryConfig(
        endpoint="https://receiver.example.test/results",
        redact=("data.email", "call.from"),
        purge_after_days=purge_after_days,
        recording_enabled=recording,
    )


async def _terminal_call(
    repository: SQLiteRepository,
    *,
    call_id: str = "call_01",
    now: datetime,
    recording: bool = False,
    purge_after_days: int = 30,
) -> tuple[CallLease, PersistedEvent]:
    lease = await repository.begin_call(
        _call(call_id, started_at=now - timedelta(seconds=10), recording=recording),
        owner_id="worker_a",
        delivery=_delivery(
            recording=recording,
            purge_after_days=purge_after_days,
        ),
        lease_ttl=LEASE_TTL,
        now=now - timedelta(seconds=10),
    )
    event = await repository.terminalize(
        lease,
        TerminalRequest(
            event_type="call.completed",
            ended_reason="caller_hangup",
            ended_at=now,
        ),
    )
    return lease, event


@pytest.mark.asyncio
async def test_terminal_transaction_is_idempotent_immutable_and_pull_equal(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        lease = await repository.begin_call(
            _call(started_at=now - timedelta(seconds=143), recording=True),
            owner_id="worker_a",
            delivery=_delivery(recording=True),
            lease_ttl=LEASE_TTL,
            now=now - timedelta(seconds=10),
        )
        await repository.append_timeline(
            "call_01",
            TimelineEvent(event_type="call.connected", occurred_at=now),
        )
        await repository.append_transcript(
            "call_01",
            TranscriptTurn(
                turn_id="turn_1",
                role="user",
                text="My email is patient@example.test",
                t_ms=4210,
            ),
        )
        await repository.record_latency(
            "call_01",
            LatencySample(
                turn_id="turn_1",
                turn_index=1,
                metric="e2e",
                duration_ms=780,
                observed_at=now,
            ),
        )
        await repository.flush_results(
            lease,
            ResultSnapshot(
                outcome="booked",
                data={
                    "email": "patient@example.test",
                    "slot": "2026-07-30T14:00",
                },
                interruptions=2,
            ),
        )
        request = TerminalRequest(
            event_type="call.completed",
            ended_reason="caller_hangup",
            ended_at=now,
        )

        first, second = await asyncio.gather(
            repository.terminalize(lease, request),
            repository.terminalize(lease, request),
        )
        pulled = await repository.get_terminal_event_for_call("call_01")
        deliveries = await repository.list_deliveries()

    assert first == second == pulled
    assert len(deliveries) == 1
    payload = cast("dict[str, object]", json.loads(first.body))
    call_payload = cast("dict[str, object]", payload["call"])
    data = cast("dict[str, object]", payload["data"])
    recording = cast("dict[str, object]", payload["recording"])
    assert payload["event"] == "call.completed"
    assert payload["outcome"] == "booked"
    assert call_payload["from"] == "[REDACTED]"
    assert call_payload["to"] == "+14155550124"
    assert call_payload["duration_s"] == 143
    assert data == {
        "email": "[REDACTED]",
        "slot": "2026-07-30T14:00",
    }
    assert cast("str", recording["id"]).startswith("rec_")
    assert recording["status"] == "pending"
    assert recording["url"] is None
    metrics = cast("dict[str, object]", payload["metrics"])
    assert metrics["latency_ms"] == {"p50": 780.0, "p95": 780.0}


@pytest.mark.asyncio
async def test_generation_fences_delayed_heartbeat_and_late_completion(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        stale = await repository.begin_call(
            _call(started_at=started),
            owner_id="old_worker",
            delivery=_delivery(),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        current = await repository.takeover_expired_call(
            "call_01",
            owner_id="new_worker",
            lease_ttl=LEASE_TTL,
            now=started + timedelta(seconds=2),
        )

        with pytest.raises(VoicekitError) as heartbeat_error:
            await repository.renew_lease(
                stale,
                lease_ttl=LEASE_TTL,
                now=started + timedelta(seconds=2),
            )
        with pytest.raises(VoicekitError) as terminal_error:
            await repository.terminalize(
                stale,
                TerminalRequest(
                    event_type="call.failed",
                    ended_reason="worker_crash",
                    ended_at=started + timedelta(seconds=2),
                ),
            )
        event = await repository.terminalize(
            current,
            TerminalRequest(
                event_type="call.failed",
                ended_reason="worker_crash",
                ended_at=started + timedelta(seconds=2),
            ),
        )

    assert current.generation == stale.generation + 1
    assert heartbeat_error.value.code == "VK-RES-006"
    assert terminal_error.value.code == "VK-RES-006"
    assert event.event_type == "call.failed"


@pytest.mark.asyncio
async def test_lease_renewal_succeeds_for_current_generation(tmp_path: Path) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        lease = await repository.begin_call(
            _call(started_at=now),
            owner_id="worker",
            delivery=_delivery(),
            lease_ttl=LEASE_TTL,
            now=now,
        )
        renewed = await repository.renew_lease(
            lease,
            lease_ttl=timedelta(minutes=1),
            now=now + timedelta(seconds=1),
        )

    assert renewed.generation == lease.generation
    assert renewed.expires_at == now + timedelta(seconds=61)


@pytest.mark.asyncio
async def test_terminal_event_and_outbox_roll_back_together_on_insert_failure(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    path = tmp_path / "calls.sqlite3"
    async with SQLiteRepository(path) as repository:
        lease = await repository.begin_call(
            _call(started_at=now),
            owner_id="worker",
            delivery=_delivery(),
            lease_ttl=LEASE_TTL,
            now=now,
        )
        injector = sqlite3.connect(path)
        injector.execute(
            """
            CREATE TRIGGER force_delivery_failure
            BEFORE INSERT ON deliveries
            BEGIN
                SELECT RAISE(ABORT, 'forced outbox failure');
            END
            """
        )
        injector.commit()
        injector.close()

        with pytest.raises(VoicekitError) as failed:
            await repository.terminalize(
                lease,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="caller_hangup",
                    ended_at=now + timedelta(seconds=1),
                ),
            )
        call = await repository.get_call("call_01")
        with pytest.raises(VoicekitError):
            await repository.get_terminal_event_for_call("call_01")

        injector = sqlite3.connect(path)
        injector.execute("DROP TRIGGER force_delivery_failure")
        injector.commit()
        injector.close()
        recovered = await repository.terminalize(
            lease,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="caller_hangup",
                ended_at=now + timedelta(seconds=1),
            ),
        )

    assert failed.value.code == "VK-RES-008"
    assert call.status == "active"
    assert recovered.event_type == "call.completed"


class _Reconciler:
    def __init__(
        self,
        reconciliation: ProviderReconciliation,
        *,
        barrier: asyncio.Event | None = None,
    ) -> None:
        self.reconciliation = reconciliation
        self.calls: list[str] = []
        self.barrier = barrier

    async def reconcile(self, call: CallRecord) -> ProviderReconciliation:
        call_id = call.call_id
        self.calls.append(call_id)
        if self.barrier is not None:
            self.barrier.set()
            await asyncio.sleep(0)
        return self.reconciliation


@pytest.mark.asyncio
async def test_two_simultaneous_sweepers_emit_one_terminal_with_partial_transcript(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    sweep_time = started + timedelta(seconds=2)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await repository.begin_call(
            _call(started_at=started),
            owner_id="crashed_worker",
            delivery=_delivery(),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        await repository.append_transcript(
            "call_01",
            TranscriptTurn(
                turn_id="turn_partial",
                role="user",
                text="This survives the crash.",
                t_ms=500,
            ),
        )
        reconciler = _Reconciler(
            ProviderReconciliation(
                state="failed",
                ended_reason="provider_error",
            )
        )
        sweepers = [
            RecoveryCoordinator(
                repository,
                reconciler,
                owner_id=f"sweeper_{index}",
                clock=lambda: sweep_time,
            )
            for index in range(2)
        ]

        runs = await asyncio.gather(*(sweeper.run_once() for sweeper in sweepers))
        event = await repository.get_terminal_event_for_call("call_01")
        deliveries = await repository.list_deliveries()

    assert sum(run.terminalized for run in runs) == 1
    assert sum(run.deferred for run in runs) == 1
    assert reconciler.calls == ["call_01"]
    assert len(deliveries) == 1
    assert b"This survives the crash." in event.body


@pytest.mark.asyncio
async def test_recovery_reconciles_active_provider_without_terminalizing(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    sweep_time = started + timedelta(seconds=2)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await repository.begin_call(
            _call(started_at=started),
            owner_id="worker",
            delivery=_delivery(),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        coordinator = RecoveryCoordinator(
            repository,
            _Reconciler(ProviderReconciliation(state="active")),
            owner_id="sweeper",
            clock=lambda: sweep_time,
        )

        run = await coordinator.run_once()

        with pytest.raises(VoicekitError) as no_event:
            await repository.get_terminal_event_for_call("call_01")

    assert run.active == 1
    assert run.terminalized == 0
    assert no_event.value.code == "VK-RES-009"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "event_type", "reason"),
    [
        ("completed", "call.completed", "provider_hangup"),
        ("unknown", "call.failed", "recovery_unknown"),
    ],
)
async def test_recovery_maps_reconciled_terminal_states(
    tmp_path: Path,
    state: str,
    event_type: str,
    reason: str,
) -> None:
    started = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    sweep_time = started + timedelta(seconds=2)
    async with SQLiteRepository(tmp_path / f"{state}.sqlite3") as repository:
        await repository.begin_call(
            _call(call_id=f"call_{state}", started_at=started),
            owner_id="worker",
            delivery=_delivery(),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        coordinator = RecoveryCoordinator(
            repository,
            _Reconciler(ProviderReconciliation.model_validate({"state": state})),
            owner_id="sweeper",
            clock=lambda: sweep_time,
        )

        run = await coordinator.run_once()
        event = await repository.get_terminal_event_for_call(f"call_{state}")
        payload = cast("dict[str, object]", json.loads(event.body))

    assert run.terminalized == 1
    assert payload["event"] == event_type
    assert cast("dict[str, object]", payload["call"])["ended_reason"] == reason


@pytest.mark.asyncio
async def test_outbox_claim_is_exclusive_and_retry_curve_dead_letters(
    tmp_path: Path,
) -> None:
    current = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        _, event = await _terminal_call(repository, now=current)
        original_body = event.body

        first_claims, second_claims = await asyncio.gather(
            repository.claim_deliveries(
                owner_id="delivery_a",
                limit=10,
                lease_ttl=LEASE_TTL,
                now=current,
            ),
            repository.claim_deliveries(
                owner_id="delivery_b",
                limit=10,
                lease_ttl=LEASE_TTL,
                now=current,
            ),
        )
        claims = first_claims or second_claims
        assert len(claims) == 1
        assert not (first_claims and second_claims)

        claim = claims[0]
        for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            assert claim.attempt_count == attempt
            assert claim.event_id == event.event_id
            assert claim.body == original_body
            record = await repository.fail_delivery(
                claim,
                error="receiver unavailable",
                jitter=lambda delay: delay,
                now=current,
            )
            if attempt == MAX_DELIVERY_ATTEMPTS:
                assert record.status == "dead_lettered"
                break
            current += timedelta(seconds=RETRY_DELAYS_SECONDS[attempt])
            assert record.next_attempt_at == current
            claim = (
                await repository.claim_deliveries(
                    owner_id="delivery_a",
                    limit=1,
                    lease_ttl=LEASE_TTL,
                    now=current,
                )
            )[0]

        assert await repository.dlq_depth() == 1
        assert (await repository.list_deliveries(undelivered_only=True))[0].status == (
            "dead_lettered"
        )
        reset = await repository.redeliver(event.event_id, now=current)
        redelivery_claim = (
            await repository.claim_deliveries(
                owner_id="delivery_c",
                limit=1,
                lease_ttl=LEASE_TTL,
                now=current,
            )
        )[0]

    assert reset.attempt_count == 0
    assert redelivery_claim.event_id == event.event_id
    assert redelivery_claim.body == original_body


@pytest.mark.asyncio
async def test_outbox_rejects_invalid_claim_and_jitter_operations(tmp_path: Path) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        _, event = await _terminal_call(repository, now=now)
        with pytest.raises(VoicekitError) as limit:
            await repository.claim_deliveries(
                owner_id="worker",
                limit=0,
                lease_ttl=LEASE_TTL,
                now=now,
            )
        claim = (
            await repository.claim_deliveries(
                owner_id="worker",
                limit=1,
                lease_ttl=LEASE_TTL,
                now=now,
            )
        )[0]
        with pytest.raises(VoicekitError) as jitter:
            await repository.fail_delivery(
                claim,
                error="failed",
                jitter=lambda delay: delay * 2,
                now=now,
            )
        with pytest.raises(VoicekitError) as missing:
            await repository.get_event("evt_missing")
        with pytest.raises(VoicekitError) as redeliver:
            await repository.redeliver("evt_missing", now=now)

    assert event.event_id == claim.event_id
    assert limit.value.code == "VK-OBS-005"
    assert jitter.value.code == "VK-RES-008"
    assert missing.value.code == "VK-RES-009"
    assert redeliver.value.code == "VK-RES-009"


@pytest.mark.asyncio
async def test_delivery_worker_reuses_body_and_refreshes_standard_signature(
    tmp_path: Path,
) -> None:
    current = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []
    statuses = iter([503, 204])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(next(statuses), request=request)

    transport = httpx.MockTransport(handler)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        httpx.AsyncClient(transport=transport) as client,
    ):
        _, event = await _terminal_call(repository, now=current)
        clock_value = [current]
        worker = DeliveryWorker(
            repository,
            owner_id="delivery",
            current_secret=WEBHOOK_SECRET,
            client=client,
            clock=lambda: clock_value[0],
            jitter=lambda delay: delay,
        )

        failed = await worker.run_once()
        clock_value[0] += timedelta(seconds=5)
        delivered = await worker.run_once()

        assert (await repository.list_deliveries())[0].status == "delivered"

    assert failed.failed == 1
    assert delivered.delivered == 1
    assert len(requests) == 2
    assert requests[0].content == requests[1].content == event.body
    assert requests[0].headers["webhook-id"] == event.event_id
    assert requests[0].headers["webhook-signature"] != requests[1].headers["webhook-signature"]
    WebhookSigner(WEBHOOK_SECRET).verify(
        requests[1].headers,
        requests[1].content,
        now=int(clock_value[0].timestamp()),
    )


@pytest.mark.asyncio
async def test_delivery_worker_handles_network_error_and_closes_owned_client(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await _terminal_call(repository, now=now)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            worker = DeliveryWorker(
                repository,
                owner_id="delivery",
                current_secret=WEBHOOK_SECRET,
                client=client,
                clock=lambda: now,
                jitter=lambda delay: delay,
            )
            run = await worker.run_once()
            await worker.close()

        owned = DeliveryWorker(
            repository,
            owner_id="unused",
            current_secret=WEBHOOK_SECRET,
        )
        await owned.close()

    assert run.failed == 1


@pytest.mark.asyncio
async def test_recording_ready_is_separate_and_terminal_body_stays_immutable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        _, terminal = await _terminal_call(repository, now=now, recording=True)
        terminal_payload = cast("dict[str, object]", json.loads(terminal.body))
        recording = cast("dict[str, object]", terminal_payload["recording"])
        update = RecordingReady(
            recording_id=cast("str", recording["id"]),
            access_url="https://engine.example.test/recordings/rec",
            storage_key="recordings/rec.wav",
            ready_at=now + timedelta(seconds=5),
        )

        ready = await repository.mark_recording_ready(update)
        duplicate = await repository.mark_recording_ready(update)
        terminal_after = await repository.get_terminal_event_for_call("call_01")

    assert ready == duplicate
    assert terminal_after.body == terminal.body
    assert b'"status":"pending"' in terminal.body
    assert b'"status":"ready"' in ready.body
    assert ready.event_type == "call.recording.ready"
    assert ready.event_id != terminal.event_id


@pytest.mark.asyncio
async def test_recording_ready_before_terminal_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await repository.begin_call(
            _call(started_at=now, recording=True),
            owner_id="worker",
            delivery=_delivery(recording=True),
            lease_ttl=LEASE_TTL,
            now=now,
        )
        call = await repository.get_call("call_01")
        raw = sqlite3.connect(tmp_path / "calls.sqlite3")
        recording_row = raw.execute(
            "SELECT recording_id FROM recordings WHERE call_id = 'call_01'"
        ).fetchone()
        raw.close()
        assert call.status == "active"
        assert recording_row is not None

        with pytest.raises(VoicekitError) as caught:
            await repository.mark_recording_ready(
                RecordingReady(
                    recording_id=str(recording_row[0]),
                    access_url="https://engine.example.test/recordings/rec",
                    storage_key="recordings/rec.wav",
                    ready_at=now,
                )
            )

    assert caught.value.code == "VK-RES-010"


@pytest.mark.asyncio
async def test_retention_purges_database_wal_recordings_and_backups(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    database_path = tmp_path / "data" / "calls.sqlite3"
    artifacts = LocalArtifactStore(tmp_path / "data" / "artifacts")
    async with SQLiteRepository(database_path) as repository:
        _, terminal = await _terminal_call(
            repository,
            now=now - timedelta(days=2),
            recording=True,
            purge_after_days=1,
        )
        terminal_payload = cast("dict[str, object]", json.loads(terminal.body))
        recording = cast("dict[str, object]", terminal_payload["recording"])
        recording_key = "recordings/rec.wav"
        backup_key = "backups/calls.sqlite3.bak"
        await artifacts.put(recording_key, b"recording")
        await artifacts.put(backup_key, b"backup")
        await repository.mark_recording_ready(
            RecordingReady(
                recording_id=cast("str", recording["id"]),
                access_url="https://engine.example.test/recordings/rec",
                storage_key=recording_key,
                ready_at=now - timedelta(days=2),
            )
        )
        await repository.register_backup(
            backup_id="backup_01",
            storage_key=backup_key,
            expires_at=now - timedelta(seconds=1),
            now=now - timedelta(days=2),
        )
        worker = RetentionWorker(repository, artifacts)

        assert await worker.run_once() == 2
        with pytest.raises(VoicekitError) as missing_call:
            await repository.get_call("call_01")
        assert await repository.queue_retention(now=now) == ()

    assert missing_call.value.code == "VK-OBS-003"
    with pytest.raises(VoicekitError):
        await artifacts.read(recording_key)
    with pytest.raises(VoicekitError):
        await artifacts.read(backup_key)
    wal_path = database_path.with_name(f"{database_path.name}-wal")
    assert not wal_path.exists() or wal_path.stat().st_size == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "storage_key",
    ["", "../outside", "/absolute", r"recordings\\outside", "a/../outside"],
)
async def test_local_artifact_store_rejects_path_escape(
    tmp_path: Path,
    storage_key: str,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(VoicekitError) as caught:
        await store.put(storage_key, b"no")

    assert caught.value.code == "VK-ART-001"


@pytest.mark.asyncio
async def test_local_artifacts_are_private_and_symlink_safe(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    await store.put("recordings/call.wav", b"audio")
    path = tmp_path / "artifacts" / "recordings" / "call.wav"

    assert await store.read("recordings/call.wav") == b"audio"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "artifacts" / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(VoicekitError) as caught:
        await store.put("linked/escape", b"no")
    assert caught.value.code == "VK-ART-001"
    await store.delete("recordings/call.wav")
    await store.delete("recordings/call.wav")
    assert not path.exists()


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: ResultDeliveryConfig(endpoint="http://receiver.test"),
            "Fix: configure an https://",
        ),
        (
            lambda: ResultDeliveryConfig(
                endpoint="https://receiver.test",
                purge_after_days=0,
            ),
            "Fix: choose a supported retention",
        ),
        (
            lambda: TerminalRequest(
                event_type="call.failed",
                ended_reason="unknown",
                ended_at=datetime.now(),
            ),
            "Fix: use a UTC-aware",
        ),
        (
            lambda: RecordingReady(
                recording_id="rec",
                access_url="http://engine.test/rec",
                storage_key="recordings/rec",
            ),
            "Fix: expose the engine-owned",
        ),
        (
            lambda: ResultSnapshot(interruptions=-1),
            "Fix: record a non-negative",
        ),
    ],
)
def test_storage_contract_models_carry_fixes(
    factory: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        factory()


@pytest.mark.asyncio
async def test_schema_one_database_migrates_to_current(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE calls (
            call_id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            runtime TEXT NOT NULL,
            channel TEXT NOT NULL,
            direction TEXT NOT NULL,
            provider TEXT,
            provider_call_id TEXT,
            config_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            webhook_status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT,
            terminal_reason TEXT
        )
        """
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    async with SQLiteRepository(path):
        pass

    migrated = sqlite3.connect(path)
    row = migrated.execute("PRAGMA user_version").fetchone()
    migrated.close()
    assert row is not None
    assert row[0] == 2
