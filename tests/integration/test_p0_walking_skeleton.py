import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from voicekit._p0.common import RuntimeProbe
from voicekit._p0.livekit_probe import run_livekit_probe
from voicekit._p0.pipecat_probe import run_pipecat_probe
from voicekit.results import WebhookSigner

pytestmark = pytest.mark.integration
ProbeRunner = Callable[[], Awaitable[RuntimeProbe]]


@pytest.mark.parametrize(
    "probe_runner",
    [run_pipecat_probe, run_livekit_probe],
    ids=["pipecat", "livekit"],
)
async def test_p0_runtime_walking_skeleton(probe_runner: ProbeRunner) -> None:
    probe = await probe_runner()

    assert probe.native_bootstrap
    assert probe.native_tool_name == "record_slot"
    assert probe.tool_result == "recorded:2030-01-02T10:00:00Z"
    assert probe.results["outcome"] == "p0_proven"
    assert probe.results["data"] == {"slot": "2030-01-02T10:00:00Z"}
    assert probe.browser.connected
    assert probe.browser.session_id
    assert probe.phone_termination.reason == "provider_mock_completed"
    assert probe.phone_termination_count == 1

    delivered = await _deliver_to_verifying_receiver(probe)

    assert delivered["event"] == "call.completed"
    assert delivered["agent"]["runtime"] == probe.runtime
    assert delivered["data"] == probe.results["data"]


async def _deliver_to_verifying_receiver(probe: RuntimeProbe) -> dict[str, Any]:
    app = FastAPI()
    received: dict[str, Any] = {}

    @app.post("/results")
    async def receive(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        body = await request.body()
        WebhookSigner(probe.webhook_secret).verify(
            dict(request.headers),
            body,
            now=1_750_000_001,
        )
        received.update(cast(dict[str, Any], json.loads(body)))
        return Response(status_code=204)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://receiver.invalid",
    ) as client:
        response = await client.post(
            "/results",
            content=probe.signed_webhook.body,
            headers=probe.signed_webhook.headers,
        )

    assert response.status_code == 204
    return received
