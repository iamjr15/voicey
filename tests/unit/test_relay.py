from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from voicey.errors import VoiceyError
from voicey.obs import LatencySample, NewCall, TimelineEvent, ToolCallObservation, TranscriptTurn
from voicey.relay import (
    RelayClient,
    RelayCredential,
    RelayKeyring,
    RepositoryRelayBackend,
    SQLiteRelayJournal,
    create_relay_app,
)
from voicey.relay.auth import FenceSigner, RelayRequestSigner, RelayRequestVerifier
from voicey.relay.models import RelayUpdateRequest
from voicey.storage import (
    RecordingReady,
    ResultDeliveryConfig,
    ResultSnapshot,
    SQLiteRepository,
    TerminalRequest,
)
from voicey.storage.models import CallLease

CONFIG_HASH = f"sha256:{'e' * 64}"


class _DropFirstUpdateResponse(httpx.AsyncBaseTransport):
    def __init__(self, app: object) -> None:
        self._inner = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        self.dropped = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.path.endswith("/updates") and not self.dropped:
            self.dropped = True
            await response.aread()
            await response.aclose()
            raise httpx.ReadError("simulated acknowledgement loss", request=request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CancelFirstUpdateResponse(httpx.AsyncBaseTransport):
    def __init__(self, app: object) -> None:
        self._inner = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        self.cancelled = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.path.endswith("/updates") and not self.cancelled:
            self.cancelled = True
            await response.aread()
            await response.aclose()
            raise asyncio.CancelledError
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _call(call_id: str = "call_relay", *, started_at: datetime | None = None) -> NewCall:
    return NewCall(
        call_id=call_id,
        agent_name="relay-agent",
        runtime="pipecat",
        channel="phone",
        direction="inbound",
        provider="twilio",
        provider_call_id=f"CA_{call_id}",
        config_hash=CONFIG_HASH,
        started_at=started_at or datetime.now(UTC),
    )


def _delivery(*, recording: bool = False) -> ResultDeliveryConfig:
    return ResultDeliveryConfig(
        endpoint="https://receiver.example.test/results",
        recording_enabled=recording,
    )


@pytest.mark.asyncio
async def test_relay_full_runtime_stream_survives_lost_ack_exactly_once(
    tmp_path: Path,
) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        backend = RepositoryRelayBackend(
            repository,
            journal,
            fences=FenceSigner(keyring),
        )
        app = create_relay_app(backend, keyring=keyring)
        transport = _DropFirstUpdateResponse(app)
        http = httpx.AsyncClient(transport=transport, base_url="https://relay.test")
        async with RelayClient(
            "https://relay.test",
            credential,
            client=http,
        ) as client:
            lease = await client.begin_call(
                _call(),
                owner_id="worker-a",
                delivery=_delivery(),
                lease_ttl=timedelta(seconds=30),
            )
            await client.append_timeline(
                lease.call_id,
                TimelineEvent(event_type="runtime.admitted"),
            )
            await client.append_transcript(
                lease.call_id,
                TranscriptTurn(
                    turn_id="turn-1",
                    role="user",
                    text="Book Tuesday",
                    t_ms=100,
                ),
            )
            await client.record_tool_call(
                lease.call_id,
                ToolCallObservation(
                    invocation_id="inv-1",
                    tool_name="book",
                    arguments={"day": "Tuesday"},
                    result={"ok": True},
                    duration_ms=12,
                    status="succeeded",
                ),
            )
            await client.record_latency(
                lease.call_id,
                LatencySample(
                    turn_id="turn-1",
                    turn_index=1,
                    metric="e2e",
                    duration_ms=600,
                    observed_at=datetime.now(UTC),
                ),
            )
            await client.flush_results(
                lease,
                ResultSnapshot(outcome="booked", data={"day": "Tuesday"}),
            )
            await client.update_provider_state(lease, "active")
            assert await client.get_recording_for_call(lease.call_id) is None
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
            call = await client.get_call(lease.call_id)

        assert transport.dropped
        assert event.event_type == "call.completed"
        assert call.status == "completed"
        assert [item.event_type for item in call.timeline] == ["runtime.admitted"]
        assert len(call.transcript) == len(call.tool_calls) == len(call.latency) == 1
        assert await journal.next_sequence(lease.call_id) == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_same_operation", [False, True])
async def test_relay_stream_survives_cancelled_ack_exactly_once(
    tmp_path: Path,
    *,
    repeat_same_operation: bool,
) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        transport = _CancelFirstUpdateResponse(app)
        http = httpx.AsyncClient(transport=transport, base_url="https://relay.test")
        async with RelayClient(
            "https://relay.test",
            credential,
            client=http,
        ) as client:
            lease = await client.begin_call(
                _call("call_cancelled_ack"),
                owner_id="worker-a",
                delivery=_delivery(),
                lease_ttl=timedelta(seconds=30),
            )
            admitted = TimelineEvent(event_type="runtime.admitted")
            with pytest.raises(asyncio.CancelledError):
                await client.append_timeline(lease.call_id, admitted)
            if repeat_same_operation:
                await client.append_timeline(lease.call_id, admitted)
            event = await client.terminalize(
                lease,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="caller_hangup",
                ),
            )
            call = await client.get_call(lease.call_id)

        assert transport.cancelled
        assert event.event_type == "call.completed"
        assert [item.event_type for item in call.timeline] == ["runtime.admitted"]
        assert await journal.next_sequence(lease.call_id) == 3


@pytest.mark.asyncio
async def test_relay_rejects_nonce_replay_before_route_execution(tmp_path: Path) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        signer = RelayRequestSigner(credential)
        headers = signer.headers("GET", "/v1/ready", b"")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        ) as client:
            first = await client.get("/v1/ready", headers=headers)
            replay = await client.get("/v1/ready", headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "VY-REL-003"


@pytest.mark.asyncio
async def test_relay_rotation_accepts_previous_key_and_unknown_key_fails_closed(
    tmp_path: Path,
) -> None:
    current = RelayCredential.issue("current-key")
    previous = RelayCredential.issue("previous-key")
    unknown = RelayCredential.issue("unknown-key")
    keyring = RelayKeyring(current=current, previous=previous)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        transport = httpx.ASGITransport(app=app)
        previous_http = httpx.AsyncClient(transport=transport, base_url="https://relay.test")
        unknown_http = httpx.AsyncClient(transport=transport, base_url="https://relay.test")
        async with RelayClient(
            "https://relay.test",
            previous,
            client=previous_http,
        ) as previous_client:
            assert previous_client is not None
        with pytest.raises(VoiceyError) as caught:
            await RelayClient(
                "https://relay.test",
                unknown,
                client=unknown_http,
                max_attempts=1,
            ).open()
        await previous_http.aclose()
        await unknown_http.aclose()

    assert caught.value.code == "VY-REL-003"


@pytest.mark.asyncio
async def test_relay_rejects_sequence_gap_and_idempotency_conflict(tmp_path: Path) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        backend = RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring))
        app = create_relay_app(backend, keyring=keyring)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient("https://relay.test", credential, client=http) as client:
            lease = await client.begin_call(
                _call("call_order"),
                owner_id="worker-a",
                delivery=_delivery(),
                lease_ttl=timedelta(seconds=30),
            )
        current = await repository.current_relay_lease(lease.call_id)
        fence = backend.fences.issue(current)
        signer = RelayRequestSigner(credential)
        request = RelayUpdateRequest(
            sequence=2,
            idempotency_key="op_gap_1234567890123456",
            fence_token=fence,
            operation="append_timeline",
            payload=TimelineEvent(event_type="bad.gap").model_dump(mode="json"),
        )
        path = f"/v1/calls/{lease.call_id}/updates"
        body = request.model_dump_json().encode()
        response = await http.post(path, content=body, headers=signer.headers("POST", path, body))
        await http.aclose()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VY-REL-005"


@pytest.mark.asyncio
async def test_relay_rejects_idempotency_key_reuse_with_different_bytes(
    tmp_path: Path,
) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        backend = RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring))
        app = create_relay_app(backend, keyring=keyring)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient("https://relay.test", credential, client=http) as client:
            lease = await client.begin_call(
                _call("call_conflict"),
                owner_id="worker-a",
                delivery=_delivery(),
                lease_ttl=timedelta(seconds=30),
            )
        fence = backend.fences.issue(await repository.current_relay_lease(lease.call_id))
        signer = RelayRequestSigner(credential)
        path = f"/v1/calls/{lease.call_id}/updates"
        first_request = RelayUpdateRequest(
            sequence=1,
            idempotency_key="op_same_123456789012345",
            fence_token=fence,
            operation="append_timeline",
            payload=TimelineEvent(event_type="first").model_dump(mode="json"),
        )
        first_body = first_request.model_dump_json().encode()
        first = await http.post(
            path,
            content=first_body,
            headers=signer.headers("POST", path, first_body),
        )
        conflicting_request = first_request.model_copy(
            update={
                "payload": TimelineEvent(event_type="different").model_dump(mode="json"),
            }
        )
        conflicting_body = conflicting_request.model_dump_json().encode()
        conflicting = await http.post(
            path,
            content=conflicting_body,
            headers=signer.headers("POST", path, conflicting_body),
        )
        await http.aclose()

    assert first.status_code == 200
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "VY-REL-005"


@pytest.mark.asyncio
async def test_relay_handoff_and_recording_events_cross_worker_clients(
    tmp_path: Path,
) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        first_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient(
            "https://relay.test",
            credential,
            client=first_http,
        ) as reservation_client:
            await reservation_client.begin_call(
                _call("call_recording"),
                owner_id="reservation-owner",
                delivery=_delivery(recording=True),
                lease_ttl=timedelta(seconds=30),
            )
        second_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient(
            "https://relay.test",
            credential,
            client=second_http,
        ) as worker_client:
            lease = await worker_client.handoff_call(
                "call_recording",
                expected_owner_id="reservation-owner",
                owner_id="worker-b",
                lease_ttl=timedelta(seconds=30),
            )
            await worker_client.terminalize(
                lease,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="caller_hangup",
                ),
            )
            pending = await worker_client.get_recording_for_call(lease.call_id)
            assert pending is not None
            ready_update = RecordingReady(
                recording_id=pending.recording_id,
                access_url=f"https://relay.example.test/recordings/{pending.recording_id}",
                storage_key=f"recordings/{pending.recording_id}.mp3",
            )
            ready_event = await worker_client.mark_recording_ready(ready_update)
            duplicate_event = await worker_client.mark_recording_ready(ready_update)
            ready = await worker_client.get_recording(pending.recording_id)

            failed_lease = await worker_client.begin_call(
                _call("call_recording_failed"),
                owner_id="worker-b",
                delivery=_delivery(recording=True),
                lease_ttl=timedelta(seconds=30),
            )
            await worker_client.mark_recording_failed(failed_lease.call_id)
            failed = await worker_client.get_recording_for_call(failed_lease.call_id)
            await worker_client.terminalize(
                failed_lease,
                TerminalRequest(
                    event_type="call.failed",
                    ended_reason="provider_error",
                ),
            )
        await first_http.aclose()
        await second_http.aclose()

    assert lease.owner_id == "worker-b"
    assert lease.generation == 2
    assert ready_event == duplicate_event
    assert ready.status == "ready"
    assert failed is not None
    assert failed.status == "failed"


@pytest.mark.asyncio
async def test_relay_rejects_tampered_signature_and_fence(tmp_path: Path) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        backend = RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring))
        app = create_relay_app(backend, keyring=keyring)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        ) as http:
            signer = RelayRequestSigner(credential)
            bad_headers = signer.headers("GET", "/v1/ready", b"")
            bad_headers["x-voicey-relay-signature"] = "tampered"
            signature_response = await http.get("/v1/ready", headers=bad_headers)

            async with RelayClient(
                "https://relay.test",
                credential,
                client=http,
            ) as client:
                lease = await client.begin_call(
                    _call("call_tamper"),
                    owner_id="worker-a",
                    delivery=_delivery(),
                    lease_ttl=timedelta(seconds=30),
                )
            request = RelayUpdateRequest(
                sequence=1,
                idempotency_key="op_tamper_1234567890123",
                fence_token="x" * 32,
                operation="append_timeline",
                payload=TimelineEvent(event_type="tampered").model_dump(mode="json"),
            )
            path = f"/v1/calls/{lease.call_id}/updates"
            body = request.model_dump_json().encode()
            fence_response = await http.post(
                path,
                content=body,
                headers=signer.headers("POST", path, body),
            )

    assert signature_response.status_code == 401
    assert signature_response.json()["error"]["code"] == "VY-REL-003"
    assert fence_response.status_code == 401
    assert fence_response.json()["error"]["code"] == "VY-REL-004"


@pytest.mark.asyncio
async def test_relay_rejects_old_generation_after_server_takeover(tmp_path: Path) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    started = datetime.now(UTC) - timedelta(seconds=10)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient("https://relay.test", credential, client=http) as client:
            lease = await client.begin_call(
                _call("call_stale", started_at=started),
                owner_id="old-worker",
                delivery=_delivery(),
                lease_ttl=timedelta(seconds=1),
                now=started,
            )
            await repository.takeover_expired_call(
                lease.call_id,
                owner_id="recovery-worker",
                lease_ttl=timedelta(seconds=30),
                now=datetime.now(UTC),
            )
            with pytest.raises(VoiceyError) as caught:
                await client.append_timeline(
                    lease.call_id,
                    TimelineEvent(event_type="stale.write"),
                )
        await http.aclose()

    assert caught.value.code == "VY-REL-004"


@pytest.mark.asyncio
async def test_relay_client_requires_ready_ack_before_begin() -> None:
    client = RelayClient(
        "https://relay.example.test",
        RelayCredential.issue("current-key"),
    )
    with pytest.raises(VoiceyError) as caught:
        await client.begin_call(
            _call(),
            owner_id="worker-a",
            delivery=_delivery(),
            lease_ttl=timedelta(seconds=30),
        )
    await client.close()

    assert caught.value.code == "VY-REL-002"


@pytest.mark.asyncio
async def test_open_relay_client_rejects_updates_without_a_call_fence(
    tmp_path: Path,
) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        )
        async with RelayClient("https://relay.test", credential, client=http) as client:
            with pytest.raises(VoiceyError) as caught:
                await client.append_timeline(
                    "missing-call",
                    TimelineEvent(event_type="missing"),
                )
        await http.aclose()

    assert caught.value.code == "VY-REL-004"


@pytest.mark.asyncio
async def test_signed_relay_body_still_requires_strict_wire_schema(tmp_path: Path) -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        app = create_relay_app(
            RepositoryRelayBackend(repository, journal, fences=FenceSigner(keyring)),
            keyring=keyring,
        )
        body = b"{}"
        path = "/v1/calls/begin"
        headers = RelayRequestSigner(credential).headers("POST", path, body)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://relay.test",
        ) as client:
            response = await client.post(path, content=body, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VY-REL-001"


def test_relay_credentials_are_strong_rotatable_and_secret_safe() -> None:
    credential = RelayCredential.issue("current-key")
    parsed = RelayCredential.parse(credential.reveal())

    assert parsed == credential
    assert credential.reveal() not in repr(credential)
    with pytest.raises(VoiceyError):
        RelayCredential.parse("not-a-relay-credential")


def test_relay_auth_configuration_and_token_failures_are_catalogued() -> None:
    credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=credential)
    now = datetime.now(UTC)
    lease = CallLease(
        call_id="call_auth",
        owner_id="worker-a",
        generation=1,
        expires_at=now + timedelta(seconds=30),
    )

    with pytest.raises(VoiceyError):
        RelayCredential(key_id="x", secret=b"short")
    with pytest.raises(VoiceyError):
        RelayCredential.parse("vkr_current-key_A")
    with pytest.raises(VoiceyError):
        RelayKeyring(current=credential, previous=credential)
    with pytest.raises(VoiceyError):
        RelayRequestVerifier(keyring, tolerance=timedelta(0))
    with pytest.raises(VoiceyError):
        FenceSigner(keyring, token_ttl=timedelta(seconds=1))

    verifier = RelayRequestVerifier(keyring)
    malformed = RelayRequestSigner(credential).headers("GET", "/v1/ready", b"")
    malformed["x-voicey-relay-timestamp"] = "not-an-integer"
    with pytest.raises(VoiceyError):
        verifier.verify(malformed, method="GET", path="/v1/ready", body=b"")
    expired = RelayRequestSigner(credential, clock=lambda: 0).headers(
        "GET",
        "/v1/ready",
        b"",
    )
    with pytest.raises(VoiceyError):
        verifier.verify(expired, method="GET", path="/v1/ready", body=b"")

    fences = FenceSigner(keyring, clock=lambda: now)
    token = fences.issue(lease)
    with pytest.raises(VoiceyError):
        fences.verify("bad-token", call_id=lease.call_id)
    key_id, _payload, _signature = token.split(".")
    with pytest.raises(VoiceyError):
        fences.verify(f"{key_id}.bad.bad", call_id=lease.call_id)
    with pytest.raises(VoiceyError):
        fences.verify(token, call_id="another-call")
    expired_fences = FenceSigner(
        keyring,
        clock=lambda: now + timedelta(hours=2),
    )
    with pytest.raises(VoiceyError):
        expired_fences.verify(token, call_id=lease.call_id)


@pytest.mark.asyncio
async def test_relay_journal_rejects_invalid_direct_state_transitions(
    tmp_path: Path,
) -> None:
    journal = SQLiteRelayJournal(tmp_path / "relay.sqlite3")
    with pytest.raises(VoiceyError):
        await journal.ready()
    await journal.open()
    assert await journal.open() is journal
    now = datetime.now(UTC)
    assert (
        await journal.reserve_request(
            idempotency_key="op_request_123456789012",
            request_hash="hash-a",
            request_kind="begin",
            call_id="call-a",
            now=now,
        )
        is None
    )
    with pytest.raises(VoiceyError):
        await journal.reserve_request(
            idempotency_key="op_request_123456789012",
            request_hash="hash-b",
            request_kind="begin",
            call_id="call-a",
            now=now,
        )
    with pytest.raises(VoiceyError):
        await journal.complete_request(
            idempotency_key="missing_request_123456",
            request_hash="hash",
            response_body=b"{}",
        )
    with pytest.raises(VoiceyError):
        await journal.complete_update(
            call_id="missing",
            sequence=1,
            idempotency_key="missing_update_1234567",
            request_hash="hash",
            response_body=b"{}",
        )
    with pytest.raises(VoiceyError):
        await journal.reserve_request(
            idempotency_key="naive_time_1234567890",
            request_hash="hash",
            request_kind="begin",
            call_id="call",
            now=datetime.now(),
        )
    await journal.close()
    await journal.close()


@pytest.mark.asyncio
async def test_relay_client_transport_and_response_failures_are_catalogued() -> None:
    credential = RelayCredential.issue("current-key")
    with pytest.raises(VoiceyError):
        RelayClient("http://relay.example.test", credential)
    with pytest.raises(VoiceyError):
        RelayClient("https://relay.example.test?bad=1", credential)

    async def invalid_ready(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    invalid_http = httpx.AsyncClient(
        transport=httpx.MockTransport(invalid_ready),
        base_url="https://relay.test",
    )
    with pytest.raises(VoiceyError) as invalid:
        await RelayClient(
            "https://relay.test",
            credential,
            client=invalid_http,
        ).open()
    await invalid_http.aclose()

    async def broken_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("offline", request=request)

    broken_http = httpx.AsyncClient(
        transport=httpx.MockTransport(broken_transport),
        base_url="https://relay.test",
    )
    with pytest.raises(VoiceyError) as unavailable:
        await RelayClient(
            "https://relay.test",
            credential,
            client=broken_http,
            max_attempts=2,
        ).open()
    await broken_http.aclose()

    assert invalid.value.code == "VY-REL-002"
    assert unavailable.value.code == "VY-REL-002"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(400, text="not-json"), "VY-REL-006"),
        (
            httpx.Response(
                400,
                json={"error": {"code": "NOT-CATALOGUED", "detail": "bad"}},
            ),
            "VY-REL-006",
        ),
        (httpx.Response(503, json={"unexpected": True}), "VY-REL-002"),
    ],
)
async def test_relay_http_error_fallback_never_trusts_unknown_error_codes(
    response: httpx.Response,
    expected_code: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://relay.test",
    )
    with pytest.raises(VoiceyError) as caught:
        await RelayClient(
            "https://relay.test",
            RelayCredential.issue("current-key"),
            client=http,
            max_attempts=1,
        ).open()
    await http.aclose()

    assert caught.value.code == expected_code
