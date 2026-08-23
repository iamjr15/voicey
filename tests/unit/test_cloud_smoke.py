# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from voicey import Agent, Models, Results, Web
from voicey.deploy import cloud_smoke
from voicey.deploy.cloud_smoke import (
    LiveKitCloudSessionSmoke,
    PipecatCloudSessionSmoke,
    PipecatDailySession,
)
from voicey.errors import VoiceyError
from voicey.obs.records import CallRecord, NewCall, TimelineEvent
from voicey.relay.auth import RelayCredential
from voicey.storage.models import ResultDeliveryConfig, TerminalRequest


class FakeRoomService:
    def __init__(self, relay: FakeSmokeRelay) -> None:
        self.relay = relay
        self.created: object | None = None
        self.deleted: object | None = None
        self.removed: object | None = None
        self.room_already_gone = False

    async def create_room(self, request: object) -> object:
        self.created = request
        agents = list(cast(Any, request).agents)
        metadata = json.loads(agents[0].metadata)
        if self.relay.call is not None:
            assert metadata["call_id"] == self.relay.call.call_id
        self.relay.dispatched_call_id = metadata["call_id"]
        assert agents[0].agent_name == "voicey-agent"
        self.relay.claimed = True
        return object()

    async def delete_room(self, request: object) -> object:
        self.deleted = request
        if self.room_already_gone:
            from livekit.api.twirp_client import ServerError

            raise ServerError("not_found", "requested room does not exist", status=404)
        return object()

    async def remove_participant(self, request: object) -> object:
        self.removed = request
        if self.relay.claimed:
            self.relay.ended = True
        return object()


class FakeApi:
    def __init__(self, relay: FakeSmokeRelay) -> None:
        self.room = FakeRoomService(relay)
        self.sip = FakeSipService()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeSmokeRelay:
    def __init__(self, *, failed: bool = False, session_started: bool = True) -> None:
        self.opened = False
        self.closed = False
        self.claimed = False
        self.ended = False
        self.terminalized = 0
        self.call: NewCall | None = None
        self.dispatched_call_id: str | None = None
        self.timeline: list[TimelineEvent] = []
        self.failed = failed
        self.session_started = session_started

    async def open(self) -> FakeSmokeRelay:
        self.opened = True
        return self

    async def close(self) -> None:
        self.closed = True

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
    ) -> object:
        del owner_id, delivery, lease_ttl
        self.call = call
        return object()

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None:
        del call_id
        self.timeline.append(event)

    async def get_call(self, call_id: str) -> CallRecord:
        del call_id
        timeline = [*self.timeline]
        if self.claimed:
            timeline.append(TimelineEvent(event_type="runtime.admitted"))
            if self.session_started:
                timeline.append(TimelineEvent(event_type="runtime.session_started"))
        now = datetime.now(UTC)
        stored = self.call
        stored_call_id = (
            stored.call_id if stored is not None else cast(str, self.dispatched_call_id)
        )
        return CallRecord(
            call_id=stored_call_id,
            agent_name="voicey-agent" if stored is None else stored.agent_name,
            runtime="livekit",
            channel="web",
            direction="inbound",
            provider="livekit-cloud-smoke",
            provider_call_id=None,
            from_number=None,
            to_number=None,
            config_hash=_agent().config_hash if stored is None else stored.config_hash,
            status=("failed" if self.failed else "completed") if self.ended else "active",
            webhook_status="pending" if self.ended else "not_ready",
            started_at=now,
            updated_at=now,
            ended_at=now if self.ended else None,
            terminal_reason=("setup_error" if self.failed else "caller_hangup")
            if self.ended
            else None,
            timeline=tuple(timeline),
            transcript=(),
            tool_calls=(),
            latency=(),
        )

    async def terminalize(self, lease: Any, request: TerminalRequest) -> object:
        del lease, request
        self.terminalized += 1
        self.ended = True
        return object()


class FakeParticipant:
    def __init__(self, relay: FakeSmokeRelay) -> None:
        self.relay = relay
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True
        if self.relay.claimed:
            self.relay.ended = True


class FakeParticipantConnector:
    def __init__(self, relay: FakeSmokeRelay) -> None:
        self.relay = relay
        self.participant = FakeParticipant(relay)
        self.room_name: str | None = None

    async def __call__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        room_name: str,
    ) -> FakeParticipant:
        assert url == "wss://voicey.livekit.cloud"
        assert api_key == "api-key"
        assert api_secret == "api-secret"
        self.room_name = room_name
        return self.participant


class FakePipecatSmokeRelay(FakeSmokeRelay):
    def __init__(self, *, failed: bool = False, terminal_before_flow: bool = False) -> None:
        super().__init__(failed=failed)
        self.connected = False
        self.terminal_before_flow = terminal_before_flow
        self.dispatched_call_id = "pcc_session_123"

    async def get_call(self, call_id: str) -> CallRecord:
        assert call_id == "pcc_session_123"
        self.claimed = True
        if self.terminal_before_flow and self.connected:
            self.ended = True
        record = await super().get_call(call_id)
        timeline = [*record.timeline]
        if self.connected and not self.terminal_before_flow:
            timeline.append(TimelineEvent(event_type="runtime.flow_initialized"))
        return record.model_copy(
            update={
                "runtime": "pipecat",
                "provider": "pipecat-cloud-smoke",
                "timeline": tuple(timeline),
            }
        )


class FakePipecatParticipantConnector:
    def __init__(self, relay: FakePipecatSmokeRelay) -> None:
        self.relay = relay
        self.participant = FakeParticipant(relay)
        self.room_url: str | None = None
        self.room_token: str | None = None

    async def __call__(
        self,
        *,
        room_url: str,
        room_token: str,
    ) -> FakeParticipant:
        self.room_url = room_url
        self.room_token = room_token
        self.relay.connected = True
        return self.participant


class FakeSipService:
    def __init__(self) -> None:
        self.created: object | None = None

    async def create_sip_participant(self, request: object) -> object:
        self.created = request
        return object()


def _agent() -> Agent:
    return Agent(
        name="voicey-agent",
        runtime="livekit",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Test cloud session smoke.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["https://app.example.test"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEY_WEBHOOK_SECRET",
        ),
    )


def _pipecat_session() -> PipecatDailySession:
    return PipecatDailySession(
        session_id="session_123",
        room_url="https://daily.example.test/smoke-room",
        room_token="daily-secret-token",
    )


@pytest.mark.asyncio
async def test_pipecat_cloud_smoke_proves_flow_and_graceful_terminal() -> None:
    relay = FakePipecatSmokeRelay()
    connector = FakePipecatParticipantConnector(relay)

    def relay_factory(_url: str, _credential: RelayCredential) -> FakePipecatSmokeRelay:
        return relay

    smoke = PipecatCloudSessionSmoke(
        relay_client_factory=relay_factory,
        participant_connector=connector,
        poll_interval_s=0,
    )
    assert await smoke.run(
        session=_pipecat_session(),
        relay_url="https://relay.example.test",
        relay_credential=RelayCredential.issue("smoke-key"),
    )
    assert relay.opened
    assert relay.closed
    assert relay.connected
    assert relay.ended
    assert connector.participant.disconnected
    assert connector.room_url == "https://daily.example.test/smoke-room"
    assert connector.room_token == "daily-secret-token"
    assert "daily-secret-token" not in repr(_pipecat_session())


@pytest.mark.asyncio
async def test_pipecat_cloud_smoke_rejects_failed_terminal() -> None:
    relay = FakePipecatSmokeRelay(failed=True)
    connector = FakePipecatParticipantConnector(relay)
    smoke = PipecatCloudSessionSmoke(
        relay_client_factory=lambda _url, _credential: relay,
        participant_connector=connector,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="without a completed call"):
        await smoke.run(
            session=_pipecat_session(),
            relay_url="https://relay.example.test",
            relay_credential=RelayCredential.issue("smoke-key"),
        )
    assert relay.closed


@pytest.mark.asyncio
async def test_pipecat_cloud_smoke_rejects_terminal_before_flow_start() -> None:
    relay = FakePipecatSmokeRelay(terminal_before_flow=True)
    connector = FakePipecatParticipantConnector(relay)
    smoke = PipecatCloudSessionSmoke(
        relay_client_factory=lambda _url, _credential: relay,
        participant_connector=connector,
        claim_timeout_s=1,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="did not initialize"):
        await smoke.run(
            session=_pipecat_session(),
            relay_url="https://relay.example.test",
            relay_credential=RelayCredential.issue("smoke-key"),
        )
    assert connector.participant.disconnected
    assert relay.closed


@pytest.mark.asyncio
async def test_pipecat_daily_connector_is_nonpublishing_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[Any] = []
    initialized = 0

    class Daily:
        @staticmethod
        def init() -> None:
            nonlocal initialized
            initialized += 1

    class CallClient:
        def __init__(self) -> None:
            self.settings: dict[str, Any] | None = None
            self.released = False
            clients.append(self)

        def join(
            self,
            _room_url: str,
            _room_token: str,
            *,
            client_settings: dict[str, Any],
            completion: Any,
        ) -> None:
            self.settings = client_settings
            completion({"participants": {}}, None)

        def leave(self, *, completion: Any) -> None:
            completion(None)

        def release(self) -> None:
            self.released = True

    monkeypatch.setitem(sys.modules, "daily", SimpleNamespace(CallClient=CallClient, Daily=Daily))
    monkeypatch.setattr(cloud_smoke, "_daily_initialized", False)
    participant = await cloud_smoke._connect_pipecat_smoke_participant(
        room_url="https://daily.example.test/smoke-room",
        room_token="secret-token",
    )
    assert initialized == 1
    assert clients[0].settings == {
        "inputs": {
            "camera": {"isEnabled": False},
            "microphone": {"isEnabled": False},
        },
        "publishing": {
            "camera": {"isPublishing": False},
            "microphone": {"isPublishing": False},
        },
    }
    await participant.disconnect()
    await participant.disconnect()
    assert clients[0].released


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_proves_dispatch_and_terminal_event() -> None:
    relay = FakeSmokeRelay()
    api_client = FakeApi(relay)
    connector = FakeParticipantConnector(relay)
    factory_arguments: dict[str, str] = {}

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        factory_arguments.update(url=url, api_key=api_key, api_secret=api_secret)
        return api_client

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
        participant_connector=connector,
        poll_interval_s=0,
    )
    result = await smoke.run(
        agent=_agent(),
        relay_url="https://relay.example.test",
        relay_credential=RelayCredential.issue("smoke-key"),
        environment={
            "LIVEKIT_URL": "wss://voicey.livekit.cloud",
            "LIVEKIT_API_KEY": "api-key",
            "LIVEKIT_API_SECRET": "api-secret",
        },
    )

    assert result
    assert relay.opened
    assert relay.closed
    assert relay.claimed
    assert relay.ended
    assert relay.terminalized == 0
    assert api_client.closed
    assert api_client.room.created is not None
    assert api_client.room.deleted is not None
    assert connector.participant.disconnected
    assert connector.room_name is not None
    assert factory_arguments == {
        "url": "wss://voicey.livekit.cloud",
        "api_key": "api-key",
        "api_secret": "api-secret",
    }


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_accepts_room_auto_deleted_after_disconnect() -> None:
    relay = FakeSmokeRelay()
    api_client = FakeApi(relay)
    api_client.room.room_already_gone = True
    connector = FakeParticipantConnector(relay)

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        del url, api_key, api_secret
        return api_client

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
        participant_connector=connector,
        poll_interval_s=0,
    )

    assert await smoke.run(
        agent=_agent(),
        relay_url="https://relay.example.com",
        relay_credential=RelayCredential.issue("smoke-key"),
        environment={
            "LIVEKIT_URL": "wss://voicey.livekit.cloud",
            "LIVEKIT_API_KEY": "api-key",
            "LIVEKIT_API_SECRET": "api-secret",
        },
    )
    assert connector.participant.disconnected
    assert api_client.room.deleted is not None


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_requires_credentials_before_mutation() -> None:
    relay = FakeSmokeRelay()

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        del url, api_key, api_secret
        return FakeApi(relay)

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
    )
    with pytest.raises(VoiceyError, match="LIVEKIT_URL"):
        await smoke.run(
            agent=_agent(),
            relay_url="https://relay.example.test",
            relay_credential=RelayCredential.issue("smoke-key"),
            environment={},
        )
    assert not relay.opened


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_rejects_failed_terminal_call() -> None:
    relay = FakeSmokeRelay(failed=True)
    api_client = FakeApi(relay)
    connector = FakeParticipantConnector(relay)

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        del url, api_key, api_secret
        return api_client

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
        participant_connector=connector,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="without a completed call"):
        await smoke.run(
            agent=_agent(),
            relay_url="https://relay.example.test",
            relay_credential=RelayCredential.issue("smoke-key"),
            environment={
                "LIVEKIT_URL": "wss://voicey.livekit.cloud",
                "LIVEKIT_API_KEY": "api-key",
                "LIVEKIT_API_SECRET": "api-secret",
            },
        )
    assert relay.terminalized == 0


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_requires_started_media_before_room_close() -> None:
    relay = FakeSmokeRelay(session_started=False)
    api_client = FakeApi(relay)
    connector = FakeParticipantConnector(relay)

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        del url, api_key, api_secret
        return api_client

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
        participant_connector=connector,
        claim_timeout_s=0.000001,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="did not start"):
        await smoke.run(
            agent=_agent(),
            relay_url="https://relay.example.test",
            relay_credential=RelayCredential.issue("smoke-key"),
            environment={
                "LIVEKIT_URL": "wss://voicey.livekit.cloud",
                "LIVEKIT_API_KEY": "api-key",
                "LIVEKIT_API_SECRET": "api-secret",
            },
        )
    assert api_client.room.deleted is not None
    assert relay.terminalized == 0
    assert relay.ended


@pytest.mark.asyncio
async def test_livekit_cloud_phone_smoke_dials_pinned_outbound_trunk() -> None:
    relay = FakeSmokeRelay()
    api_client = FakeApi(relay)

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        del url, api_key, api_secret
        return api_client

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
        poll_interval_s=0,
    )
    result = await smoke.run(
        agent=_agent(),
        relay_url="https://relay.example.test",
        relay_credential=RelayCredential.issue("smoke-key"),
        environment={
            "LIVEKIT_URL": "wss://voicey.livekit.cloud",
            "LIVEKIT_API_KEY": "api-key",
            "LIVEKIT_API_SECRET": "api-secret",
            "VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID": "ST_cloud_test",
        },
        to_number="+14155550199",
    )

    assert result
    request = cast(Any, api_client.sip.created)
    assert request.sip_trunk_id == "ST_cloud_test"
    assert request.sip_call_to == "+14155550199"
    assert request.wait_until_answered
    assert relay.call is None
    assert api_client.room.removed is not None


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_timeout_deletes_room_and_terminalizes_reservation() -> None:
    relay = FakeSmokeRelay()
    api_client = FakeApi(relay)
    connector = FakeParticipantConnector(relay)

    async def create_without_claim(request: object) -> object:
        api_client.room.created = request
        return object()

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        del url, api_key, api_secret
        return api_client

    def relay_factory(_url: str, _credential: RelayCredential) -> FakeSmokeRelay:
        return relay

    api_client.room.create_room = create_without_claim  # type: ignore[method-assign]
    smoke = LiveKitCloudSessionSmoke(
        api_factory=api_factory,
        relay_client_factory=relay_factory,
        participant_connector=connector,
        claim_timeout_s=0.000001,
        terminal_timeout_s=1,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="did not dispatch"):
        await smoke.run(
            agent=_agent(),
            relay_url="https://relay.example.test",
            relay_credential=RelayCredential.issue("smoke-key"),
            environment={
                "LIVEKIT_URL": "wss://voicey.livekit.cloud",
                "LIVEKIT_API_KEY": "api-key",
                "LIVEKIT_API_SECRET": "api-secret",
            },
        )

    assert api_client.room.deleted is not None
    assert api_client.closed
    assert relay.closed
    assert relay.terminalized == 1


def test_cloud_smokes_reject_invalid_timeouts() -> None:
    with pytest.raises(VoiceyError, match="VY-DEP-008"):
        PipecatCloudSessionSmoke(terminal_timeout_s=0)
    with pytest.raises(VoiceyError, match="VY-DEP-008"):
        LiveKitCloudSessionSmoke(claim_timeout_s=0)
