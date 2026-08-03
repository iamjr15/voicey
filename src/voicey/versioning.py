"""SemVer transition and deprecation policy for Voicey's public surface."""

from __future__ import annotations

import functools
import inspect
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import ParamSpec, TypeVar, cast

_SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, order=True, slots=True)
class SemVer:
    """Strict Semantic Versioning 2.0.0 representation."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    build: str | None = None

    @classmethod
    def parse(cls, raw: str) -> SemVer:
        match = _SEMVER_PATTERN.fullmatch(raw)
        if match is None:
            msg = f"{raw!r} is not a valid Semantic Versioning 2.0.0 version."
            raise ValueError(msg)
        return cls(
            major=int(match["major"]),
            minor=int(match["minor"]),
            patch=int(match["patch"]),
            prerelease=match["prerelease"],
            build=match["build"],
        )

    @property
    def public(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch


@dataclass(frozen=True, slots=True)
class Deprecation:
    """A public deprecation with its enforceable removal horizon."""

    name: str
    since: str
    remove_in: str
    replacement: str
    migration_url: str

    def __post_init__(self) -> None:
        since = SemVer.parse(self.since)
        removal = SemVer.parse(self.remove_in)
        if since.prerelease is not None or removal.prerelease is not None:
            msg = "Deprecation horizons must use stable SemVer releases."
            raise ValueError(msg)
        distance = (removal.major - since.major) * 1_000_000 + removal.minor - since.minor
        if removal.public <= since.public or distance < 2:
            msg = (
                f"{self.name} removal in {self.remove_in} violates the minimum "
                f"two-minor window after {self.since}."
            )
            raise ValueError(msg)

    @property
    def message(self) -> str:
        return (
            f"{self.name} is deprecated since Voicey {self.since} and will be removed "
            f"in {self.remove_in}. Use {self.replacement}. Migration: {self.migration_url}"
        )


class VoiceyDeprecationWarning(FutureWarning):
    """A supported public API is scheduled for removal."""


DEPRECATIONS: tuple[Deprecation, ...] = ()


def warn_deprecated(deprecation: Deprecation, *, stacklevel: int = 2) -> None:
    """Emit Voicey's standard actionable deprecation warning."""
    warnings.warn(
        deprecation.message,
        VoiceyDeprecationWarning,
        stacklevel=stacklevel,
    )


def deprecated(
    deprecation: Deprecation,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a synchronous or asynchronous public callable."""

    def decorator(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                warn_deprecated(deprecation, stacklevel=3)
                return await function(*args, **kwargs)  # type: ignore[misc]

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            warn_deprecated(deprecation, stacklevel=3)
            return function(*args, **kwargs)

        return wrapper

    return decorator


def validate_deprecations(
    current_version: str,
    *,
    registry: tuple[Deprecation, ...] = DEPRECATIONS,
) -> None:
    """Fail release preparation if a registered removal is overdue."""
    current = SemVer.parse(current_version)
    for entry in registry:
        removal = SemVer.parse(entry.remove_in)
        if current.public >= removal.public:
            msg = (
                f"{entry.name} reached removal version {entry.remove_in}; "
                "remove it or extend the policy with a spec amendment."
            )
            raise ValueError(msg)
