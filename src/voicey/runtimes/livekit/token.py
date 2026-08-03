"""Short-lived LiveKit participant tokens with explicit agent dispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from livekit import api
from livekit.protocol import agent_dispatch as lk_dispatch
from livekit.protocol import room as lk_room

from voicey.errors import VoiceyError


@dataclass(frozen=True, slots=True)
class LiveKitToken:
    server_url: str
    participant_token: str
    room_name: str
    participant_identity: str


class LiveKitTokenIssuer:
    """Mint least-privilege room tokens for one configured native agent."""

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        api_secret: str,
        agent_name: str,
        ttl_s: int = 300,
    ) -> None:
        if not server_url.startswith(("ws://", "wss://")):
            raise VoiceyError(
                "VY-RUN-002",
                detail="LiveKit server URL must use ws:// or wss://.",
            )
        if not api_key or not api_secret:
            raise VoiceyError(
                "VY-RUN-002",
                detail="LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required.",
            )
        if not 30 <= ttl_s <= 3600:
            raise VoiceyError(
                "VY-RUN-002",
                detail="LiveKit participant token TTL must be 30-3600 seconds.",
            )
        self.server_url = server_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.agent_name = agent_name
        self.ttl = timedelta(seconds=ttl_s)

    def issue(
        self,
        *,
        call_id: str,
        room_name: str,
        participant_identity: str,
        metadata: dict[str, str],
    ) -> LiveKitToken:
        dispatch_metadata = json.dumps(
            {"call_id": call_id, **metadata},
            sort_keys=True,
            separators=(",", ":"),
        )
        token = (
            api.AccessToken(self.api_key, self.api_secret)
            .with_identity(participant_identity)
            .with_metadata(dispatch_metadata)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                    can_update_own_metadata=False,
                )
            )
            .with_room_config(
                lk_room.RoomConfiguration(
                    agents=[
                        lk_dispatch.RoomAgentDispatch(
                            agent_name=self.agent_name,
                            metadata=dispatch_metadata,
                        )
                    ]
                )
            )
            .with_ttl(self.ttl)
            .to_jwt()
        )
        return LiveKitToken(
            server_url=self.server_url,
            participant_token=token,
            room_name=room_name,
            participant_identity=participant_identity,
        )
