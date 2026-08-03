"""Context-local results recorder used directly from native flow code."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from voicey.errors import VoiceyError


@dataclass(slots=True)
class CallResultBuffer:
    """Mutable per-call buffer; persistence adapters snapshot it incrementally."""

    call_id: str
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    outcome: str | None = None

    def snapshot(self) -> Mapping[str, Any]:
        """Return a detached representation safe to serialize."""
        return {
            "call_id": self.call_id,
            "outcome": self.outcome,
            "data": dict(self.data),
        }


_current_buffer: ContextVar[CallResultBuffer | None] = ContextVar(
    "voicey_current_result_buffer",
    default=None,
)


@contextmanager
def result_context(buffer: CallResultBuffer) -> Generator[CallResultBuffer, None, None]:
    """Bind a result buffer to the current async/thread context."""
    token = _current_buffer.set(buffer)
    try:
        yield buffer
    finally:
        _current_buffer.reset(token)


def set(key: str, value: Any) -> None:
    """Record a structured result field for the current call."""
    _require_buffer().data[key] = value


def set_outcome(outcome: str) -> None:
    """Record the current call's business outcome."""
    _require_buffer().outcome = outcome


def _require_buffer() -> CallResultBuffer:
    buffer = _current_buffer.get()
    if buffer is None:
        raise VoiceyError("VY-RES-005")
    return buffer
