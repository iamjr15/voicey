"""Safe, deterministic discovery of owned scenario source files."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar, cast

from pydantic import ValidationError

from voicekit.errors import VoicekitError
from voicekit.testing.models import ScenarioDefinition

ScenarioFunction = Callable[[], Mapping[str, Any] | ScenarioDefinition]
F = TypeVar("F", bound=ScenarioFunction)
_SCENARIO_MARKER = "__voicekit_scenario__"


def scenario(function: F) -> F:
    """Mark a zero-argument owned function as a voicekit scenario."""
    if inspect.signature(function).parameters:
        raise VoicekitError(
            "VK-TST-001",
            detail=f"@scenario function {function.__name__!r} must accept no arguments.",
        )
    setattr(function, _SCENARIO_MARKER, True)
    return function


def discover_scenarios(root: Path) -> tuple[ScenarioDefinition, ...]:
    """Import only the documented scenario locations and validate every marker."""
    test_root = root / "tests"
    candidates = [test_root / "scenarios.py"]
    scenario_dir = test_root / "scenarios"
    if scenario_dir.is_dir():
        candidates.extend(sorted(scenario_dir.glob("*.py")))
    files = [path for path in candidates if path.is_file() and path.name != "__init__.py"]
    if not files:
        raise VoicekitError(
            "VK-TST-001",
            detail="no tests/scenarios.py or tests/scenarios/*.py files were found.",
        )

    definitions: list[ScenarioDefinition] = []
    with _project_path(root):
        for index, path in enumerate(files):
            module_name = f"_voicekit_scenarios_{index}_{path.stem}"
            module = _load_module(path, module_name)
            try:
                definitions.extend(_definitions(module, path))
            finally:
                sys.modules.pop(module_name, None)
    if not definitions:
        raise VoicekitError(
            "VK-TST-001",
            detail="scenario files contain no @scenario functions.",
        )
    names = [definition.name for definition in definitions]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise VoicekitError(
            "VK-TST-001",
            detail=f"duplicate scenario names: {', '.join(duplicates)}.",
        )
    return tuple(sorted(definitions, key=lambda item: item.name))


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise VoicekitError("VK-TST-001", detail=f"cannot import {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except VoicekitError:
        raise
    except Exception as exc:
        raise VoicekitError(
            "VK-TST-001",
            detail=f"{path} raised {type(exc).__name__} during import.",
        ) from exc
    return module


def _definitions(module: ModuleType, path: Path) -> list[ScenarioDefinition]:
    found: list[ScenarioDefinition] = []
    members = inspect.getmembers(module, inspect.isfunction)
    for name, candidate in members:
        if not getattr(candidate, _SCENARIO_MARKER, False):
            continue
        function = cast(ScenarioFunction, candidate)
        try:
            raw = function()
            if isinstance(raw, ScenarioDefinition):
                definition = raw
            else:
                definition = ScenarioDefinition.model_validate({"name": name, **dict(raw)})
        except (TypeError, ValueError, ValidationError) as exc:
            raise VoicekitError(
                "VK-TST-001",
                detail=f"{path}:{name} returned an invalid scenario: {exc}.",
            ) from exc
        found.append(definition)
    return found


@contextmanager
def _project_path(root: Path):
    text = str(root)
    sys.path.insert(0, text)
    try:
        yield
    finally:
        if text in sys.path:
            sys.path.remove(text)
