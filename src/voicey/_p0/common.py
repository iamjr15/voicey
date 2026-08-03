"""Shared evidence model for the two P0 walking-skeleton probes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from voicey.results import CallResultBuffer, SignedWebhook, WebhookSigner, encode_secret


@dataclass(frozen=True, slots=True)
class BrowserEvidence:
    """Proof that the runtime's browser-session mechanism was exercised."""

    session_id: str
    connected: bool


@dataclass(frozen=True, slots=True)
class PhoneTermination:
    """Terminal state produced by the provider mock."""

    call_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    """Evidence returned by one runtime's complete P0 slice."""

    runtime: str
    native_bootstrap: str
    native_tool_name: str
    tool_result: str
    results: dict[str, Any]
    browser: BrowserEvidence
    phone_termination: PhoneTermination
    phone_termination_count: int
    signed_webhook: SignedWebhook
    webhook_secret: str


class MockPhoneProvider:
    """Minimal idempotent provider terminalization used only by the P0 spike."""

    def __init__(self) -> None:
        self.termination: PhoneTermination | None = None
        self.termination_count = 0

    async def terminate(self, call_id: str, reason: str) -> PhoneTermination:
        """Record the first terminal state and return it on duplicate callbacks."""
        if self.termination is None:
            self.termination = PhoneTermination(call_id=call_id, reason=reason)
            self.termination_count += 1
        return self.termination


def finalize_probe(
    *,
    runtime: str,
    native_bootstrap: str,
    native_tool_name: str,
    tool_result: str,
    buffer: CallResultBuffer,
    browser: BrowserEvidence,
    phone: MockPhoneProvider,
) -> RuntimeProbe:
    """Build and sign the immutable terminal envelope for a completed probe."""
    if phone.termination is None:
        msg = "P0 provider mock was not terminalized"
        raise AssertionError(msg)
    event_id = f"evt_p0_{runtime}"
    payload = {
        "event": "call.completed",
        "id": event_id,
        "call": {
            "id": buffer.call_id,
            "ended_reason": phone.termination.reason,
        },
        "agent": {"name": "p0-walking-skeleton", "runtime": runtime},
        "outcome": buffer.outcome,
        "data": dict(buffer.data),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    secret = encode_secret(f"voicey-p0-{runtime}-signing-key".encode())
    signed = WebhookSigner(secret).sign(event_id, body, timestamp=1_750_000_000)
    return RuntimeProbe(
        runtime=runtime,
        native_bootstrap=native_bootstrap,
        native_tool_name=native_tool_name,
        tool_result=tool_result,
        results=dict(buffer.snapshot()),
        browser=browser,
        phone_termination=phone.termination,
        phone_termination_count=phone.termination_count,
        signed_webhook=signed,
        webhook_secret=secret,
    )
