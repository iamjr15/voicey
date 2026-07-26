"""Shared carrier adapter contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from voicekit.telephony.models import (
    CallEvent,
    Capabilities,
    NumberInfo,
    RollbackToken,
    RuntimeTarget,
    TelephonyRequest,
)


@runtime_checkable
class TelephonyAdapter(Protocol):
    """Operations implemented by every built-in and third-party carrier."""

    provider: str
    capabilities: Capabilities

    def list_numbers(self) -> list[NumberInfo]: ...

    def buy_number(
        self,
        country: str,
        area: str | None = None,
    ) -> NumberInfo: ...

    def release_number(self, number: str) -> None: ...

    def point_inbound(
        self,
        number: str,
        target: RuntimeTarget,
    ) -> RollbackToken: ...

    def restore(self, token: RollbackToken) -> None: ...

    def start_call(
        self,
        from_no: str,
        to_no: str,
        target: RuntimeTarget,
    ) -> str: ...

    def verify_request(self, request: TelephonyRequest) -> bool: ...

    def answer_response(self, target: RuntimeTarget) -> str: ...

    def parse_event(self, request: TelephonyRequest) -> CallEvent: ...
