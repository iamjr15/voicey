"""Runtime-neutral telephony values shared by carrier adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from urllib.parse import quote, urlsplit

from voicekit.errors import VoicekitError
from voicekit.storage.models import EndedReason

_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

CallEventType: TypeAlias = Literal[
    "initiated",
    "ringing",
    "answered",
    "completed",
    "failed",
    "recording_ready",
    "recording_failed",
    "amd",
    "dtmf",
]
TransferMode: TypeAlias = Literal["cold", "warm"]
CallDirection: TypeAlias = Literal["inbound", "outbound"]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Features that callers may safely offer for one carrier path."""

    inbound: bool
    outbound: bool
    amd: bool
    dtmf_receive: bool
    dtmf_send: bool
    transfer_modes: frozenset[TransferMode]
    recording: bool
    regions: tuple[str, ...]
    native_outbound_idempotency: bool
    livekit_sip: bool


@dataclass(frozen=True, slots=True)
class NumberInfo:
    """One owned or purchasable phone number."""

    number: str
    provider_id: str
    friendly_name: str | None = None
    country: str | None = None
    locality: str | None = None
    region: str | None = None
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CarrierAccountState:
    """Safe account facts used by doctor; never contains credentials."""

    provider: str
    status: str
    account_type: str | None
    balance: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class PipecatTarget:
    """Public HTTP/WS routes for a Pipecat media-stream worker."""

    https_base: str
    ws_path: str = "/twilio/media"
    answer_path: str = "/twilio/answer"
    event_path: str = "/twilio/events"
    recording_path: str = "/twilio/recordings"
    amd_path: str = "/twilio/amd"
    custom_parameters: dict[str, str] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        parsed = urlsplit(self.https_base)
        paths = (
            self.ws_path,
            self.answer_path,
            self.event_path,
            self.recording_path,
            self.amd_path,
        )
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(not path.startswith("/") or "?" in path or "#" in path for path in paths)
        ):
            raise VoicekitError(
                "VK-TEL-002",
                detail="PipecatTarget requires an HTTPS base and absolute query-free paths.",
            )
        if any(
            not _SAFE_IDENTIFIER.fullmatch(name) or len(value) > 500
            for name, value in self.custom_parameters.items()
        ):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Carrier stream parameters require safe names and values up to 500 chars.",
            )

    @property
    def stream_url(self) -> str:
        """Return the WSS media URL without a query string."""
        base = self.https_base.rstrip("/")
        return f"wss://{urlsplit(base).netloc}{urlsplit(base).path}{self.ws_path}"

    @property
    def answer_url(self) -> str:
        return _join(self.https_base, self.answer_path)

    def event_url(self, intent_id: str | None = None) -> str:
        suffix = "" if intent_id is None else f"/{quote(intent_id, safe='')}"
        return _join(self.https_base, f"{self.event_path}{suffix}")

    @property
    def recording_url(self) -> str:
        return _join(self.https_base, self.recording_path)

    @property
    def amd_url(self) -> str:
        return _join(self.https_base, self.amd_path)


@dataclass(frozen=True, slots=True)
class LiveKitTarget:
    """Carrier-side SIP destination used by the P2 LiveKit provisioner."""

    project: str
    sip_uri: str


RuntimeTarget: TypeAlias = PipecatTarget | LiveKitTarget


@dataclass(frozen=True, slots=True)
class RollbackToken:
    """Opaque durable routing snapshot reference."""

    provider: str
    token: str


@dataclass(frozen=True, slots=True)
class CallEvent:
    """Normalized carrier callback or media event."""

    type: CallEventType
    provider_call_id: str
    provider_status: str
    ended_reason: EndedReason | None = None
    recording_sid: str | None = None
    recording_url: str | None = None
    answered_by: str | None = None
    digits: str | None = None
    intent_id: str | None = None
    direction: CallDirection | None = None
    from_number: str | None = None
    to_number: str | None = None


@dataclass(frozen=True, slots=True)
class TelephonyRequest:
    """Framework-neutral request values needed for verification and parsing."""

    scheme: str
    host: str
    path: str
    headers: dict[str, str]
    query_string: str = ""
    form: object | None = None
    raw_body: str | None = None
    peer_host: str | None = None
    is_websocket: bool = False
    route_params: dict[str, str] = field(default_factory=lambda: {})


def validate_e164(number: str) -> str:
    """Validate the common carrier number format at adapter boundaries."""
    if not _E164.fullmatch(number):
        raise VoicekitError("VK-TEL-002", detail=f"invalid E.164 number {number!r}.")
    return number


def validate_identifier(value: str, *, field_name: str) -> str:
    """Validate ids embedded into callback paths and durable records."""
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise VoicekitError("VK-TEL-002", detail=f"invalid {field_name}.")
    return value


def _join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"
