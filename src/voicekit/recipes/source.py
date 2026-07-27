"""Offline recipe sources and no-overwrite project installation."""

from __future__ import annotations

import importlib.resources
import os
import tempfile
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from voicekit.config.models import RuntimeName
from voicekit.errors import VoicekitError

_RUNTIME_DIRECTORIES = frozenset({"pipecat", "livekit"})


def recipe_files(name: str, runtime: RuntimeName) -> dict[str, str]:
    """Return shared files plus the selected native runtime variant."""
    root = _recipe_root(name)
    if not root.is_dir():
        raise VoicekitError("VK-CLI-005", detail=f"recipe {name!r} source is missing.")
    runtime_root = root.joinpath(runtime)
    if not runtime_root.is_dir():
        raise VoicekitError(
            "VK-CLI-005",
            detail=f"recipe {name!r} does not contain a {runtime} variant.",
        )

    rendered: dict[str, str] = {}
    for child in root.iterdir():
        if child.name in _RUNTIME_DIRECTORIES:
            continue
        _collect_files(child, PurePosixPath(child.name), rendered)
    for child in runtime_root.iterdir():
        _collect_files(child, PurePosixPath(child.name), rendered)
    if not rendered:
        raise VoicekitError("VK-CLI-005", detail=f"recipe {name!r} source is empty.")
    return rendered


def install_recipe(
    project_dir: Path,
    *,
    name: str,
    runtime: RuntimeName,
) -> tuple[Path, ...]:
    """Copy a recipe atomically, accepting identical files but never overwriting."""
    rendered = recipe_files(name, runtime)
    try:
        conflicts = [
            relative
            for relative, payload in rendered.items()
            if (project_dir / relative).exists()
            and (
                not (project_dir / relative).is_file()
                or (project_dir / relative).read_text(encoding="utf-8") != payload
            )
        ]
    except OSError as exc:
        raise VoicekitError(
            "VK-CLI-003",
            detail=f"could not inspect recipe paths in {project_dir}.",
        ) from exc
    if conflicts:
        raise VoicekitError(
            "VK-CLI-003",
            detail=(
                f"refusing to overwrite {conflicts[0]}; create a project with "
                f"`voicekit init --recipe {name}`."
            ),
        )

    written: list[Path] = []
    try:
        for relative, payload in rendered.items():
            destination = project_dir / relative
            if destination.exists():
                continue
            _write_new(destination, payload)
            written.append(destination)
    except Exception:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        raise
    return tuple(written)


def _recipe_root(name: str) -> Traversable:
    packaged = importlib.resources.files("voicekit").joinpath("recipe_data", name)
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).parents[3] / "recipes" / name
    return checkout


def _collect_files(
    source: Traversable,
    relative: PurePosixPath,
    rendered: dict[str, str],
) -> None:
    if source.name == "__pycache__" or source.name.startswith("."):
        return
    if source.is_dir():
        for child in source.iterdir():
            _collect_files(child, relative / child.name, rendered)
        return
    if not source.is_file():
        return
    destination = relative.as_posix()
    if relative.suffix in {".pyc", ".pyo"}:
        return
    if destination.startswith("../") or relative.is_absolute():
        raise VoicekitError("VK-SEC-002", detail=f"unsafe recipe path {destination!r}.")
    payload = source.read_text(encoding="utf-8")
    if destination in rendered and rendered[destination] != payload:
        raise VoicekitError(
            "VK-CLI-005",
            detail=f"recipe source contains conflicting {destination}.",
        )
    rendered[destination] = payload


def _write_new(path: Path, payload: str) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise VoicekitError("VK-CLI-003", detail=f"could not create {path}.") from exc
