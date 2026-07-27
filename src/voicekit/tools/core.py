"""Plain typed tool declarations and JSON Schema generation."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, get_type_hints, overload

from pydantic import TypeAdapter

from voicekit.errors import VoicekitError

if TYPE_CHECKING:
    from voicekit.tools.http import HttpTool

FunctionT = TypeVar("FunctionT", bound=Callable[..., Any])
_METADATA_ATTRIBUTE = "__voicekit_tool__"
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Metadata consumed by native Pipecat and LiveKit tool adapters."""

    name: str
    description: str
    parameters_schema: Mapping[str, Any]
    return_schema: Mapping[str, Any]
    say_while_running: str | None
    mutating: bool
    is_async: bool
    source: str = "python"


class ToolDecorator:
    """Callable public decorator with an attached HTTP-tool constructor."""

    @overload
    def __call__(self, function: FunctionT, /) -> FunctionT: ...

    @overload
    def __call__(
        self,
        function: None = None,
        /,
        *,
        say_while_running: str | None = None,
        mutating: bool = False,
    ) -> Callable[[FunctionT], FunctionT]: ...

    def __call__(
        self,
        function: FunctionT | None = None,
        /,
        *,
        say_while_running: str | None = None,
        mutating: bool = False,
    ) -> FunctionT | Callable[[FunctionT], FunctionT]:
        """Mark a plain typed function as a voicekit tool without wrapping it."""

        def decorate(candidate: FunctionT) -> FunctionT:
            metadata = metadata_from_callable(
                candidate,
                say_while_running=say_while_running,
                mutating=mutating,
            )
            setattr(candidate, _METADATA_ATTRIBUTE, metadata)
            return candidate

        if function is not None:
            return decorate(function)
        return decorate

    def http(
        self,
        *,
        name: str,
        url: str,
        method: str = "GET",
        headers_env: Mapping[str, str] | None = None,
        timeout_s: float = 8.0,
        say_while_running: str | None = None,
        mutating: bool = False,
        description: str | None = None,
    ) -> HttpTool:
        """Create an HTTP-backed tool while keeping credentials environment-only."""
        from voicekit.tools.http import HttpTool

        return HttpTool(
            name=name,
            url=url,
            method=method,
            headers_env=headers_env or {},
            timeout_s=timeout_s,
            say_while_running=say_while_running,
            mutating=mutating,
            description=description,
        )


tool = ToolDecorator()


def metadata_from_callable(
    function: Callable[..., Any],
    *,
    say_while_running: str | None,
    mutating: bool = False,
) -> ToolMetadata:
    """Build stable metadata from a callable signature and docstring."""
    name = getattr(function, "__name__", "")
    validate_tool_name(name)
    description = inspect.getdoc(function) or ""
    signature = inspect.signature(function)
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, TypeError) as exc:
        raise VoicekitError("VK-TOL-002", detail=f"{name}: {exc}") from exc

    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter_name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise VoicekitError(
                "VK-TOL-002",
                detail=f"{name}.{parameter_name} uses variadic arguments.",
            )
        annotation = hints.get(parameter_name)
        if annotation is None:
            raise VoicekitError(
                "VK-TOL-002",
                detail=f"{name}.{parameter_name} has no type annotation.",
            )
        try:
            adapter = TypeAdapter(annotation)
            properties[parameter_name] = adapter.json_schema()
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter_name)
            else:
                properties[parameter_name]["default"] = adapter.dump_python(
                    parameter.default,
                    mode="json",
                )
        except Exception as exc:
            raise VoicekitError(
                "VK-TOL-002",
                detail=f"{name}.{parameter_name} cannot be represented as JSON Schema.",
            ) from exc

    return_annotation = hints.get("return")
    if return_annotation is None:
        raise VoicekitError(
            "VK-TOL-002",
            detail=f"{name} has no return type annotation.",
        )
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters_schema["required"] = required
    try:
        return_schema = TypeAdapter(return_annotation).json_schema()
    except Exception as exc:
        raise VoicekitError(
            "VK-TOL-002",
            detail=f"{name} return type cannot be represented as JSON Schema.",
        ) from exc
    return ToolMetadata(
        name=name,
        description=description,
        parameters_schema=parameters_schema,
        return_schema=return_schema,
        say_while_running=say_while_running,
        mutating=mutating,
        is_async=inspect.iscoroutinefunction(function),
    )


def get_tool_metadata(function: Callable[..., Any]) -> ToolMetadata:
    """Return tool metadata, rejecting undecorated callables."""
    metadata = getattr(function, _METADATA_ATTRIBUTE, None)
    if not isinstance(metadata, ToolMetadata):
        detail = f"{getattr(function, '__name__', function)!r} is not decorated with @tool."
        raise VoicekitError("VK-TOL-001", detail=detail)
    return metadata


def set_tool_metadata(function: object, metadata: ToolMetadata) -> None:
    """Attach precomputed metadata to callable tool objects such as HTTP tools."""
    setattr(function, _METADATA_ATTRIBUTE, metadata)


def validate_tool_name(name: str) -> None:
    """Reject names native runtime tool registries cannot represent."""
    if not _TOOL_NAME.fullmatch(name):
        raise VoicekitError("VK-TOL-002", detail=f"invalid tool name: {name!r}.")
