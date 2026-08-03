"""Runtime compatibility policy and non-fatal version diagnostics."""

from __future__ import annotations

import importlib.metadata
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from voicey.config.models import RuntimeName

CompatibilityStatus: TypeAlias = Literal["supported", "out-of-range", "missing", "invalid"]
VersionProvider: TypeAlias = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """One runtime's empirically certified compatibility window."""

    runtime: RuntimeName
    distribution: str
    supported: str
    tested_versions: tuple[str, ...]
    extra: str

    def __post_init__(self) -> None:
        specifier = SpecifierSet(self.supported)
        if not self.tested_versions:
            msg = f"{self.runtime} must declare at least one empirically tested version."
            raise ValueError(msg)
        for raw_version in self.tested_versions:
            if Version(raw_version) not in specifier:
                msg = (
                    f"{self.runtime} tested version {raw_version} is outside "
                    f"its supported window {self.supported}."
                )
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RuntimeCompatibilityReport:
    """Installed runtime state suitable for hosts, doctor, and release gates."""

    runtime: RuntimeName
    distribution: str
    supported_range: str
    installed_version: str | None
    status: CompatibilityStatus

    @property
    def supported(self) -> bool:
        return self.status == "supported"

    @property
    def warning(self) -> str | None:
        if self.status == "supported":
            return None
        if self.status == "missing":
            state = "is not installed"
        elif self.status == "invalid":
            state = f"reported an invalid version {self.installed_version!r}"
        else:
            state = (
                f"version {self.installed_version} is outside the certified "
                f"range {self.supported_range}"
            )
        return (
            f"{self.distribution} {state}. Voicey will continue, but this runtime "
            "combination is not certified. See docs/compatibility.md and run "
            f'`uv pip install "voicey[{self.runtime}]"`.'
        )


class RuntimeCompatibilityWarning(UserWarning):
    """A runtime dependency is usable but outside the certified range."""


RUNTIME_REQUIREMENTS: dict[RuntimeName, RuntimeRequirement] = {
    "pipecat": RuntimeRequirement(
        runtime="pipecat",
        distribution="pipecat-ai",
        supported=">=1.6.0,<1.6.1",
        tested_versions=("1.6.0",),
        extra="pipecat",
    ),
    "livekit": RuntimeRequirement(
        runtime="livekit",
        distribution="livekit-agents",
        supported=">=1.6.7,<1.6.8",
        tested_versions=("1.6.7",),
        extra="livekit",
    ),
}


def inspect_runtime_compatibility(
    runtime: RuntimeName,
    *,
    version_provider: VersionProvider = importlib.metadata.version,
) -> RuntimeCompatibilityReport:
    """Inspect one installed runtime without turning version drift into an outage."""
    requirement = RUNTIME_REQUIREMENTS[runtime]
    try:
        installed = version_provider(requirement.distribution)
    except importlib.metadata.PackageNotFoundError:
        return RuntimeCompatibilityReport(
            runtime=runtime,
            distribution=requirement.distribution,
            supported_range=requirement.supported,
            installed_version=None,
            status="missing",
        )
    try:
        parsed = Version(installed)
    except InvalidVersion:
        status: CompatibilityStatus = "invalid"
    else:
        status = "supported" if parsed in SpecifierSet(requirement.supported) else "out-of-range"
    return RuntimeCompatibilityReport(
        runtime=runtime,
        distribution=requirement.distribution,
        supported_range=requirement.supported,
        installed_version=installed,
        status=status,
    )


def warn_runtime_compatibility(
    runtime: RuntimeName,
    *,
    version_provider: VersionProvider = importlib.metadata.version,
    stacklevel: int = 2,
) -> RuntimeCompatibilityReport:
    """Emit a loud warning for uncertified versions while allowing startup."""
    report = inspect_runtime_compatibility(runtime, version_provider=version_provider)
    if report.warning is not None:
        warnings.warn(
            report.warning,
            RuntimeCompatibilityWarning,
            stacklevel=stacklevel,
        )
    return report
