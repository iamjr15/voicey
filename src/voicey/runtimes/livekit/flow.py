"""Loader for native LiveKit agent workflows.

This module deliberately accepts only LiveKit ``Agent`` objects or factories
that return one. It is an import seam, not a conversation DSL.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from livekit.agents import Agent
from livekit.agents import llm as lk_llm

from voicey.errors import VoiceyError

AgentFactory = Callable[
    [list[lk_llm.Tool | lk_llm.Toolset]],
    Agent | Awaitable[Agent],
]


async def load_native_agent(
    reference: str,
    *,
    shared_tools: list[lk_llm.Tool | lk_llm.Toolset],
) -> Agent:
    """Load one native Agent and merge engine-supplied tools before start."""
    module_name, attribute = reference.split(":", maxsplit=1)
    try:
        entry = getattr(importlib.import_module(module_name), attribute)
        native = await _call_entry(entry, shared_tools)
        if not isinstance(native, Agent):
            raise VoiceyError(
                "VY-RUN-003",
                detail="LiveKit flow entrypoint must return a native livekit.agents.Agent.",
            )
        existing = list(native.tools)
        names = {tool.info.name for tool in existing if isinstance(tool, lk_llm.FunctionTool)}
        additions: list[lk_llm.Tool | lk_llm.Toolset] = []
        for tool in shared_tools:
            if isinstance(tool, lk_llm.FunctionTool) and tool.info.name in names:
                continue
            additions.append(cast(lk_llm.Tool | lk_llm.Toolset, tool))
        await native.update_tools([*existing, *additions])
        return native
    except VoiceyError:
        raise
    except Exception as exc:
        raise VoiceyError(
            "VY-RUN-003",
            detail=f"native LiveKit workflow {reference!r} could not initialize.",
        ) from exc


async def _call_entry(
    entry: object,
    tools: list[lk_llm.Tool | lk_llm.Toolset],
) -> Any:
    if isinstance(entry, Agent):
        return entry
    if not callable(entry):
        raise VoiceyError(
            "VY-RUN-003",
            detail="LiveKit flow entrypoint must be an Agent or callable factory.",
        )
    parameters = inspect.signature(entry).parameters
    if len(parameters) == 0:
        result = cast(Callable[[], Any], entry)()
    elif len(parameters) == 1:
        result = cast(AgentFactory, entry)(tools)
    else:
        raise VoiceyError(
            "VY-RUN-003",
            detail="LiveKit flow factory accepts only zero args or one native-tools arg.",
        )
    if inspect.isawaitable(result):
        return await result
    return result
