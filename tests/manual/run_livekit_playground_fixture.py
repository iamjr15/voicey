"""Serve the embedded LiveKit playground path without external credentials."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from voicey import Agent, Models, Results, Web, tool
from voicey.obs import NewCall
from voicey.playground import (
    OriginPolicy,
    PlaygroundService,
    PlaygroundSettings,
    SessionTokenManager,
    WebSessionSecurity,
)
from voicey.results.signing import encode_secret
from voicey.storage import ResultDeliveryConfig, SQLiteRepository


@tool
def fixture_lookup(query: str) -> dict[str, str]:
    """Return a fixed visual-review result."""
    return {"query": query, "status": "fixture"}


agent = Agent(
    name="livekit-appointment-concierge",
    runtime="livekit",
    models=Models(
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
    ),
    persona="Help callers manage appointments.",
    flow="tests.fixtures.agent:entry",
    tools=[fixture_lookup],
    web=Web(enabled=True, allowed_origins=["http://127.0.0.1:7871"]),
    results=Results(
        webhook="https://receiver.example/results",
        secret_env="RESULT_SECRET",  # pragma: allowlist secret
    ),
)
public = FastAPI()
repository = SQLiteRepository(Path(".voicey/manual-livekit-playground.sqlite3"))
tokens = SessionTokenManager(
    encode_secret(b"manual-livekit-playground-fixture"),
    audience="http://127.0.0.1:7870",
    agent_name=agent.name,
)
security = WebSessionSecurity(
    tokens,
    OriginPolicy(
        allowed_origins=frozenset({"http://127.0.0.1:7871"}),
        expected_public_origin="http://127.0.0.1:7870",
    ),
)
repository_open = False


@dataclass(frozen=True, slots=True)
class FixtureRoomToken:
    server_url: str
    participant_token: str
    room_name: str
    participant_identity: str


class FixtureRoomTokenIssuer:
    def issue(
        self,
        *,
        call_id: str,
        room_name: str,
        participant_identity: str,
        metadata: dict[str, str],
    ) -> FixtureRoomToken:
        del call_id, metadata
        return FixtureRoomToken(
            server_url="ws://127.0.0.1:7999",
            participant_token="fixture-livekit-token",
            room_name=room_name,
            participant_identity=participant_identity,
        )


async def reserve_web_call() -> str:
    """Create a real durable LiveKit fixture row before returning its token."""
    global repository_open
    if not repository_open:
        await repository.open()
        repository_open = True
    call_id = f"call_web_{uuid.uuid4().hex}"
    await repository.begin_call(
        NewCall(
            call_id=call_id,
            agent_name=agent.name,
            runtime="livekit",
            channel="web",
            direction="inbound",
            config_hash=agent.config_hash,
            started_at=datetime.now(UTC),
        ),
        owner_id=f"livekit_reservation_{call_id}",
        delivery=ResultDeliveryConfig(endpoint=agent.results.webhook),
        lease_ttl=timedelta(minutes=5),
    )
    return call_id


async def cancel_web_call(_call_id: str) -> None:
    return


service = PlaygroundService(
    agent=agent,
    public_app=public,
    repository=repository,
    tokens=tokens,
    settings=PlaygroundSettings(
        admin_origin="http://127.0.0.1:7871",
        public_origin="http://127.0.0.1:7870",
        frontend_dir=Path("src/voicey/_frontend").resolve(),
        connect_origins=("ws://127.0.0.1:7999",),
    ),
    reserve_web_call=reserve_web_call,
    reload_status=lambda: {
        "revision": 4,
        "state": "ready",
        "message": "LiveKit visual fixture loaded",
    },
    public_security=security,
    room_token_issuer=FixtureRoomTokenIssuer(),
    cancel_web_call=cancel_web_call,
)


async def main() -> None:
    public_server = uvicorn.Server(
        uvicorn.Config(public, host="127.0.0.1", port=7870, log_level="warning")
    )
    admin_server = uvicorn.Server(
        uvicorn.Config(service.admin_app, host="127.0.0.1", port=7871, log_level="warning")
    )
    await asyncio.gather(public_server.serve(), admin_server.serve())


if __name__ == "__main__":
    asyncio.run(main())
