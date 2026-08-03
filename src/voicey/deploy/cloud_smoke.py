"""LiveKit Cloud room-dispatch smoke with durable relay evidence."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import timedelta
from typing import Any, Protocol

from voicey.config.models import Agent
from voicey.errors import VoiceyError
from voicey.obs.records import CallRecord, NewCall, TimelineEvent
from voicey.relay.auth import RelayCredential
from voicey.relay.client import RelayClient
from voicey.storage.models import ResultDeliveryConfig, TerminalRequest
from voicey.telephony.models import validate_e164


class LiveKitApiFactory(Protocol):
    """Construct the installed async LiveKit API client."""

    def __call__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
    ) -> Any: ...


class SmokeRelay(Protocol):
    """Relay surface needed to prove cloud dispatch and termination."""

    async def open(self) -> SmokeRelay: ...

    async def close(self) -> None: ...

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
    ) -> Any: ...

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None: ...

    async def get_call(self, call_id: str) -> CallRecord: ...

    async def terminalize(self, lease: Any, request: TerminalRequest) -> Any: ...


class LiveKitCloudSessionSmoke:
    """Dispatch a named cloud agent and verify relay claim plus terminal event."""

    def __init__(
        self,
        *,
        api_factory: LiveKitApiFactory | None = None,
        relay_client_factory: (Callable[[str, RelayCredential], SmokeRelay] | None) = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        claim_timeout_s: float = 90,
        terminal_timeout_s: float = 120,
        poll_interval_s: float = 2,
    ) -> None:
        if claim_timeout_s <= 0 or terminal_timeout_s <= 0 or poll_interval_s < 0:
            raise VoiceyError("VY-DEP-008", detail="cloud smoke timeouts are invalid.")
        self._api_factory = api_factory or _livekit_api
        self._relay_client_factory = relay_client_factory or RelayClient
        self._sleep = sleep
        self._claim_timeout_s = claim_timeout_s
        self._terminal_timeout_s = terminal_timeout_s
        self._poll_interval_s = poll_interval_s

    async def run(
        self,
        *,
        agent: Agent,
        relay_url: str,
        relay_credential: RelayCredential,
        environment: Mapping[str, str],
        to_number: str | None = None,
    ) -> bool:
        """Run a paid cloud room smoke only after the caller confirms deployment."""
        from livekit import api

        url = _required(environment, "LIVEKIT_URL")
        api_key = _required(environment, "LIVEKIT_API_KEY")
        api_secret = _required(environment, "LIVEKIT_API_SECRET")
        call_id = f"call_lk_cloud_smoke_{uuid.uuid4().hex}"
        room_name = f"vy-smoke-{uuid.uuid4().hex}"
        reservation_owner = f"livekit_reservation_{call_id}"
        phone_smoke = to_number is not None
        trunk_id = (
            _required(environment, "VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID") if phone_smoke else None
        )
        destination = validate_e164(to_number) if to_number is not None else None
        relay = self._relay_client_factory(relay_url, relay_credential)
        client = self._api_factory(url=url, api_key=api_key, api_secret=api_secret)
        lease: Any | None = None
        room_created = False
        terminal = False
        try:
            await relay.open()
            if not phone_smoke:
                lease = await relay.begin_call(
                    NewCall(
                        call_id=call_id,
                        agent_name=agent.name,
                        runtime="livekit",
                        channel="web",
                        direction="inbound",
                        provider="livekit-cloud-smoke",
                        config_hash=agent.config_hash,
                    ),
                    owner_id=reservation_owner,
                    delivery=ResultDeliveryConfig(
                        endpoint=agent.results.webhook,
                        include=tuple(agent.results.include),
                        redact=tuple(agent.results.redact),
                        purge_after_days=agent.results.purge_after_days,
                        recording_enabled=False,
                    ),
                    lease_ttl=timedelta(minutes=5),
                )
                await relay.append_timeline(
                    call_id,
                    TimelineEvent(event_type="runtime.reserved"),
                )
            metadata = json.dumps(
                {
                    "call_id": call_id,
                    "channel": "phone" if phone_smoke else "web",
                    "direction": "outbound" if phone_smoke else "inbound",
                    "provider": "livekit-sip" if phone_smoke else "livekit-cloud-smoke",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            await client.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=300,
                    agents=[
                        api.RoomAgentDispatch(
                            agent_name=agent.name,
                            metadata=metadata,
                        )
                    ],
                )
            )
            room_created = True
            if phone_smoke:
                await client.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=trunk_id,
                        sip_call_to=destination,
                        room_name=room_name,
                        participant_identity=f"voicey-smoke-{uuid.uuid4().hex}",
                        participant_name="voicey cloud smoke",
                        wait_until_answered=True,
                    )
                )
            await self._wait_for(
                relay,
                call_id,
                timeout_s=self._claim_timeout_s,
                predicate=lambda record: any(
                    event.event_type == "runtime.admitted" for event in record.timeline
                ),
                failure="LiveKit Cloud did not dispatch the named agent.",
            )
            await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            room_created = False
            await self._wait_for(
                relay,
                call_id,
                timeout_s=self._terminal_timeout_s,
                predicate=lambda record: record.ended_at is not None,
                failure="LiveKit Cloud room close produced no terminal relay event.",
            )
            terminal = True
            return True
        finally:
            if room_created:
                with suppress(Exception):
                    await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            if lease is not None and not terminal:
                await _terminalize_unclaimed(relay, call_id, lease)
            with suppress(Exception):
                await client.aclose()
            await relay.close()

    async def _wait_for(
        self,
        relay: SmokeRelay,
        call_id: str,
        *,
        timeout_s: float,
        predicate: Callable[[CallRecord], bool],
        failure: str,
    ) -> CallRecord:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            try:
                record = await relay.get_call(call_id)
            except VoiceyError as exc:
                if exc.code != "VY-OBS-003":
                    raise
                record = None
            if record is not None and predicate(record):
                return record
            if asyncio.get_running_loop().time() >= deadline:
                raise VoiceyError("VY-DEP-004", detail=failure)
            await self._sleep(self._poll_interval_s)


def _livekit_api(*, url: str, api_key: str, api_secret: str) -> Any:
    from livekit import api

    return api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)


async def _terminalize_unclaimed(
    relay: SmokeRelay,
    call_id: str,
    lease: Any,
) -> None:
    try:
        record = await relay.get_call(call_id)
        if record.ended_at is None:
            await relay.terminalize(
                lease,
                TerminalRequest(
                    event_type="call.failed",
                    ended_reason="setup_error",
                ),
            )
    except Exception:
        return


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise VoiceyError("VY-DEP-008", detail=f"LiveKit Cloud smoke requires {name}.")
    return value
