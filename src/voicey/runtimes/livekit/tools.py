"""Shared typed tools exposed through LiveKit's native function-tool surface."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from livekit.agents import RunContext, ToolError, function_tool
from livekit.agents.llm import RawFunctionTool

from voicey import results
from voicey.config.models import ToolReference
from voicey.tools import ToolExecutor, get_tool_metadata, load_tools
from voicey.tools.execution import ToolObservationSink, tool_execution_context


def shared_livekit_tools(
    references: str | list[ToolReference],
    *,
    call_id: str,
    buffer: results.CallResultBuffer,
    sink: ToolObservationSink,
    executor: ToolExecutor | None = None,
) -> list[RawFunctionTool[..., Any]]:
    """Wrap shared declarations as native raw-schema LiveKit tools."""
    active_executor = executor or ToolExecutor()
    native: list[RawFunctionTool[..., Any]] = []
    for function in load_tools(references):
        metadata = get_tool_metadata(function)

        async def invoke(
            ctx: RunContext[Any],
            raw_arguments: dict[str, Any],
            *,
            _function: Callable[..., Any] = function,
            _say: str | None = metadata.say_while_running,
            _mutating: bool = metadata.mutating,
        ) -> Any:
            if _mutating:
                ctx.disallow_interruptions()
            async with _filler(ctx, _say):
                with (
                    results.result_context(buffer),
                    tool_execution_context(call_id, sink),
                ):
                    execution = await active_executor.execute(_function, raw_arguments)
            if not execution.ok:
                raise ToolError(
                    json.dumps(
                        execution.for_llm()["error"],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            return execution.value

        native.append(
            function_tool(
                invoke,
                raw_schema={
                    "name": metadata.name,
                    "description": metadata.description,
                    "parameters": dict(metadata.parameters_schema),
                },
            )
        )
    return native


@asynccontextmanager
async def _filler(
    context: RunContext[Any],
    text: str | None,
) -> AsyncGenerator[None]:
    if text is None:
        yield
        return
    async with context.with_filler(text):
        yield
