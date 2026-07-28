"""Build-time capability registry used to prevent dead-end CLI choices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from voicekit.errors import VoicekitError

CapabilityKind: TypeAlias = Literal["runtime", "carrier", "recipe", "deploy"]


@dataclass(frozen=True, slots=True)
class Capability:
    """One factual CLI choice and its implementation state."""

    kind: CapabilityKind
    id: str
    description: str
    enabled: bool
    unavailable_reason: str | None = None
    install_extra: str | None = None

    def require(self) -> Capability:
        """Return an enabled capability or a stable, actionable error."""
        if self.enabled:
            return self
        reason = self.unavailable_reason or "not implemented in this build"
        extra = (
            f' Install with `uv pip install "voicekit[{self.install_extra}]"`.'
            if self.install_extra
            else ""
        )
        raise VoicekitError(
            "VK-CLI-005",
            detail=f"{self.kind} {self.id!r} is unavailable: {reason}.{extra}",
        )


class CapabilityRegistry:
    """Immutable indexed capability set with deterministic display ordering."""

    def __init__(self, capabilities: tuple[Capability, ...]) -> None:
        indexed: dict[tuple[CapabilityKind, str], Capability] = {}
        for capability in capabilities:
            key = (capability.kind, capability.id)
            if key in indexed:
                raise AssertionError(f"duplicate capability: {capability.kind}/{capability.id}")
            indexed[key] = capability
        self._capabilities = capabilities
        self._indexed = indexed

    def get(self, kind: CapabilityKind, identifier: str) -> Capability | None:
        return self._indexed.get((kind, identifier))

    def require(self, kind: CapabilityKind, identifier: str) -> Capability:
        capability = self.get(kind, identifier)
        if capability is None:
            raise VoicekitError(
                "VK-CLI-005",
                detail=f"{kind} {identifier!r} is unknown in this build.",
            )
        return capability.require()

    def choices(
        self,
        kind: CapabilityKind,
        *,
        include_unavailable: bool = False,
    ) -> tuple[Capability, ...]:
        return tuple(
            sorted(
                (
                    capability
                    for capability in self._capabilities
                    if capability.kind == kind and (include_unavailable or capability.enabled)
                ),
                key=lambda capability: capability.id,
            )
        )


DEFAULT_CAPABILITIES = CapabilityRegistry(
    (
        Capability(
            kind="runtime",
            id="pipecat",
            description=(
                "Open-source Python pipeline framework by Daily; "
                "phone audio uses carrier media streams."
            ),
            enabled=True,
            install_extra="pipecat",
        ),
        Capability(
            kind="runtime",
            id="livekit",
            description=("Open-source agent framework on LiveKit WebRTC and SIP infrastructure."),
            enabled=True,
            install_extra="livekit",
        ),
        Capability(
            kind="carrier",
            id="twilio",
            description="Programmable voice and SIP carrier with country-specific pricing.",
            enabled=True,
            install_extra="twilio",
        ),
        Capability(
            kind="carrier",
            id="telnyx",
            description="Call Control and SIP carrier with country-specific pricing.",
            enabled=True,
            install_extra="telnyx",
        ),
        Capability(
            kind="carrier",
            id="vobiz",
            description="India-focused programmable voice and SIP carrier.",
            enabled=True,
            install_extra="vobiz",
        ),
        Capability(
            kind="carrier",
            id="plivo",
            description="Programmable voice carrier; launch tier is beta.",
            enabled=True,
            install_extra="plivo",
        ),
        Capability(
            kind="carrier",
            id="sip",
            description="Generic SIP trunk for the LiveKit runtime.",
            enabled=True,
            install_extra="livekit",
        ),
        Capability(
            kind="recipe",
            id="scratch",
            description="A minimal talking agent seeded from your own description.",
            enabled=True,
        ),
        Capability(
            kind="recipe",
            id="appointment-booking",
            description="Book, reschedule, and cancel appointments through a calendar stub.",
            enabled=True,
        ),
        Capability(
            kind="recipe",
            id="restaurant-reservations",
            description="Reserve tables and offer an explicit waitlist fallback.",
            enabled=True,
        ),
        Capability(
            kind="recipe",
            id="front-desk",
            description="Answer, triage, take messages, and warm transfer.",
            enabled=True,
        ),
        Capability(
            kind="recipe",
            id="lead-intake",
            description="Qualify inquiries, capture consented leads, and schedule follow-up.",
            enabled=True,
        ),
        Capability(
            kind="deploy",
            id="docker",
            description="Self-hosted container with durable local storage.",
            enabled=True,
        ),
        Capability(
            kind="deploy",
            id="pipecat-cloud",
            description="Managed Pipecat worker deployment.",
            enabled=False,
            unavailable_reason="the cloud target and results relay land in P3",
        ),
        Capability(
            kind="deploy",
            id="livekit-cloud",
            description="Managed LiveKit agent deployment.",
            enabled=False,
            unavailable_reason="the cloud target and results relay land in P3",
        ),
        Capability(
            kind="deploy",
            id="fly",
            description="Fly deployment with managed Postgres companion.",
            enabled=True,
        ),
        Capability(
            kind="deploy",
            id="railway",
            description="Railway deployment with managed Postgres.",
            enabled=False,
            unavailable_reason="the Railway target lands in P4",
        ),
    )
)
