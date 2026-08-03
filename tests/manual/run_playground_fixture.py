"""Serve the embedded playground for manual visual/accessibility review."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from voicey import Agent, Models, Results, Web, tool
from voicey.obs import NewCall
from voicey.playground import PlaygroundService, PlaygroundSettings, SessionTokenManager
from voicey.results.signing import encode_secret
from voicey.storage import ResultDeliveryConfig, SQLiteRepository


@tool
def fixture_lookup(query: str) -> dict[str, str]:
    """Return a fixed visual-review result."""
    return {"query": query, "status": "fixture"}


agent = Agent(
    name="appointment-concierge",
    runtime="pipecat",
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
repository = SQLiteRepository(Path(".voicey/manual-playground.sqlite3"))
tokens = SessionTokenManager(
    encode_secret(b"manual-playground-fixture-secret"),
    audience="http://127.0.0.1:7870",
    agent_name=agent.name,
)
repository_open = False


async def reserve_web_call() -> str:
    """Create a real durable fixture row before returning its browser token."""
    global repository_open
    if not repository_open:
        await repository.open()
        repository_open = True
    call_id = f"call_web_fixture_{uuid.uuid4().hex}"
    await repository.begin_call(
        NewCall(
            call_id=call_id,
            agent_name=agent.name,
            runtime="pipecat",
            channel="web",
            direction="inbound",
            config_hash=agent.config_hash,
            started_at=datetime.now(UTC),
        ),
        owner_id="manual-playground-fixture",
        delivery=ResultDeliveryConfig(endpoint=agent.results.webhook),
        lease_ttl=timedelta(minutes=5),
    )
    return call_id


service = PlaygroundService(
    agent=agent,
    public_app=public,
    repository=repository,
    tokens=tokens,
    settings=PlaygroundSettings(
        admin_origin="http://127.0.0.1:7871",
        public_origin="http://127.0.0.1:7870",
        frontend_dir=Path("src/voicey/_frontend").resolve(),
    ),
    reserve_web_call=reserve_web_call,
    reload_status=lambda: {
        "revision": 3,
        "state": "ready",
        "message": "visual fixture loaded",
    },
)


if __name__ == "__main__":
    uvicorn.run(
        service.admin_app,
        host="127.0.0.1",
        port=7871,
        log_level="warning",
        proxy_headers=False,
    )
