from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from voicey import Agent, Models, Results, Web
from voicey.deploy.cloud_smoke import LiveKitCloudSessionSmoke
from voicey.errors import VoiceyError
from voicey.obs.records import CallRecord, NewCall, TimelineEvent
from voicey.relay.auth import RelayCredential
from voicey.storage.models import ResultDeliveryConfig, TerminalRequest


class FakeRoomService:
    def __init__(self, relay: FakeSmokeRelay) -> None:
        self.relay = relay
        self.created: object | None = None
        self.deleted: object | None = None

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
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.claimed = False
        self.ended = False
        self.terminalized = 0
        self.call: NewCall | None = None
        self.dispatched_call_id: str | None = None
        self.timeline: list[TimelineEvent] = []

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
            status="completed" if self.ended else "active",
            webhook_status="pending" if self.ended else "not_ready",
            started_at=now,
            updated_at=now,
            ended_at=now if self.ended else None,
            terminal_reason="caller_hangup" if self.ended else None,
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


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_proves_dispatch_and_terminal_event() -> None:
    relay = FakeSmokeRelay()
    api_client = FakeApi(relay)
    factory_arguments: dict[str, str] = {}

    def api_factory(*, url: str, api_key: str, api_secret: str) -> FakeApi:
        factory_arguments.update(url=url, api_key=api_key, api_secret=api_secret)
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
    assert factory_arguments == {
        "url": "wss://voicey.livekit.cloud",
        "api_key": "api-key",
        "api_secret": "api-secret",
    }


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


@pytest.mark.asyncio
async def test_livekit_cloud_smoke_timeout_deletes_room_and_terminalizes_reservation() -> None:
    relay = FakeSmokeRelay()
    api_client = FakeApi(relay)

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


def test_livekit_cloud_smoke_rejects_invalid_timeouts() -> None:
    with pytest.raises(VoiceyError, match="VY-DEP-008"):
        LiveKitCloudSessionSmoke(claim_timeout_s=0)
