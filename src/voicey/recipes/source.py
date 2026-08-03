"""Offline recipe sources and no-overwrite project installation."""

from __future__ import annotations

import importlib.resources
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import cast

from voicey.config.models import RuntimeName
from voicey.errors import VoiceyError

_RUNTIME_DIRECTORIES = frozenset({"pipecat", "livekit"})
RECIPE_LOCK_NAME = "voicey.recipe-lock.json"


@dataclass(frozen=True, slots=True)
class RecipeBaseline:
    """Committed copy of the exact upstream recipe source originally installed."""

    schema_version: int
    name: str
    version: str
    runtime: RuntimeName
    files: dict[str, str]

    @classmethod
    def from_payload(cls, payload: object) -> RecipeBaseline:
        if not isinstance(payload, dict):
            raise VoiceyError("VY-UPG-003", detail="recipe baseline is not an object.")
        mapping = cast("dict[str, object]", payload)
        if set(mapping) != {"schema_version", "name", "version", "runtime", "files"}:
            raise VoiceyError("VY-UPG-003", detail="recipe baseline fields are invalid.")
        files = mapping["files"]
        runtime = mapping["runtime"]
        if (
            mapping["schema_version"] != 1
            or not isinstance(mapping["name"], str)
            or not mapping["name"]
            or not isinstance(mapping["version"], str)
            or not mapping["version"]
            or runtime not in {"pipecat", "livekit"}
            or not isinstance(files, dict)
            or not files
        ):
            raise VoiceyError("VY-UPG-003", detail="recipe baseline values are invalid.")
        normalized: dict[str, str] = {}
        for path, contents in cast("dict[object, object]", files).items():
            if not isinstance(path, str) or not isinstance(contents, str):
                raise VoiceyError(
                    "VY-UPG-003",
                    detail="recipe baseline source entries are invalid.",
                )
            _validate_relative_path(path)
            normalized[path] = contents
        return cls(
            schema_version=1,
            name=mapping["name"],
            version=mapping["version"],
            runtime=cast("RuntimeName", runtime),
            files=normalized,
        )


class RecipeBaselineStore:
    """Atomic public project metadata; it contains authored source, never secrets."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RecipeBaseline | None:
        if self.path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(self.path))
        if not self.path.exists():
            return None
        try:
            return RecipeBaseline.from_payload(json.loads(self.path.read_text(encoding="utf-8")))
        except VoiceyError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceyError(
                "VY-UPG-003",
                detail=f"could not read recipe baseline {self.path}.",
            ) from exc

    def save(self, baseline: RecipeBaseline) -> None:
        if self.path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(self.path))
        payload = json.dumps(asdict(baseline), indent=2, sort_keys=True) + "\n"
        _write_atomic(self.path, payload)


def build_recipe_baseline(
    name: str,
    version: str,
    runtime: RuntimeName,
) -> RecipeBaseline:
    """Capture the installed upstream source before project customization."""
    return RecipeBaseline(
        schema_version=1,
        name=name,
        version=version,
        runtime=runtime,
        files=recipe_files(name, runtime),
    )


def render_recipe_baseline(
    name: str,
    version: str,
    runtime: RuntimeName,
) -> str:
    """Render deterministic tracked project metadata for scaffold transactions."""
    return (
        json.dumps(
            asdict(build_recipe_baseline(name, version, runtime)),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def recipe_files(name: str, runtime: RuntimeName) -> dict[str, str]:
    """Return shared files plus the selected native runtime variant."""
    root = _recipe_root(name)
    if not root.is_dir():
        raise VoiceyError("VY-CLI-005", detail=f"recipe {name!r} source is missing.")
    runtime_root = root.joinpath(runtime)
    if not runtime_root.is_dir():
        raise VoiceyError(
            "VY-CLI-005",
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
        raise VoiceyError("VY-CLI-005", detail=f"recipe {name!r} source is empty.")
    return rendered


def install_recipe(
    project_dir: Path,
    *,
    name: str,
    version: str,
    runtime: RuntimeName,
) -> tuple[Path, ...]:
    """Copy a recipe atomically, accepting identical files but never overwriting."""
    rendered = recipe_files(name, runtime)
    rendered[RECIPE_LOCK_NAME] = render_recipe_baseline(name, version, runtime)
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
        raise VoiceyError(
            "VY-CLI-003",
            detail=f"could not inspect recipe paths in {project_dir}.",
        ) from exc
    if conflicts:
        raise VoiceyError(
            "VY-CLI-003",
            detail=(
                f"refusing to overwrite {conflicts[0]}; create a project with "
                f"`voicey init --recipe {name}`."
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
    packaged = importlib.resources.files("voicey").joinpath("recipe_data", name)
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
        raise VoiceyError("VY-SEC-002", detail=f"unsafe recipe path {destination!r}.")
    payload = source.read_text(encoding="utf-8")
    if destination in rendered and rendered[destination] != payload:
        raise VoiceyError(
            "VY-CLI-005",
            detail=f"recipe source contains conflicting {destination}.",
        )
    rendered[destination] = payload


def _write_new(path: Path, payload: str) -> None:
    if path.exists():
        raise VoiceyError("VY-CLI-003", detail=f"refusing to overwrite {path}.")
    _write_atomic(path, payload)


def _write_atomic(path: Path, payload: str) -> None:
    temporary_path: Path | None = None
    try:
        if path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(path))
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
        temporary_path.chmod(0o644)
        os.replace(temporary_path, path)
    except VoiceyError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise VoiceyError("VY-CLI-003", detail=f"could not create {path}.") from exc


def _validate_relative_path(value: str) -> None:
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or relative.is_absolute()
        or ".." in relative.parts
        or value.startswith("./")
    ):
        raise VoiceyError("VY-UPG-003", detail=f"unsafe recipe baseline path {value!r}.")


__all__ = [
    "RECIPE_LOCK_NAME",
    "RecipeBaseline",
    "RecipeBaselineStore",
    "build_recipe_baseline",
    "install_recipe",
    "recipe_files",
    "render_recipe_baseline",
]
