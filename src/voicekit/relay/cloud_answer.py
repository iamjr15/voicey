"""Public provider XML that connects owned numbers to Pipecat Cloud."""

from __future__ import annotations

import re
from typing import Literal, cast

from fastapi import FastAPI
from fastapi.responses import Response

from voicekit.errors import VoicekitError

PipecatCloudProvider = Literal["twilio", "telnyx", "vobiz", "plivo"]
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,53}$")
_REGION = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def add_pipecat_cloud_answer_routes(app: FastAPI) -> None:
    """Mount stable hosted-answer URLs used by carrier rollback ledgers."""

    @app.api_route(
        "/v1/pipecat-cloud/{region}/{organization}/{agent_name}/{provider}/answer",
        methods=["GET", "POST"],
        response_class=Response,
    )
    async def answer(
        region: str,
        organization: str,
        agent_name: str,
        provider: str,
    ) -> Response:
        return Response(
            content=pipecat_cloud_answer_xml(
                region=region,
                organization=organization,
                agent_name=agent_name,
                provider=_provider(provider),
            ),
            media_type="application/xml",
            headers={"cache-control": "no-store"},
        )

    _ = answer


def pipecat_cloud_answer_path(
    *,
    region: str,
    organization: str,
    agent_name: str,
    provider: PipecatCloudProvider,
) -> str:
    """Return the query-free companion path suitable for provider configuration."""
    _validate_identity(region, organization, agent_name)
    _provider(provider)
    return f"/v1/pipecat-cloud/{region}/{organization}/{agent_name}/{provider}/answer"


def pipecat_cloud_answer_xml(
    *,
    region: str,
    organization: str,
    agent_name: str,
    provider: PipecatCloudProvider,
) -> str:
    """Render the current official provider-specific cloud media handshake."""
    _validate_identity(region, organization, agent_name)
    selected = _provider(provider)
    service_host = f"{agent_name}.{organization}"
    websocket = pipecat_cloud_websocket_url(
        region=region,
        organization=organization,
        agent_name=agent_name,
        provider=selected,
        service_host_query=False,
    )
    if selected == "twilio":
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            "  <Connect>\n"
            f'    <Stream url="{websocket}">\n'
            '      <Parameter name="_pipecatCloudServiceHost" '
            f'value="{service_host}"/>\n'
            "    </Stream>\n"
            "  </Connect>\n"
            "</Response>\n"
        )
    if selected == "telnyx":
        websocket = pipecat_cloud_websocket_url(
            region=region,
            organization=organization,
            agent_name=agent_name,
            provider=selected,
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            "  <Connect>\n"
            f'    <Stream url="{websocket}" '
            'bidirectionalMode="rtp"></Stream>\n'
            "  </Connect>\n"
            '  <Pause length="40"/>\n'
            "</Response>\n"
        )
    websocket = pipecat_cloud_websocket_url(
        region=region,
        organization=organization,
        agent_name=agent_name,
        provider=selected,
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Stream bidirectional="true" keepCallAlive="true" '
        'contentType="audio/x-mulaw;rate=8000">\n'
        f"    {websocket}\n"
        "  </Stream>\n"
        "</Response>\n"
    )


def pipecat_cloud_websocket_url(
    *,
    region: str,
    organization: str,
    agent_name: str,
    provider: PipecatCloudProvider,
    service_host_query: bool = True,
) -> str:
    """Return the installed cloud gateway URL for one provider wire format."""
    _validate_identity(region, organization, agent_name)
    selected = _provider(provider)
    prefix = "" if region == "us-west" else f"{region}."
    wire_provider = "plivo" if selected == "vobiz" else selected
    base = f"wss://{prefix}api.pipecat.daily.co/ws/{wire_provider}"
    if selected == "twilio" or not service_host_query:
        return base
    return f"{base}?serviceHost={agent_name}.{organization}"


def _validate_identity(region: str, organization: str, agent_name: str) -> None:
    if (
        not _REGION.fullmatch(region)
        or not _NAME.fullmatch(organization)
        or not _NAME.fullmatch(agent_name)
    ):
        raise VoicekitError(
            "VK-DEP-008",
            detail="Pipecat Cloud answer route identity is invalid.",
        )


def _provider(value: str) -> PipecatCloudProvider:
    if value not in {"twilio", "telnyx", "vobiz", "plivo"}:
        raise VoicekitError(
            "VK-DEP-008",
            detail=f"Pipecat Cloud answer provider {value!r} is unsupported.",
        )
    return cast(PipecatCloudProvider, value)
