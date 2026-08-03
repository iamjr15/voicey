from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from voicey.errors import VoiceyError
from voicey.relay.cloud_answer import (
    add_pipecat_cloud_answer_routes,
    pipecat_cloud_answer_path,
    pipecat_cloud_answer_xml,
    pipecat_cloud_websocket_url,
)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("twilio", "_pipecatCloudServiceHost"),
        ("telnyx", 'bidirectionalMode="rtp"'),
        ("vobiz", "audio/x-mulaw;rate=8000"),
        ("plivo", "audio/x-mulaw;rate=8000"),
    ],
)
def test_pipecat_cloud_answer_xml_matches_provider_contract(
    provider: str,
    expected: str,
) -> None:
    xml = pipecat_cloud_answer_xml(
        region="eu-central",
        organization="voicey-org",
        agent_name="voicey-agent",
        provider=provider,  # type: ignore[arg-type]
    )
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert expected in xml
    assert "voicey-agent.voicey-org" in xml
    assert "wss://eu-central.api.pipecat.daily.co/ws/" in xml
    assert ("ws/plivo" in xml) is (provider in {"vobiz", "plivo"})


def test_us_west_cloud_answer_uses_default_endpoint_and_safe_path() -> None:
    path = pipecat_cloud_answer_path(
        region="us-west",
        organization="voicey-org",
        agent_name="voicey-agent",
        provider="twilio",
    )
    xml = pipecat_cloud_answer_xml(
        region="us-west",
        organization="voicey-org",
        agent_name="voicey-agent",
        provider="twilio",
    )
    assert path == ("/v1/pipecat-cloud/us-west/voicey-org/voicey-agent/twilio/answer")
    assert "wss://api.pipecat.daily.co/ws/twilio" in xml
    assert "us-west.api" not in xml
    assert (
        pipecat_cloud_websocket_url(
            region="us-west",
            organization="voicey-org",
            agent_name="voicey-agent",
            provider="telnyx",
        )
        == "wss://api.pipecat.daily.co/ws/telnyx"
        "?serviceHost=voicey-agent.voicey-org"
    )


@pytest.mark.asyncio
async def test_cloud_answer_route_supports_provider_get_and_post() -> None:
    app = FastAPI()
    add_pipecat_cloud_answer_routes(app)
    path = "/v1/pipecat-cloud/us-west/voicey-org/voicey-agent/telnyx/answer"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://results.example.test",
    ) as client:
        get_response = await client.get(path)
        post_response = await client.post(path)

    assert get_response.status_code == 200
    assert get_response.headers["content-type"].startswith("application/xml")
    assert get_response.headers["cache-control"] == "no-store"
    assert get_response.text == post_response.text


def test_cloud_answer_rejects_untrusted_path_values() -> None:
    with pytest.raises(VoiceyError, match="identity is invalid"):
        pipecat_cloud_answer_path(
            region="us-west",
            organization="Bad.Org",
            agent_name="voicey-agent",
            provider="twilio",
        )
    with pytest.raises(VoiceyError, match="provider"):
        pipecat_cloud_answer_xml(
            region="us-west",
            organization="voicey-org",
            agent_name="voicey-agent",
            provider="unknown",  # type: ignore[arg-type]
        )
