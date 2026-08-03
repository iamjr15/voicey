"""LiveKit Agents 1.6.7 walking skeleton against installed current APIs."""

from __future__ import annotations

from typing import Any

from livekit import api
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    function_tool,
)

from voicey import results, tool
from voicey._p0.common import (
    BrowserEvidence,
    MockPhoneProvider,
    RuntimeProbe,
    finalize_probe,
)
from voicey.tools import get_tool_metadata


async def run_livekit_probe() -> RuntimeProbe:
    """Run native AgentServer/session/tool and browser-token paths."""
    call_id = "call_p0_livekit"
    buffer = results.CallResultBuffer(call_id=call_id)
    phone = MockPhoneProvider()
    server = AgentServer()

    @server.rtc_session(agent_name="voicey-p0")
    async def session_entrypoint(  # pyright: ignore[reportUnusedFunction]
        _job_context: JobContext,
    ) -> None:
        return None

    @tool(say_while_running="I am checking the P0 slot.")
    async def record_slot(slot: str) -> str:
        """Record the selected P0 appointment slot."""
        results.set("slot", slot)
        return f"recorded:{slot}"

    native_tool = function_tool(record_slot)
    session = AgentSession[Any](
        tools=[native_tool],
        turn_handling=TurnHandlingOptions(
            endpointing={"min_delay": 0.3, "max_delay": 3.0},
            interruption={"enabled": True},
        ),
    )
    try:
        with results.result_context(buffer):
            tool_result = await native_tool(slot="2030-01-02T10:00:00Z")
            results.set_outcome("p0_proven")
            await phone.terminate(call_id, "provider_mock_completed")
    finally:
        await session.aclose()

    token = _issue_browser_token()
    return finalize_probe(
        runtime="livekit",
        native_bootstrap=f"{type(server).__name__}+{type(session).__name__}",
        native_tool_name=get_tool_metadata(record_slot).name,
        tool_result=tool_result,
        buffer=buffer,
        browser=BrowserEvidence(session_id=token, connected=token.count(".") == 2),
        phone=phone,
    )


def _issue_browser_token() -> str:
    return (
        api.AccessToken(
            "voicey-p0-key",
            "voicey-p0-development-secret-at-least-32-bytes",
        )
        .with_identity("p0-browser")
        .with_grants(api.VideoGrants(room_join=True, room="voicey-p0-room"))
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="voicey-p0")])
        )
        .to_jwt()
    )
