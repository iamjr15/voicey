"""Carrier-authenticated recording callbacks owned by the results companion."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response

from voicey.errors import VoiceyError
from voicey.telephony.models import CallEvent, TelephonyRequest

CallbackProvider = Literal["twilio", "telnyx", "vobiz", "plivo"]


class CarrierCallbackAdapter(Protocol):
    """Carrier verifier/parser subset used by a companion callback."""

    def verify_request(self, request: TelephonyRequest) -> bool: ...

    def parse_event(self, request: TelephonyRequest) -> CallEvent: ...


@dataclass(frozen=True, slots=True)
class CarrierCallbackIngress:
    """One configured carrier callback and its normalized ingestion function."""

    provider: CallbackProvider
    adapter: CarrierCallbackAdapter
    handle: Callable[[CallEvent], Awaitable[None]]
    observe: Callable[[CallEvent], Awaitable[None]] | None = None


def add_carrier_callback_routes(
    app: FastAPI,
    *,
    public_base: str,
    ingresses: tuple[CarrierCallbackIngress, ...],
) -> None:
    """Install only explicitly configured provider routes."""
    parsed = urlsplit(public_base)
    by_provider = {ingress.provider: ingress for ingress in ingresses}
    if len(by_provider) != len(ingresses):
        raise VoiceyError("VY-DEP-003", detail="recording providers are duplicated.")

    async def process(
        provider: CallbackProvider,
        request: Request,
        *,
        recording_only: bool,
    ) -> Response:
        try:
            ingress = by_provider[provider]
        except KeyError as exc:
            raise VoiceyError(
                "VY-TEL-001",
                detail=f"{provider} recording ingress is not configured.",
            ) from exc
        raw = await request.body()
        if provider == "telnyx":
            try:
                raw_body = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VoiceyError(
                    "VY-TEL-008",
                    detail="Telnyx recording callback is not UTF-8.",
                ) from exc
            form: object | None = None
        else:
            raw_body = None
            form = await request.form()
        base_path = parsed.path.rstrip("/")
        telephony = TelephonyRequest(
            scheme=parsed.scheme,
            host=parsed.netloc,
            path=f"{base_path}{request.url.path}",
            headers=dict(request.headers),
            query_string=request.url.query,
            form=form,
            raw_body=raw_body,
            peer_host=None if request.client is None else request.client.host,
        )
        if not ingress.adapter.verify_request(telephony):
            raise VoiceyError(
                "VY-RUN-007",
                detail=f"{provider} recording callback signature is invalid.",
            )
        event = ingress.adapter.parse_event(telephony)
        if event.type in {"recording_ready", "recording_failed"}:
            try:
                await ingress.handle(event)
            except VoiceyError as exc:
                if exc.code == "VY-RES-010":
                    raise VoiceyError(
                        "VY-TEL-009",
                        detail="recording callback arrived before durable terminal state.",
                    ) from exc
                raise
            return Response(status_code=204)
        if recording_only:
            raise VoiceyError(
                "VY-TEL-009",
                detail=f"{provider} callback is not a recording event.",
            )
        if ingress.observe is None:
            raise VoiceyError(
                "VY-TEL-008",
                detail=f"{provider} provider observation handler is unavailable.",
            )
        await ingress.observe(event)
        return Response(status_code=204)

    if "twilio" in by_provider:

        @app.post("/twilio/recordings")
        async def twilio_recording(request: Request) -> Response:
            return await process("twilio", request, recording_only=True)

        _ = twilio_recording

        @app.post("/twilio/events")
        async def twilio_event(request: Request) -> Response:
            return await process("twilio", request, recording_only=False)

        _ = twilio_event

    if "telnyx" in by_provider:

        @app.post("/telnyx/recordings")
        async def telnyx_recording(request: Request) -> Response:
            return await process("telnyx", request, recording_only=True)

        _ = telnyx_recording

        @app.post("/telnyx/events")
        async def telnyx_event(request: Request) -> Response:
            return await process("telnyx", request, recording_only=False)

        _ = telnyx_event

    if "vobiz" in by_provider:

        @app.post("/vobiz/recordings")
        async def vobiz_recording(request: Request) -> Response:
            return await process("vobiz", request, recording_only=True)

        _ = vobiz_recording

        @app.post("/vobiz/events")
        async def vobiz_event(request: Request) -> Response:
            return await process("vobiz", request, recording_only=False)

        _ = vobiz_event

    if "plivo" in by_provider:

        @app.post("/plivo/recordings")
        async def plivo_recording(request: Request) -> Response:
            return await process("plivo", request, recording_only=True)

        _ = plivo_recording

        @app.post("/plivo/events")
        async def plivo_event(request: Request) -> Response:
            return await process("plivo", request, recording_only=False)

        _ = plivo_event


def parse_callback_providers(value: str) -> tuple[CallbackProvider, ...]:
    """Parse an explicit comma list; no carrier is silently preselected."""
    providers: list[CallbackProvider] = []
    allowed = {"twilio", "telnyx", "vobiz", "plivo"}
    for raw in value.split(","):
        item = raw.strip().casefold()
        if not item:
            continue
        if item not in allowed or item in providers:
            raise VoiceyError(
                "VY-DEP-003",
                detail="VOICEY_CALLBACK_PROVIDERS contains an unknown or duplicate value.",
            )
        providers.append(cast("CallbackProvider", item))
    return tuple(providers)
