"""Deterministic discovery of decorated tools from config references."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from types import ModuleType
from typing import Any

from voicekit.config.models import ToolReference
from voicekit.errors import VoicekitError
from voicekit.tools.core import get_tool_metadata


def load_tools(references: str | list[ToolReference]) -> tuple[Callable[..., Any], ...]:
    """Load decorated callables from modules or an explicit callable list."""
    configured = [references] if isinstance(references, str) else references
    discovered: list[Callable[..., Any]] = []
    for reference in configured:
        if isinstance(reference, str):
            discovered.extend(_module_tools(_import_module(reference)))
        else:
            get_tool_metadata(reference)
            discovered.append(reference)

    indexed: dict[str, Callable[..., Any]] = {}
    for function in discovered:
        metadata = get_tool_metadata(function)
        if metadata.name in indexed and indexed[metadata.name] is not function:
            raise VoicekitError(
                "VK-TOL-002",
                detail=f"duplicate configured tool name {metadata.name!r}.",
            )
        indexed[metadata.name] = function
    return tuple(indexed[name] for name in sorted(indexed))


def _import_module(reference: str) -> ModuleType:
    try:
        return importlib.import_module(reference)
    except (ImportError, AttributeError) as exc:
        raise VoicekitError(
            "VK-TOL-001",
            detail=f"cannot import configured tools module {reference!r}.",
        ) from exc


def _module_tools(module: ModuleType) -> list[Callable[..., Any]]:
    functions: list[Callable[..., Any]] = []
    for _, candidate in inspect.getmembers(module):
        if not callable(candidate):
            continue
        try:
            get_tool_metadata(candidate)
        except VoicekitError as exc:
            if exc.code == "VK-TOL-001":
                continue
            raise
        functions.append(candidate)
    return functions
