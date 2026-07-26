"""The runtime-neutral portion of the public ``@tool`` contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, overload

from voicekit.errors import VoicekitError

FunctionT = TypeVar("FunctionT", bound=Callable[..., Any])
_METADATA_ATTRIBUTE = "__voicekit_tool__"


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Metadata consumed by native Pipecat and LiveKit tool adapters."""

    name: str
    description: str
    say_while_running: str | None


@overload
def tool(function: FunctionT, /) -> FunctionT: ...


@overload
def tool(
    function: None = None,
    /,
    *,
    say_while_running: str | None = None,
) -> Callable[[FunctionT], FunctionT]: ...


def tool(
    function: FunctionT | None = None,
    /,
    *,
    say_while_running: str | None = None,
) -> FunctionT | Callable[[FunctionT], FunctionT]:
    """Mark a plain typed function as a voicekit tool without wrapping it."""

    def decorate(candidate: FunctionT) -> FunctionT:
        description = (candidate.__doc__ or "").strip()
        metadata = ToolMetadata(
            name=candidate.__name__,
            description=description,
            say_while_running=say_while_running,
        )
        setattr(candidate, _METADATA_ATTRIBUTE, metadata)
        return candidate

    if function is not None:
        return decorate(function)
    return decorate


def get_tool_metadata(function: Callable[..., Any]) -> ToolMetadata:
    """Return tool metadata, rejecting undecorated callables."""
    metadata = getattr(function, _METADATA_ATTRIBUTE, None)
    if not isinstance(metadata, ToolMetadata):
        detail = f"{getattr(function, '__name__', function)!r} is not decorated with @tool."
        raise VoicekitError("VK-TOL-001", detail=detail)
    return metadata
