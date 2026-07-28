from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from voicekit.deploy.results_service import ResultsServiceSettings
from voicekit.errors import VoicekitError
from voicekit.obs import NewCall, TimelineEvent
from voicekit.relay import RelayClient, RelayCredential, RelayKeyring, SQLiteRelayJournal
from voicekit.relay.companion import (
    CompanionMaintenance,
    CompanionService,
    CompanionSettings,
)
from voicekit.relay.recording import (
    CallbackProvider,
    CarrierCallbackIngress,
    add_carrier_callback_routes,
    parse_callback_providers,
)
from voicekit.results import DeliveryWorker, encode_secret
from voicekit.storage import (
    LocalArtifactStore,
    RecordingReady,
    ResultDeliveryConfig,
    SQLiteRepository,
    TerminalRequest,
)
from voicekit.telephony.models import CallEvent, TelephonyRequest

_CONFIG_HASH = f"sha256:{'4' * 64}"
_RESULT_CURRENT = encode_secret(b"c" * 32)
_RESULT_PREVIOUS = encode_secret(b"p" * 32)


async def _ready() -> bool:
    return True


def _call(call_id: str, *, started_at: datetime | None = None) -> NewCall:
    return NewCall(
        call_id=call_id,
        agent_name="companion-test",
        runtime="pipecat",
        channel="phone",
        direction="inbound",
        provider="twilio",
        provider_call_id=call_id,
        config_hash=_CONFIG_HASH,
        started_at=started_at or datetime.now(UTC),
    )


def _delivery(*, recording: bool = False) -> ResultDeliveryConfig:
    return ResultDeliveryConfig(
        endpoint="https://receiver.example.test/results",
        recording_enabled=recording,
    )


def _settings_environment() -> dict[str, str]:
    database_url = "postgresql://voicekit:password@db.test/voicekit"  # pragma: allowlist secret
    return {
        "VOICEKIT_PUBLIC_BASE": "https://results.example.test",
        "DATABASE_URL": database_url,
        "VOICEKIT_OBJECT_BUCKET": "voicekit-artifacts",
        "VOICEKIT_RELAY_CREDENTIAL": RelayCredential.issue("current-key").reveal(),
        "VOICEKIT_RESULTS_SECRET": _RESULT_CURRENT,
        "VOICEKIT_DEPLOY_TARGET": "fly",
        "VOICEKIT_STORAGE_BACKEND": "postgres",
        "VOICEKIT_ARTIFACT_BACKEND": "s3",
    }


def test_results_service_settings_fail_closed_and_hide_secrets() -> None:
    environment = _settings_environment()
    settings = ResultsServiceSettings.from_environment(environment)

    rendered = repr(settings)
    assert settings.target == "fly"
    assert settings.pool_max == 5
    assert environment["DATABASE_URL"] not in rendered
    assert environment["VOICEKIT_RELAY_CREDENTIAL"] not in rendered
    assert _RESULT_CURRENT not in rendered

    environment["VOICEKIT_STORAGE_BACKEND"] = "sqlite"
    with pytest.raises(VoicekitError) as topology:
        ResultsServiceSettings.from_environment(environment)
    assert topology.value.code == "VK-DEP-002"

    invalid = _settings_environment()
    invalid["VOICEKIT_DB_CONNECTION_BUDGET"] = "5"
    with pytest.raises(VoicekitError) as budget:
        ResultsServiceSettings.from_environment(invalid)
    assert budget.value.code == "VK-DEP-003"

    with pytest.raises(VoicekitError) as public_base:
        CompanionSettings(
            public_base="http://results.example.test/",
            recovery_owner="results-test",
        )
    assert public_base.value.code == "VK-DEP-003"
    with pytest.raises(VoicekitError) as owner:
        CompanionSettings(
            public_base="https://results.example.test",
            recovery_owner="",
        )
    assert owner.value.code == "VK-DEP-003"

    recording = _settings_environment()
    recording["VOICEKIT_CALLBACK_PROVIDERS"] = "twilio"
    with pytest.raises(VoicekitError) as credentials:
        ResultsServiceSettings.from_environment(recording)
    assert credentials.value.code == "VK-DEP-003"


class _RecordingAdapter:
    def __init__(self, event: CallEvent, *, valid: bool = True) -> None:
        self.event = event
        self.valid = valid
        self.requests: list[TelephonyRequest] = []

    def verify_request(self, request: TelephonyRequest) -> bool:
        self.requests.append(request)
        return self.valid

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        _ = request
        return self.event


@pytest.mark.asyncio
async def test_recording_ingress_is_explicit_authenticated_and_uses_public_url() -> None:
    event = CallEvent(
        type="recording_ready",
        provider_call_id="CA123",
        provider_status="completed",
        recording_sid="RE123",
    )
    adapter = _RecordingAdapter(event)
    handled: list[CallEvent] = []
    observed: list[CallEvent] = []

    async def handle(value: CallEvent) -> None:
        handled.append(value)

    async def observe(value: CallEvent) -> None:
        observed.append(value)

    app = FastAPI()
    add_carrier_callback_routes(
        app,
        public_base="https://results.example.test/relay",
        ingresses=(
            CarrierCallbackIngress(
                provider="twilio",
                adapter=adapter,
                handle=handle,
                observe=observe,
            ),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://internal",
    ) as http:
        accepted = await http.post(
            "/twilio/recordings",
            data={
                "CallSid": "CA123",
                "RecordingStatus": "completed",
                "RecordingSid": "RE123",
            },
        )
        adapter.event = CallEvent(
            type="completed",
            provider_call_id="CA123",
            provider_status="completed",
        )
        provider_event = await http.post(
            "/twilio/events",
            data={"CallSid": "CA123", "CallStatus": "completed"},
        )
        absent = await http.post("/plivo/recordings", data={})

    assert accepted.status_code == 204
    assert provider_event.status_code == 204
    assert absent.status_code == 404
    assert handled == [event]
    assert observed == [adapter.event]
    assert adapter.requests[0].scheme == "https"
    assert adapter.requests[0].host == "results.example.test"
    assert adapter.requests[0].path == "/relay/twilio/recordings"
    assert parse_callback_providers("telnyx, twilio") == ("telnyx", "twilio")

    with pytest.raises(VoicekitError) as duplicate:
        parse_callback_providers("twilio,twilio")
    assert duplicate.value.code == "VK-DEP-003"
    assert parse_callback_providers(" , ") == ()

    with pytest.raises(VoicekitError) as unknown:
        parse_callback_providers("unknown")
    assert unknown.value.code == "VK-DEP-003"

    with pytest.raises(VoicekitError) as duplicate_routes:
        add_carrier_callback_routes(
            FastAPI(),
            public_base="https://results.example.test",
            ingresses=(
                CarrierCallbackIngress("twilio", adapter, handle),
                CarrierCallbackIngress("twilio", adapter, handle),
            ),
        )
    assert duplicate_routes.value.code == "VK-DEP-003"


@pytest.mark.asyncio
async def test_all_carrier_callback_routes_and_failure_modes(tmp_path: Path) -> None:
    relay_credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=relay_credential)
    providers: tuple[CallbackProvider, ...] = ("twilio", "telnyx", "vobiz", "plivo")
    adapters = {
        provider: _RecordingAdapter(
            CallEvent(
                type="recording_ready",
                provider_call_id=f"{provider}-call",
                provider_status="ready",
                recording_sid=f"{provider}-recording",
            )
        )
        for provider in providers
    }
    handled: list[str] = []
    observed: list[str] = []

    async def handle(event: CallEvent) -> None:
        if event.provider_call_id == "pending":
            raise VoicekitError("VK-RES-010", detail="pending")
        if event.provider_call_id == "artifact-error":
            raise VoicekitError("VK-ART-002", detail="object")
        handled.append(event.provider_call_id)

    async def observe(event: CallEvent) -> None:
        observed.append(event.provider_call_id)

    ingresses = tuple(
        CarrierCallbackIngress(
            provider=provider,
            adapter=adapters[provider],
            handle=handle,
            observe=None if provider == "plivo" else observe,
        )
        for provider in providers
    )
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        service = CompanionService(
            repository,
            journal,
            LocalArtifactStore(tmp_path / "artifacts"),
            keyring=keyring,
            current_result_secret=_RESULT_CURRENT,
            previous_result_secret=None,
            settings=CompanionSettings(
                public_base="https://results.example.test",
                recovery_owner="results-test",
            ),
            artifact_ready=_ready,
            callback_ingresses=ingresses,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app),
            base_url="https://results.example.test",
        ) as http:
            for provider in adapters:
                response = await http.post(
                    f"/{provider}/recordings",
                    content=b"{}" if provider == "telnyx" else None,
                    data=None if provider == "telnyx" else {"recording": "ready"},
                )
                assert response.status_code == 204

            for provider, adapter in adapters.items():
                adapter.event = CallEvent(
                    type="completed",
                    provider_call_id=f"{provider}-completed",
                    provider_status="completed",
                )
                response = await http.post(
                    f"/{provider}/events",
                    content=b"{}" if provider == "telnyx" else None,
                    data=None if provider == "telnyx" else {"status": "completed"},
                )
                expected = 400 if provider == "plivo" else 204
                assert response.status_code == expected

            adapters["twilio"].event = CallEvent(
                type="completed",
                provider_call_id="wrong-kind",
                provider_status="completed",
            )
            wrong_kind = await http.post("/twilio/recordings", data={"status": "completed"})

            adapters["vobiz"].event = CallEvent(
                type="recording_ready",
                provider_call_id="pending",
                provider_status="ready",
                recording_sid="recording-pending",
            )
            pending = await http.post("/vobiz/recordings", data={"recording": "ready"})

            adapters["vobiz"].event = CallEvent(
                type="recording_ready",
                provider_call_id="artifact-error",
                provider_status="ready",
                recording_sid="recording-error",
            )
            artifact_error = await http.post(
                "/vobiz/recordings",
                data={"recording": "ready"},
            )
            invalid_utf8 = await http.post("/telnyx/events", content=b"\xff")

    assert sorted(handled) == sorted(f"{provider}-call" for provider in adapters)
    assert sorted(observed) == sorted(
        f"{provider}-completed" for provider in ("twilio", "telnyx", "vobiz")
    )
    assert wrong_kind.status_code == 503
    assert wrong_kind.json()["error"]["code"] == "VK-TEL-009"
    assert pending.status_code == 503
    assert pending.json()["error"]["code"] == "VK-TEL-009"
    assert artifact_error.status_code == 409
    assert artifact_error.json()["error"]["code"] == "VK-ART-002"
    assert invalid_utf8.status_code == 400
    assert invalid_utf8.json()["error"]["code"] == "VK-TEL-008"


@pytest.mark.asyncio
async def test_companion_signed_readiness_drain_and_existing_update(tmp_path: Path) -> None:
    relay_credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=relay_credential)
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        invalid_recording = _RecordingAdapter(
            CallEvent(
                type="recording_ready",
                provider_call_id="CA_invalid",
                provider_status="completed",
                recording_sid="RE_invalid",
            ),
            valid=False,
        )

        async def reject_handle(_event: CallEvent) -> None:
            raise AssertionError("invalid callback reached its handler")

        service = CompanionService(
            repository,
            journal,
            LocalArtifactStore(tmp_path / "artifacts"),
            keyring=keyring,
            current_result_secret=_RESULT_CURRENT,
            previous_result_secret=_RESULT_PREVIOUS,
            settings=CompanionSettings(
                public_base="https://results.example.test",
                recovery_owner="results-test",
            ),
            artifact_ready=_ready,
            callback_ingresses=(
                CarrierCallbackIngress(
                    provider="twilio",
                    adapter=invalid_recording,
                    handle=reject_handle,
                ),
            ),
        )
        transport = httpx.ASGITransport(app=service.app)
        http = httpx.AsyncClient(transport=transport, base_url="https://results.example.test")
        async with RelayClient(
            "https://results.example.test",
            relay_credential,
            client=http,
        ) as client:
            lease = await client.begin_call(
                _call("call_drain"),
                owner_id="worker-a",
                delivery=_delivery(),
                lease_ttl=timedelta(seconds=30),
            )
            before = await http.get("/healthz")
            rejected_recording = await http.post(
                "/twilio/recordings",
                data={"CallSid": "CA_invalid"},
            )
            service.begin_drain()
            after = await http.get("/healthz")
            await client.append_timeline(
                lease.call_id,
                TimelineEvent(event_type="worker.finishing"),
            )
            with pytest.raises(VoicekitError) as caught:
                await client.begin_call(
                    _call("call_rejected"),
                    owner_id="worker-b",
                    delivery=_delivery(),
                    lease_ttl=timedelta(seconds=30),
                )
        await http.aclose()

        call = await repository.get_call(lease.call_id)

    assert before.status_code == 200
    assert before.json()["signed_readiness_path"] == "/v1/ready"
    assert rejected_recording.status_code == 403
    assert rejected_recording.json()["error"]["code"] == "VK-RUN-007"
    assert after.status_code == 503
    assert caught.value.code == "VK-REL-002"
    assert [item.event_type for item in call.timeline] == ["worker.finishing"]


@pytest.mark.asyncio
async def test_companion_admin_and_artifact_reads_accept_rotation_only(
    tmp_path: Path,
) -> None:
    relay_credential = RelayCredential.issue("current-key")
    keyring = RelayKeyring(current=relay_credential)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    async with (
        SQLiteRepository(tmp_path / "calls.sqlite3") as repository,
        SQLiteRelayJournal(tmp_path / "relay.sqlite3") as journal,
    ):
        lease = await repository.begin_call(
            _call("call_recorded"),
            owner_id="worker-a",
            delivery=_delivery(recording=True),
            lease_ttl=timedelta(seconds=30),
        )
        await repository.terminalize(
            lease,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="caller_hangup",
            ),
        )
        pending = await repository.get_recording_for_call(lease.call_id)
        assert pending is not None
        storage_key = f"recordings/{pending.recording_id}.mp3"
        await artifacts.put(storage_key, b"audio-bytes")
        await repository.mark_recording_ready(
            RecordingReady(
                recording_id=pending.recording_id,
                access_url=(f"https://results.example.test/recordings/{pending.recording_id}"),
                storage_key=storage_key,
            )
        )
        service = CompanionService(
            repository,
            journal,
            artifacts,
            keyring=keyring,
            current_result_secret=_RESULT_CURRENT,
            previous_result_secret=_RESULT_PREVIOUS,
            settings=CompanionSettings(
                public_base="https://results.example.test",
                recovery_owner="results-test",
            ),
            artifact_ready=_ready,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app),
            base_url="https://results.example.test",
        ) as http:
            unauthorized = await http.get(f"/recordings/{pending.recording_id}")
            previous = await http.get(
                f"/recordings/{pending.recording_id}",
                headers={"authorization": f"Bearer {_RESULT_PREVIOUS}"},
            )
            calls = await http.get(
                "/v1/admin/calls",
                headers={"authorization": f"Bearer {_RESULT_CURRENT}"},
            )
            result = await http.get(
                f"/v1/admin/calls/{lease.call_id}/result",
                headers={"authorization": f"Bearer {_RESULT_CURRENT}"},
            )
            call = await http.get(
                f"/v1/admin/calls/{lease.call_id}",
                headers={"authorization": f"Bearer {_RESULT_CURRENT}"},
            )
            recording = await http.get(
                f"/v1/admin/calls/{lease.call_id}/recording",
                headers={"authorization": f"Bearer {_RESULT_CURRENT}"},
            )
            deliveries = await http.get(
                "/v1/admin/deliveries?undelivered_only=true",
                headers={"authorization": f"Bearer {_RESULT_CURRENT}"},
            )

    assert unauthorized.status_code == 403
    assert unauthorized.json()["error"]["code"] == "VK-WEB-004"
    assert previous.status_code == 200
    assert previous.content == b"audio-bytes"
    assert previous.headers["cache-control"] == "private, no-store"
    assert calls.status_code == 200
    assert calls.json()["calls"][0]["call_id"] == lease.call_id
    assert result.status_code == 200
    assert call.json()["call"]["call_id"] == lease.call_id
    assert recording.json()["recording"]["recording_id"] == pending.recording_id
    assert deliveries.json()["deliveries"]


@pytest.mark.asyncio
async def test_companion_recovers_terminal_provider_observation_and_delivers(
    tmp_path: Path,
) -> None:
    started = datetime.now(UTC) - timedelta(seconds=10)
    delivered: list[httpx.Request] = []

    async def receiver(request: httpx.Request) -> httpx.Response:
        delivered.append(request)
        return httpx.Response(204)

    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        lease = await repository.begin_call(
            _call("call_recovery", started_at=started),
            owner_id="dead-worker",
            delivery=_delivery(),
            lease_ttl=timedelta(seconds=1),
            now=started,
        )
        await repository.record_provider_observation(lease.call_id, "completed")
        http = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
        delivery = DeliveryWorker(
            repository,
            owner_id="results-delivery",
            current_secret=_RESULT_CURRENT,
            client=http,
        )
        maintenance = CompanionMaintenance(
            repository,
            LocalArtifactStore(tmp_path / "artifacts"),
            owner_id="results-test",
            current_result_secret=_RESULT_CURRENT,
            delivery=delivery,
        )
        report = await maintenance.run_once()
        terminal = await repository.get_terminal_event_for_call(lease.call_id)
        await maintenance.close()
        owned = CompanionMaintenance(
            repository,
            LocalArtifactStore(tmp_path / "owned-artifacts"),
            owner_id="results-owned",
            current_result_secret=_RESULT_CURRENT,
        )
        await owned.close()
        await http.aclose()

    assert report.recovery.stale == 1
    assert report.recovery.terminalized == 1
    assert report.delivery.delivered == 1
    assert terminal.event_type == "call.completed"
    assert len(delivered) == 1
