"""Pipecat 1.6 runtime bootstrap with dependency-isolated public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voicey.runtimes.pipecat.admission import AdmissionController, AdmissionLease
    from voicey.runtimes.pipecat.evals import run_eval_agent
    from voicey.runtimes.pipecat.host import DrainReport, PipecatHost, PipecatHostSettings
    from voicey.runtimes.pipecat.recording import PipecatRecordingHandler
    from voicey.runtimes.pipecat.session import (
        PipecatCall,
        PipecatSession,
        PipecatSessionBuilder,
    )

__all__ = [
    "AdmissionController",
    "AdmissionLease",
    "DrainReport",
    "PipecatCall",
    "PipecatHost",
    "PipecatHostSettings",
    "PipecatRecordingHandler",
    "PipecatSession",
    "PipecatSessionBuilder",
    "run_eval_agent",
]

_EXPORT_MODULES = {
    "AdmissionController": "voicey.runtimes.pipecat.admission",
    "AdmissionLease": "voicey.runtimes.pipecat.admission",
    "DrainReport": "voicey.runtimes.pipecat.host",
    "PipecatCall": "voicey.runtimes.pipecat.session",
    "PipecatHost": "voicey.runtimes.pipecat.host",
    "PipecatHostSettings": "voicey.runtimes.pipecat.host",
    "PipecatRecordingHandler": "voicey.runtimes.pipecat.recording",
    "PipecatSession": "voicey.runtimes.pipecat.session",
    "PipecatSessionBuilder": "voicey.runtimes.pipecat.session",
    "run_eval_agent": "voicey.runtimes.pipecat.evals",
}


def __getattr__(name: str) -> Any:
    """Load a Pipecat-backed export only when the caller asks for it."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
