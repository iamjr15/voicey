from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from voicey.errors import VoiceyError
from voicey.obs import (
    LatencySample,
    NewCall,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
)
from voicey.relay import (
    RelayClient,
    RelayCredential,
    RelayKeyring,
    RepositoryRelayBackend,
    create_relay_app,
)
from voicey.relay.auth import FenceSigner
from voicey.storage import (
    RecordingReady,
    ResultDeliveryConfig,
    ResultSnapshot,
    TerminalRequest,
)

pytestmark = pytest.mark.integration
CONFIG_HASH = f"sha256:{'f' * 64}"


def _dsn() -> str:
    value = os.environ.get("VOICEY_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("VOICEY_TEST_POSTGRES_DSN is not configured")
    return value


def _call(prefix: str, *, started_at: datetime) -> NewCall:
    call_id = f"{prefix}_{uuid.uuid4().hex}"
    return NewCall(
        call_id=call_id,
        agent_name="postgres-contract",
        runtime="pipecat",
        channel="phone",
        direction="inbound",
        provider="twilio",
        provider_call_id=f"CA_{call_id}",
        config_hash=CONFIG_HASH,
        started_at=started_at,
    )


@asynccontextmanager
async def _isolated_postgres_dsn() -> AsyncGenerator[str]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    schema = f"voicey_test_{uuid.uuid4().hex}"
    settings = conninfo_to_dict(_dsn())
    existing_options = settings.get("options", "")
    settings["options"] = f"{existing_options} -c search_path={schema}".strip()
    isolated = make_conninfo("", **settings)
    async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield isolated
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_postgres_terminal_outbox_recording_and_retention_contract() -> None:
    from voicey.storage.postgres import PostgresRepository

    ended_at = datetime.now(UTC) - timedelta(days=2)
    call = _call("call_pg_terminal", started_at=ended_at - timedelta(seconds=90))
    async with PostgresRepository(_dsn(), max_size=4) as repository:
        assert await repository.ready()
        lease = await repository.begin_call(
            call,
            owner_id="worker-a",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
                recording_enabled=True,
                purge_after_days=1,
            ),
            lease_ttl=timedelta(seconds=30),
            now=ended_at - timedelta(seconds=30),
        )
        await repository.append_timeline(
            call.call_id,
            TimelineEvent(event_type="runtime.admitted", occurred_at=ended_at),
        )
        await repository.append_transcript(
            call.call_id,
            TranscriptTurn(
                turn_id="turn-1",
                role="user",
                text="Book Tuesday",
                t_ms=100,
            ),
        )
        await repository.flush_results(
            lease,
            ResultSnapshot(outcome="booked", data={"day": "Tuesday"}),
        )
        request = TerminalRequest(
            event_type="call.completed",
            ended_reason="caller_hangup",
            ended_at=ended_at,
        )
        first, duplicate = await asyncio.gather(
            repository.terminalize(lease, request),
            repository.terminalize(lease, request),
        )
        pulled = await repository.get_terminal_event_for_call(call.call_id)
        claims_a, claims_b = await asyncio.gather(
            repository.claim_deliveries(
                owner_id="delivery-a",
                limit=10,
                lease_ttl=timedelta(seconds=30),
                now=ended_at,
            ),
            repository.claim_deliveries(
                owner_id="delivery-b",
                limit=10,
                lease_ttl=timedelta(seconds=30),
                now=ended_at,
            ),
        )
        claims = (*claims_a, *claims_b)
        assert len(claims) == 1
        await repository.acknowledge_delivery(claims[0], now=ended_at)
        pending = await repository.get_recording_for_call(call.call_id)
        assert pending is not None
        recording_event = await repository.mark_recording_ready(
            RecordingReady(
                recording_id=pending.recording_id,
                access_url=f"https://relay.example.test/recordings/{pending.recording_id}",
                storage_key=f"recordings/{pending.recording_id}.mp3",
                ready_at=ended_at,
            )
        )
        purge = await repository.queue_retention(now=datetime.now(UTC))

    assert first == duplicate == pulled
    assert json.loads(first.body)["data"] == {"day": "Tuesday"}
    assert recording_event.event_type == "call.recording.ready"
    assert any(item.storage_key.endswith(".mp3") for item in purge)


@pytest.mark.asyncio
async def test_postgres_generation_fencing_and_skip_locked_claims() -> None:
    from voicey.storage.postgres import PostgresRepository

    started = datetime.now(UTC) - timedelta(seconds=5)
    call = _call("call_pg_fence", started_at=started)
    async with PostgresRepository(_dsn(), max_size=4) as repository:
        stale = await repository.begin_call(
            call,
            owner_id="old-worker",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
            ),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        current = await repository.takeover_expired_call(
            call.call_id,
            owner_id="new-worker",
            lease_ttl=timedelta(seconds=30),
            now=datetime.now(UTC),
        )
        with pytest.raises(VoiceyError) as caught:
            await repository.flush_results(stale, ResultSnapshot(outcome="stale"))
        event = await repository.terminalize(
            current,
            TerminalRequest(
                event_type="call.failed",
                ended_reason="worker_crash",
            ),
        )

    assert current.generation == 2
    assert caught.value.code == "VY-RES-006"
    assert event.event_type == "call.failed"


@pytest.mark.asyncio
async def test_postgres_relay_backend_matches_signed_protocol() -> None:
    from voicey.relay.postgres import PostgresRelayJournal
    from voicey.storage.postgres import PostgresRepository

    repository = PostgresRepository(_dsn(), max_size=4)
    journal = PostgresRelayJournal(_dsn(), max_size=4)
    await asyncio.gather(repository.open(), journal.open())
    credential = RelayCredential.issue("postgres-key")
    keyring = RelayKeyring(current=credential)
    try:
        app = create_relay_app(
            RepositoryRelayBackend(
                repository,
                journal,
                fences=FenceSigner(keyring),
            ),
            keyring=keyring,
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient("https://relay.test", credential, client=http) as client:
            call = _call("call_pg_relay", started_at=datetime.now(UTC))
            lease = await client.begin_call(
                call,
                owner_id="cloud-worker",
                delivery=ResultDeliveryConfig(
                    endpoint="https://receiver.example.test/results",
                ),
                lease_ttl=timedelta(seconds=30),
            )
            await client.append_timeline(
                call.call_id,
                TimelineEvent(event_type="runtime.admitted"),
            )
            await client.append_transcript(
                call.call_id,
                TranscriptTurn(
                    turn_id="turn-pg",
                    role="user",
                    text="hello Postgres",
                    t_ms=20,
                ),
            )
            await client.record_tool_call(
                call.call_id,
                ToolCallObservation(
                    invocation_id=f"{call.call_id}-tool",
                    tool_name="lookup",
                    arguments={"query": "hello"},
                    result={"found": True},
                    duration_ms=5,
                    status="succeeded",
                ),
            )
            await client.record_latency(
                call.call_id,
                LatencySample(
                    turn_id="turn-pg",
                    turn_index=1,
                    metric="e2e",
                    duration_ms=250,
                    observed_at=datetime.now(UTC),
                ),
            )
            await client.flush_results(
                lease,
                ResultSnapshot(outcome="resolved", data={"source": "postgres"}),
            )
            await client.update_provider_state(lease, "answered")
            renewed = await client.renew_lease(
                lease,
                lease_ttl=timedelta(seconds=45),
            )
            event = await client.terminalize(
                renewed,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="caller_hangup",
                ),
            )
            record = await client.get_call(call.call_id)
        await http.aclose()
    finally:
        await asyncio.gather(repository.close(), journal.close())

    assert event.event_type == "call.completed"
    assert record.status == "completed"
    assert [item.event_type for item in record.timeline] == ["runtime.admitted"]
    assert [item.text for item in record.transcript] == ["hello Postgres"]
    assert [item.tool_name for item in record.tool_calls] == ["lookup"]
    assert [item.duration_ms for item in record.latency] == [250]


@pytest.mark.asyncio
async def test_postgres_schema_checksum_is_validated() -> None:
    import psycopg

    from voicey.storage.postgres import PostgresRepository

    repository = PostgresRepository(_dsn())
    await repository.open()
    try:
        async with await psycopg.AsyncConnection.connect(_dsn()) as connection:
            cursor = await connection.execute(
                """
                SELECT checksum FROM voicey_schema_migrations WHERE version = 1
                """
            )
            row = await cursor.fetchone()
            assert row is not None
            checksum = str(row[0])
            await connection.execute(
                """
                UPDATE voicey_schema_migrations
                SET checksum = 'tampered' WHERE version = 1
                """
            )
        incompatible = PostgresRepository(_dsn())
        with pytest.raises(VoiceyError) as caught:
            await incompatible.open()
        async with await psycopg.AsyncConnection.connect(_dsn()) as connection:
            await connection.execute(
                """
                UPDATE voicey_schema_migrations SET checksum = %s WHERE version = 1
                """,
                (checksum,),
            )
    finally:
        await repository.close()

    assert caught.value.code == "VY-OBS-004"


@pytest.mark.asyncio
async def test_postgres_concurrent_migration_and_rolling_generation() -> None:
    from voicey.storage.postgres import PostgresRepository

    async with _isolated_postgres_dsn() as dsn:
        old_generation = PostgresRepository(dsn, max_size=3)
        new_generation = PostgresRepository(dsn, max_size=3)
        await asyncio.gather(old_generation.open(), new_generation.open())
        now = datetime.now(UTC)
        call = _call("call_pg_rolling", started_at=now)
        try:
            old_lease = await old_generation.begin_call(
                call,
                owner_id="release-old",
                delivery=ResultDeliveryConfig(
                    endpoint="https://receiver.example.test/results",
                ),
                lease_ttl=timedelta(seconds=30),
                now=now,
            )
            new_lease = await new_generation.handoff_call(
                call.call_id,
                expected_owner_id="release-old",
                owner_id="release-new",
                lease_ttl=timedelta(seconds=30),
                now=now,
            )
            with pytest.raises(VoiceyError) as fenced:
                await old_generation.flush_results(
                    old_lease,
                    ResultSnapshot(outcome="late-old-release"),
                )
            event = await new_generation.terminalize(
                new_lease,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="caller_hangup",
                ),
            )
        finally:
            await asyncio.gather(old_generation.close(), new_generation.close())

    assert new_lease.generation == 2
    assert fenced.value.code == "VY-RES-006"
    assert event.event_type == "call.completed"


@pytest.mark.asyncio
async def test_postgres_repository_complete_operator_contract() -> None:
    from voicey.storage.postgres import PostgresRepository

    with pytest.raises(VoiceyError) as bad_settings:
        PostgresRepository("", min_size=2, max_size=1)
    assert bad_settings.value.code == "VY-RES-008"
    async with _isolated_postgres_dsn() as dsn, PostgresRepository(dsn) as repository:
        assert await repository.open() is repository
        now = datetime.now(UTC)
        invalid_call = _call("call_pg_invalid_time", started_at=now)
        with pytest.raises(VoiceyError) as naive_time:
            await repository.begin_call(
                invalid_call,
                owner_id="worker",
                delivery=ResultDeliveryConfig(
                    endpoint="https://receiver.example.test/results",
                ),
                lease_ttl=timedelta(seconds=30),
                now=datetime.now(),
            )
        assert naive_time.value.code == "VY-RES-008"
        with pytest.raises(VoiceyError) as invalid_ttl:
            await repository.begin_call(
                invalid_call,
                owner_id="worker",
                delivery=ResultDeliveryConfig(
                    endpoint="https://receiver.example.test/results",
                ),
                lease_ttl=timedelta(0),
                now=now,
            )
        assert invalid_ttl.value.code == "VY-RES-008"
        call = _call("call_pg_operator", started_at=now)
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
        renewed = await repository.renew_lease(
            lease,
            lease_ttl=timedelta(seconds=60),
            now=now,
        )
        await repository.update_provider_state(renewed, "answered")
        assert (await repository.current_relay_lease(call.call_id)).owner_id == "worker-a"
        await repository.assert_relay_fence(renewed)
        pending = await repository.get_recording_for_call(call.call_id)
        assert pending is not None
        with pytest.raises(VoiceyError) as early_recording:
            await repository.mark_recording_ready(
                RecordingReady(
                    recording_id=pending.recording_id,
                    access_url="https://relay.example.test/recordings/early",
                    storage_key="recordings/early.wav",
                )
            )
        assert early_recording.value.code == "VY-RES-010"
        with pytest.raises(VoiceyError) as premature_takeover:
            await repository.takeover_expired_call(
                call.call_id,
                owner_id="worker-b",
                lease_ttl=timedelta(seconds=30),
                now=now,
            )
        assert premature_takeover.value.code == "VY-RES-006"
        with pytest.raises(VoiceyError) as bad_handoff:
            await repository.handoff_call(
                call.call_id,
                expected_owner_id="wrong-owner",
                owner_id="worker-b",
                lease_ttl=timedelta(seconds=30),
                now=now,
            )
        assert bad_handoff.value.code == "VY-RES-006"

        event = await repository.terminalize(
            renewed,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="caller_hangup",
                ended_at=now,
            ),
        )
        assert await repository.get_event(event.event_id) == event
        assert (
            await repository.terminalize(
                renewed,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="caller_hangup",
                    ended_at=now,
                ),
            )
            == event
        )
        pending = await repository.get_recording_for_call(call.call_id)
        assert pending is not None
        update = RecordingReady(
            recording_id=pending.recording_id,
            access_url=f"https://relay.example.test/recordings/{pending.recording_id}",
            storage_key=f"recordings/{pending.recording_id}.wav",
            ready_at=now,
        )
        ready = await repository.mark_recording_ready(update)
        assert await repository.mark_recording_ready(update) == ready
        assert (await repository.get_recording(pending.recording_id)).status == "ready"
        assert call.call_id in {item.call_id for item in await repository.list_calls()}
        assert call.call_id in {
            item.call_id for item in await repository.list_calls(status="completed")
        }
        with pytest.raises(VoiceyError) as bad_list_limit:
            await repository.list_calls(limit=0)
        assert bad_list_limit.value.code == "VY-OBS-005"
        with pytest.raises(VoiceyError) as bad_claim_limit:
            await repository.claim_deliveries(
                owner_id="delivery",
                limit=0,
                lease_ttl=timedelta(seconds=30),
            )
        assert bad_claim_limit.value.code == "VY-OBS-005"
        with pytest.raises(VoiceyError) as missing_ready:
            await repository.mark_recording_ready(
                RecordingReady(
                    recording_id="missing",
                    access_url="https://relay.example.test/recordings/missing",
                    storage_key="recordings/missing.wav",
                )
            )
        assert missing_ready.value.code == "VY-RES-010"
        with pytest.raises(VoiceyError) as stale_ready:
            await repository.mark_recording_ready(
                update,
                relay_lease=renewed.model_copy(update={"owner_id": "stale"}),
            )
        assert stale_ready.value.code == "VY-REL-004"

        claims = await repository.claim_deliveries(
            owner_id="delivery",
            limit=100,
            lease_ttl=timedelta(seconds=30),
            now=now,
        )
        terminal_claim = next(item for item in claims if item.event_id == event.event_id)
        with pytest.raises(VoiceyError) as invalid_jitter:
            await repository.fail_delivery(
                terminal_claim,
                error="transient",
                jitter=lambda _: 999,
                now=now,
            )
        assert invalid_jitter.value.code == "VY-RES-008"
        failed = await repository.fail_delivery(
            terminal_claim,
            error="transient secret=redacted",
            jitter=lambda delay: delay,
            now=now,
        )
        while failed.status != "dead_lettered":
            retry_claims = await repository.claim_deliveries(
                owner_id="delivery",
                limit=100,
                lease_ttl=timedelta(seconds=30),
                now=failed.next_attempt_at,
            )
            terminal_claim = next(item for item in retry_claims if item.event_id == event.event_id)
            failed = await repository.fail_delivery(
                terminal_claim,
                error="still failing",
                jitter=lambda delay: delay,
                now=failed.next_attempt_at,
            )
        assert await repository.dlq_depth() == 1
        assert any(
            item.event_id == event.event_id
            for item in await repository.list_deliveries(undelivered_only=True)
        )
        redelivered = await repository.redeliver(event.event_id, now=now)
        assert redelivered.status == "pending"
        redelivery_claims = await repository.claim_deliveries(
            owner_id="delivery-final",
            limit=100,
            lease_ttl=timedelta(seconds=30),
            now=now,
        )
        terminal_claim = next(item for item in redelivery_claims if item.event_id == event.event_id)
        with pytest.raises(VoiceyError) as lost_claim:
            await repository.acknowledge_delivery(
                terminal_claim.model_copy(update={"lease_owner": "wrong"}),
                now=now,
            )
        assert lost_claim.value.code == "VY-RES-008"
        await repository.acknowledge_delivery(terminal_claim, now=now)
        assert any(
            item.status == "delivered"
            for item in await repository.list_deliveries()
            if item.event_id == event.event_id
        )

        failed_call = _call("call_pg_recording_failed", started_at=now)
        failed_lease = await repository.begin_call(
            failed_call,
            owner_id="worker-failed",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
                recording_enabled=True,
            ),
            lease_ttl=timedelta(seconds=30),
            now=now,
        )
        await repository.mark_recording_failed_fenced(failed_lease)
        await repository.mark_recording_failed(failed_call.call_id)
        failed_recording = await repository.get_recording_for_call(failed_call.call_id)
        assert failed_recording is not None
        assert failed_recording.status == "failed"

        no_recording_call = _call("call_pg_no_recording", started_at=now)
        no_recording_lease = await repository.begin_call(
            no_recording_call,
            owner_id="worker-no-recording",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
            ),
            lease_ttl=timedelta(seconds=30),
            now=now,
        )
        with pytest.raises(VoiceyError) as fenced_missing_recording:
            await repository.mark_recording_failed_fenced(no_recording_lease)
        assert fenced_missing_recording.value.code == "VY-RES-010"

        stale_call = _call("call_pg_stale", started_at=now)
        stale_lease = await repository.begin_call(
            stale_call,
            owner_id="stale-worker",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example.test/results",
            ),
            lease_ttl=timedelta(seconds=1),
            now=now,
        )
        assert stale_call.call_id in await repository.list_stale_calls(
            now=now + timedelta(seconds=2)
        )
        current = await repository.takeover_expired_call(
            stale_call.call_id,
            owner_id="recovery-worker",
            lease_ttl=timedelta(seconds=30),
            now=now + timedelta(seconds=2),
        )
        with pytest.raises(VoiceyError) as relay_fence:
            await repository.assert_relay_fence(stale_lease)
        assert relay_fence.value.code == "VY-REL-004"
        await repository.terminalize(
            current,
            TerminalRequest(event_type="call.failed", ended_reason="worker_crash"),
        )

        backup_key = f"backups/{uuid.uuid4().hex}.sqlite3"
        await repository.register_backup(
            backup_id=f"backup_{uuid.uuid4().hex}",
            storage_key=backup_key,
            expires_at=now - timedelta(seconds=1),
            now=now - timedelta(days=1),
        )
        purge = await repository.queue_retention(now=now)
        assert any(item.storage_key == backup_key for item in purge)
        await repository.acknowledge_purge(backup_key)

        for operation in (
            repository.get_call("missing"),
            repository.get_event("missing"),
            repository.get_terminal_event_for_call("missing"),
            repository.get_result_snapshot("missing"),
            repository.get_recording("missing"),
            repository.current_relay_lease("missing"),
            repository.redeliver("missing"),
        ):
            with pytest.raises(VoiceyError):
                await operation
        with pytest.raises(VoiceyError) as missing_recording:
            await repository.mark_recording_failed("missing")
        assert missing_recording.value.code == "VY-RES-010"
        await repository.close()
        await repository.close()


@pytest.mark.asyncio
async def test_postgres_migration_rejects_unknown_newer_schema() -> None:
    import psycopg

    from voicey.storage.postgres import PostgresRepository

    async with _isolated_postgres_dsn() as dsn:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                """
                CREATE TABLE voicey_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO voicey_schema_migrations(
                    version, name, checksum, applied_at
                ) VALUES (999, 'future.sql', 'future', %s)
                """,
                (datetime.now(UTC),),
            )
        repository = PostgresRepository(dsn)
        with pytest.raises(VoiceyError) as caught:
            await repository.open()
        assert caught.value.code == "VY-OBS-004"


@pytest.mark.asyncio
async def test_postgres_relay_journal_replay_and_ordering_contract() -> None:
    from voicey.relay.postgres import PostgresRelayJournal

    async with _isolated_postgres_dsn() as dsn:
        with pytest.raises(VoiceyError) as bad_settings:
            PostgresRelayJournal(dsn, min_size=2, max_size=1)
        assert bad_settings.value.code == "VY-REL-006"
        journal = PostgresRelayJournal(dsn, max_size=3)
        async with journal:
            assert await journal.open() is journal
            now = datetime.now(UTC)
            assert await journal.ready()
            await journal.claim_nonce(
                key_id="key",
                nonce="nonce",
                expires_at=now + timedelta(minutes=1),
                now=now,
            )
            with pytest.raises(VoiceyError) as replay:
                await journal.claim_nonce(
                    key_id="key",
                    nonce="nonce",
                    expires_at=now + timedelta(minutes=1),
                    now=now,
                )
            assert replay.value.code == "VY-REL-003"

            assert (
                await journal.reserve_request(
                    idempotency_key="request-key",
                    request_hash="hash-a",
                    request_kind="begin",
                    call_id="call-journal",
                    now=now,
                )
                is None
            )
            with pytest.raises(VoiceyError) as request_conflict:
                await journal.reserve_request(
                    idempotency_key="request-key",
                    request_hash="hash-b",
                    request_kind="begin",
                    call_id="call-journal",
                    now=now,
                )
            assert request_conflict.value.code == "VY-REL-005"
            await journal.complete_request(
                idempotency_key="request-key",
                request_hash="hash-a",
                response_body=b"ack",
            )
            assert (
                await journal.reserve_request(
                    idempotency_key="request-key",
                    request_hash="hash-a",
                    request_kind="begin",
                    call_id="call-journal",
                    now=now,
                )
                == b"ack"
            )
            with pytest.raises(VoiceyError) as missing_request:
                await journal.complete_request(
                    idempotency_key="missing",
                    request_hash="missing",
                    response_body=b"ack",
                )
            assert missing_request.value.code == "VY-REL-005"

            assert await journal.next_sequence("call-journal") == 1
            with pytest.raises(VoiceyError) as gap:
                await journal.reserve_update(
                    call_id="call-journal",
                    sequence=2,
                    idempotency_key="update-gap",
                    request_hash="hash-gap",
                    now=now,
                )
            assert gap.value.code == "VY-REL-005"
            assert (
                await journal.reserve_update(
                    call_id="call-journal",
                    sequence=1,
                    idempotency_key="update-one",
                    request_hash="hash-one",
                    now=now,
                )
                is None
            )
            with pytest.raises(VoiceyError) as update_conflict:
                await journal.reserve_update(
                    call_id="call-journal",
                    sequence=1,
                    idempotency_key="update-one",
                    request_hash="different",
                    now=now,
                )
            assert update_conflict.value.code == "VY-REL-005"
            with pytest.raises(VoiceyError) as missing_update:
                await journal.complete_update(
                    call_id="call-journal",
                    sequence=1,
                    idempotency_key="wrong",
                    request_hash="hash-one",
                    response_body=b"one",
                )
            assert missing_update.value.code == "VY-REL-005"
            await journal.complete_update(
                call_id="call-journal",
                sequence=1,
                idempotency_key="update-one",
                request_hash="hash-one",
                response_body=b"one",
            )
            assert await journal.next_sequence("call-journal") == 2
            assert (
                await journal.reserve_update(
                    call_id="call-journal",
                    sequence=1,
                    idempotency_key="update-one",
                    request_hash="hash-one",
                    now=now,
                )
                == b"one"
            )
        await journal.close()
