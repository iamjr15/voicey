import asyncio
import os
import socket
from contextlib import closing

import pytest
import uvicorn
from fastapi import FastAPI

from voicey.tunnel import TunnelManager, TunnelProbe

pytestmark = pytest.mark.live


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


async def test_cloudflared_quick_tunnel_websocket_round_trip() -> None:
    if os.environ.get("VOICEY_LIVE_TUNNEL_CONFIRM") != "I_ACKNOWLEDGE_PUBLIC_TUNNEL":
        pytest.skip("VOICEY_LIVE_TUNNEL_CONFIRM acknowledgement is absent")

    port = _free_port()
    app = FastAPI()
    probe = TunnelProbe()
    probe.install(app)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve(), name="voicey-live-tunnel-origin")
    handle = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        handle = await TunnelManager(environment={}).open(
            port,
            preference="cloudflared",
            startup_timeout_s=45,
        )
        await probe.verify(handle.public_url, timeout_s=60)
    finally:
        if handle is not None:
            await handle.close()
        server.should_exit = True
        await server_task
