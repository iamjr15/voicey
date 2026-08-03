"""Public simulated-caller testing API."""

from typing import TYPE_CHECKING

from voicey.testing.discovery import discover_scenarios, scenario
from voicey.testing.models import (
    JudgeConfig,
    LiveTestingConfig,
    Persona,
    ResultExpectation,
    ScenarioDefinition,
    ScenarioMetrics,
    ScenarioTurn,
    SendAfter,
    TestingConfig,
    TestProfile,
    ToolExpectation,
    TurnExpectation,
)

if TYPE_CHECKING:
    from voicey.testing.soak import SoakConfig, SoakReport, run_engine_soak

__all__ = [
    "JudgeConfig",
    "LiveTestingConfig",
    "Persona",
    "ResultExpectation",
    "ScenarioDefinition",
    "ScenarioMetrics",
    "ScenarioTurn",
    "SendAfter",
    "SoakConfig",
    "SoakReport",
    "TestProfile",
    "TestingConfig",
    "ToolExpectation",
    "TurnExpectation",
    "discover_scenarios",
    "run_engine_soak",
    "scenario",
]


def __getattr__(name: str) -> object:
    """Load the dual-runtime soak surface only when its extras are installed."""
    if name in {"SoakConfig", "SoakReport", "run_engine_soak"}:
        from voicey.testing import soak

        return getattr(soak, name)
    raise AttributeError(name)
