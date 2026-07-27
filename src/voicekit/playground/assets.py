"""Zip-safe access to the wheel-embedded playground."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from voicekit.errors import VoicekitError


@contextmanager
def embedded_frontend() -> Generator[Path, None, None]:
    """Materialize the bundled SPA for the complete server lifetime."""
    resource = files("voicekit").joinpath("_frontend")
    with as_file(resource) as path:
        index = path / "index.html"
        if not path.is_dir() or not index.is_file():
            raise VoicekitError(
                "VK-WEB-005",
                detail="the installed wheel does not contain the playground build.",
            )
        yield path
