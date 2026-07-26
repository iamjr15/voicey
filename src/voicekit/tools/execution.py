"""Timeout-bounded tool execution with context propagation and observations."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol, cast, get_type_hints

from pydantic import JsonValue, TypeAdapter, ValidationError

from voicekit.errors import VoicekitError
from voicekit.obs.records import ToolCallObservation
from voicekit.tools.core import get_tool_metadata


class ToolObservationSink(Protocol):
    """Persist final tool observations for one call."""

    async def record(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None: ...


class ToolObservationStore(Protocol):
    """Minimal repository surface used by the observation adapter."""

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None: ...


class HttpArgumentValidator(Protocol):
    """HTTP-tool validation surface used before invocation."""

    def validate_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...


class RepositoryToolObservationSink:
    """Persist executor observations in the protected per-call record."""

    def __init__(self, store: ToolObservationStore) -> None:
        self._store = store

    async def record(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        await self._store.record_tool_call(call_id, observation)


@dataclass(frozen=True, slots=True)
class ToolErrorResult:
    """Safe error surfaced to the LLM instead of an exception or stack trace."""

    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Structured, JSON-safe result consumed by native runtime adapters."""

    ok: bool
    value: JsonValue | None
    error: ToolErrorResult | None
    duration_ms: float
    invocation_id: str

    def for_llm(self) -> dict[str, JsonValue]:
        """Return the runtime-neutral tool result sent to the model."""
        if self.ok:
            return {"ok": True, "value": self.value}
        assert self.error is not None
        return {
            "ok": False,
            "error": {
                "code": self.error.code,
                "message": self.error.message,
                "retryable": self.error.retryable,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Per-call observation binding isolated by contextvars."""

    call_id: str
    sink: ToolObservationSink


_execution_context: ContextVar[ToolExecutionContext | None] = ContextVar(
    "voicekit_tool_execution_context",
    default=None,
)


@contextmanager
def tool_execution_context(
    call_id: str,
    sink: ToolObservationSink,
) -> Generator[ToolExecutionContext]:
    """Bind tool observations to the current call and propagate into threads."""
    context = ToolExecutionContext(call_id=call_id, sink=sink)
    token = _execution_context.set(context)
    try:
        yield context
    finally:
        _execution_context.reset(token)


class ToolExecutor:
    """Validate, execute, normalize, and observe plain Python/HTTP tools."""

    def __init__(self, *, timeout_s: float = 8.0) -> None:
        if timeout_s <= 0:
            raise VoicekitError("VK-TOL-003", detail="timeout_s must be positive.")
        self.timeout_s = timeout_s

    async def execute(
        self,
        function: Callable[..., Any],
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute one decorated tool without leaking exceptions to the LLM."""
        metadata = get_tool_metadata(function)
        invocation_id = f"tool_{uuid.uuid4().hex}"
        started = time.perf_counter()
        normalized_arguments: dict[str, JsonValue] = {}
        status = "succeeded"
        value: JsonValue | None = None
        error: ToolErrorResult | None = None
        try:
            if metadata.source == "http":
                validator = cast("HttpArgumentValidator", function)
                call_arguments = validator.validate_arguments(arguments)
                normalized_arguments = _json_mapping(None, call_arguments)
            else:
                call_arguments = _validate_arguments(function, arguments)
                normalized_arguments = _json_mapping(function, call_arguments)
        except (TypeError, ValueError, ValidationError):
            status = "failed"
            error = ToolErrorResult(
                code="invalid_arguments",
                message="The tool arguments do not match its schema.",
                retryable=False,
            )
        else:
            try:
                configured_timeout = getattr(function, "timeout_s", self.timeout_s)
                raw_result = await asyncio.wait_for(
                    _invoke(function, call_arguments, is_async=metadata.is_async),
                    timeout=float(configured_timeout),
                )
                value = (
                    cast(
                        "JsonValue",
                        TypeAdapter(JsonValue).validate_python(raw_result),
                    )
                    if metadata.source == "http"
                    else _normalize_result(function, raw_result)
                )
            except TimeoutError:
                status = "timed_out"
                error = ToolErrorResult(
                    code="tool_timeout",
                    message="The tool timed out before it returned.",
                    retryable=True,
                )
            except Exception:
                status = "failed"
                error = ToolErrorResult(
                    code="tool_failed",
                    message="The tool could not complete the request.",
                    retryable=False,
                )
        duration_ms = (time.perf_counter() - started) * 1000
        result = ToolExecutionResult(
            ok=error is None,
            value=value,
            error=error,
            duration_ms=duration_ms,
            invocation_id=invocation_id,
        )
        context = _execution_context.get()
        if context is not None:
            try:
                await context.sink.record(
                    context.call_id,
                    ToolCallObservation.model_validate(
                        {
                            "invocation_id": invocation_id,
                            "tool_name": metadata.name,
                            "arguments": normalized_arguments,
                            "result": result.for_llm(),
                            "duration_ms": duration_ms,
                            "status": status,
                        }
                    ),
                )
            except Exception as exc:
                raise VoicekitError(
                    "VK-TOL-005",
                    detail=f"tool observation persistence failed for {metadata.name!r}.",
                ) from exc
        return result


async def _invoke(
    function: Callable[..., Any],
    arguments: Mapping[str, Any],
    *,
    is_async: bool,
) -> Any:
    if is_async:
        awaitable = cast("Callable[..., Awaitable[Any]]", function)(**arguments)
        return await awaitable
    context = copy_context()
    call = partial(function, **arguments)
    return await asyncio.to_thread(context.run, call)


def _validate_arguments(
    function: Callable[..., Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    signature = inspect.signature(function)
    bound = signature.bind(**arguments)
    bound.apply_defaults()
    hints = get_type_hints(function, include_extras=True)
    return {
        name: TypeAdapter(hints[name]).validate_python(value)
        for name, value in bound.arguments.items()
    }


def _normalize_result(function: Callable[..., Any], value: Any) -> JsonValue:
    hints = get_type_hints(function, include_extras=True)
    annotation = hints.get("return", Any)
    adapter = TypeAdapter(annotation)
    validated = adapter.validate_python(value)
    dumped = adapter.dump_python(validated, mode="json")
    return cast("JsonValue", TypeAdapter(JsonValue).validate_python(dumped))


def _json_mapping(
    function: Callable[..., Any] | None,
    values: Mapping[str, Any],
) -> dict[str, JsonValue]:
    hints = {} if function is None else get_type_hints(function, include_extras=True)
    normalized: dict[str, JsonValue] = {}
    for key, value in values.items():
        dumped = (
            value if function is None else TypeAdapter(hints[key]).dump_python(value, mode="json")
        )
        normalized[key] = cast(
            "JsonValue",
            TypeAdapter(JsonValue).validate_python(dumped),
        )
    return normalized
