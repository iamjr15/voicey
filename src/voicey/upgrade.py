"""Lockfile-only engine upgrades with rollback and fresh-process drift analysis."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from packaging.version import InvalidVersion, Version

from voicey import __version__
from voicey.config.manifest import ProjectManifest
from voicey.errors import VoiceyError
from voicey.recipes.registry import DEFAULT_RECIPE_REGISTRY
from voicey.recipes.source import (
    RECIPE_LOCK_NAME,
    RecipeBaseline,
    RecipeBaselineStore,
    build_recipe_baseline,
)

_UV_MINIMUM = (0, 11, 0)
_UV_MAXIMUM = (1, 0, 0)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True, slots=True)
class UpgradeCommandResult:
    returncode: int
    stdout: str
    stderr: str


class UpgradeCommandRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1200,
    ) -> UpgradeCommandResult: ...


class UvCliRunner:
    """Bounded uv command boundary that never reflects index credentials."""

    def __init__(self, executable: str | None = None) -> None:
        selected = executable or shutil.which("uv")
        if selected is None:
            raise VoiceyError(
                "VY-UPG-001",
                detail="the `uv` executable is unavailable; install uv >=0.11,<1.",
            )
        self.executable = selected

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1200,
    ) -> UpgradeCommandResult:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceyError(
                "VY-UPG-002",
                detail=f"uv execution failed ({type(exc).__name__}).",
            ) from exc
        result = UpgradeCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            rendered = arguments[0] if arguments else "<missing-command>"
            raise VoiceyError(
                "VY-UPG-002",
                detail=f"`uv {rendered}` failed with exit {result.returncode}.",
            )
        return result


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """Safe facts returned after the new environment reports recipe drift."""

    from_version: str
    to_version: str
    channel: str
    changed: bool
    lockfile: str
    pyproject_unchanged: bool
    recipe_sources_unchanged: bool
    recipe_drift: dict[str, object]
    next_step: str


class UpgradeManager:
    """Upgrade only voicey's lock entry, sync, and inspect with the new process."""

    def __init__(
        self,
        project_root: Path,
        *,
        runner: UpgradeCommandRunner | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runner = runner or UvCliRunner()
        self.pyproject = self.project_root / "pyproject.toml"
        self.lockfile = self.project_root / "uv.lock"

    def upgrade(
        self,
        manifest: ProjectManifest,
        *,
        prerelease: bool,
    ) -> UpgradeReport:
        pyproject_before = self._validate_project()
        self._validate_uv()
        baseline = self._ensure_baseline(manifest)
        recipe_before = self._recipe_digest(baseline)
        if self.lockfile.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(self.lockfile))
        lock_existed = self.lockfile.exists()
        lock_before = self._read_regular(self.lockfile) if lock_existed else None
        mode = "allow" if prerelease else "if-necessary-or-explicit"
        try:
            self.runner.run(
                [
                    "lock",
                    "--upgrade-package",
                    "voicey",
                    "--prerelease",
                    mode,
                ],
                cwd=self.project_root,
            )
            locked_version = self._locked_voicey_version()
            if not prerelease and _is_prerelease(locked_version):
                raise VoiceyError(
                    "VY-UPG-002",
                    detail=(
                        "stable resolution selected a voicey prerelease; "
                        "the prior lockfile was restored."
                    ),
                )
            self.runner.run(
                ["sync", "--locked", "--prerelease", mode],
                cwd=self.project_root,
            )
            drift_result = self.runner.run(
                [
                    "run",
                    "--locked",
                    "--prerelease",
                    mode,
                    "voicey",
                    "recipes",
                    "update-check",
                    "--json",
                ],
                cwd=self.project_root,
            )
            drift = _parse_drift(drift_result.stdout)
        except Exception:
            self._restore_and_sync(lock_existed, lock_before)
            raise

        pyproject_after = self._read_regular(self.pyproject)
        recipe_after = self._recipe_digest(baseline)
        if pyproject_after != pyproject_before or recipe_after != recipe_before:
            self._restore_and_sync(lock_existed, lock_before)
            raise VoiceyError(
                "VY-UPG-002",
                detail=(
                    "upgrade changed pyproject.toml or recipe-owned project source; "
                    "the prior lockfile was restored."
                ),
            )
        return UpgradeReport(
            from_version=__version__,
            to_version=locked_version,
            channel="canary" if prerelease else "stable",
            changed=locked_version != __version__,
            lockfile=str(self.lockfile),
            pyproject_unchanged=True,
            recipe_sources_unchanged=True,
            recipe_drift=drift,
            next_step=str(drift.get("next_step") or "voicey doctor"),
        )

    def _validate_project(self) -> bytes:
        if not self.pyproject.is_file() or self.pyproject.is_symlink():
            raise VoiceyError(
                "VY-UPG-001",
                detail="upgrade requires a regular project pyproject.toml.",
            )
        payload = self._read_regular(self.pyproject)
        try:
            parsed = tomllib.loads(payload.decode())
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise VoiceyError(
                "VY-UPG-001",
                detail="project pyproject.toml is not valid UTF-8 TOML.",
            ) from exc
        parsed_mapping = cast("dict[str, object]", parsed)
        project = parsed_mapping.get("project")
        project_mapping = cast("dict[str, object]", project) if isinstance(project, dict) else {}
        dependencies = project_mapping.get("dependencies")
        if not isinstance(dependencies, list) or not any(
            isinstance(value, str) and _dependency_name(value) == "voicey"
            for value in cast("list[object]", dependencies)
        ):
            raise VoiceyError(
                "VY-UPG-001",
                detail="project.dependencies must contain a direct voicey requirement.",
            )
        return payload

    def _validate_uv(self) -> None:
        value = self.runner.run(["--version"], cwd=self.project_root, timeout_s=30).stdout
        match = _VERSION.search(value)
        if match is None:
            raise VoiceyError("VY-UPG-001", detail="uv did not report a semantic version.")
        version = tuple(int(part) for part in match.groups())
        if not _UV_MINIMUM <= version < _UV_MAXIMUM:
            raise VoiceyError(
                "VY-UPG-001",
                detail="voicey upgrade requires uv >=0.11,<1.",
            )

    def _ensure_baseline(self, manifest: ProjectManifest) -> RecipeBaseline | None:
        if manifest.recipe.name == "scratch":
            return None
        store = RecipeBaselineStore(self.project_root / RECIPE_LOCK_NAME)
        baseline = store.load()
        if baseline is not None:
            if (
                baseline.name != manifest.recipe.name
                or baseline.version != manifest.recipe.version
                or baseline.runtime != manifest.runtime
            ):
                raise VoiceyError(
                    "VY-UPG-003",
                    detail="recipe baseline does not match the active manifest.",
                )
            return baseline
        definition = DEFAULT_RECIPE_REGISTRY.require(
            manifest.recipe.name,
            manifest.runtime,
        )
        if definition.version != manifest.recipe.version:
            raise VoiceyError(
                "VY-UPG-003",
                detail=(
                    "the original recipe baseline is missing and the manifest "
                    "version differs from the installed recipe."
                ),
            )
        baseline = build_recipe_baseline(
            manifest.recipe.name,
            manifest.recipe.version,
            manifest.runtime,
        )
        store.save(baseline)
        return baseline

    def _recipe_digest(self, baseline: RecipeBaseline | None) -> str:
        if baseline is None:
            return hashlib.sha256(b"scratch").hexdigest()
        digest = hashlib.sha256()
        for relative in sorted(baseline.files):
            path = self.project_root / relative
            if path.is_symlink():
                raise VoiceyError("VY-SEC-002", detail=str(path))
            digest.update(relative.encode())
            digest.update(b"\0")
            if path.exists():
                if not path.is_file():
                    raise VoiceyError(
                        "VY-UPG-003",
                        detail=f"recipe-owned path {relative!r} is not a regular file.",
                    )
                digest.update(self._read_regular(path))
            digest.update(b"\0")
        return digest.hexdigest()

    def _locked_voicey_version(self) -> str:
        if not self.lockfile.is_file() or self.lockfile.is_symlink():
            raise VoiceyError(
                "VY-UPG-002",
                detail="uv did not produce a regular uv.lock.",
            )
        try:
            parsed = tomllib.loads(self._read_regular(self.lockfile).decode())
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise VoiceyError("VY-UPG-002", detail="uv.lock is invalid.") from exc
        packages = cast("dict[str, object]", parsed).get("package")
        if not isinstance(packages, list):
            raise VoiceyError("VY-UPG-002", detail="uv.lock has no package table.")
        versions: set[str] = set()
        for item in cast("list[object]", packages):
            if not isinstance(item, dict):
                continue
            item_mapping = cast("dict[str, object]", item)
            name = item_mapping.get("name")
            version = item_mapping.get("version")
            if name == "voicey" and isinstance(version, str):
                versions.add(version)
        if len(versions) != 1:
            raise VoiceyError(
                "VY-UPG-002",
                detail="uv.lock does not identify one voicey version.",
            )
        return versions.pop()

    def _restore_lock(self, existed: bool, payload: bytes | None) -> None:
        if existed:
            if payload is None:
                raise AssertionError("existing lockfile backup is missing")
            _atomic_write(self.lockfile, payload)
        else:
            self.lockfile.unlink(missing_ok=True)

    def _restore_and_sync(self, existed: bool, payload: bytes | None) -> None:
        self._restore_lock(existed, payload)
        if existed:
            with suppress(Exception):
                self.runner.run(
                    ["sync", "--locked"],
                    cwd=self.project_root,
                    check=False,
                )

    @staticmethod
    def _read_regular(path: Path) -> bytes:
        if path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(path))
        try:
            return path.read_bytes()
        except OSError as exc:
            raise VoiceyError(
                "VY-UPG-001",
                detail=f"could not read required project file {path}.",
            ) from exc


def _dependency_name(requirement: str) -> str:
    return re.split(r"\[|[<>=!~; @]", requirement.strip(), maxsplit=1)[0].casefold()


def _is_prerelease(value: str) -> bool:
    try:
        return Version(value).is_prerelease
    except InvalidVersion as exc:
        raise VoiceyError(
            "VY-UPG-002",
            detail="uv.lock contains an invalid voicey version.",
        ) from exc


def _parse_drift(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise VoiceyError(
            "VY-UPG-002",
            detail="upgraded recipe drift command returned invalid JSON.",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise VoiceyError(
            "VY-UPG-002",
            detail="upgraded recipe drift report has an invalid shape.",
        )
    return cast("dict[str, object]", payload)


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise VoiceyError("VY-SEC-002", detail=str(path))
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise VoiceyError("VY-UPG-002", detail=f"could not restore {path}.") from exc


__all__ = [
    "UpgradeCommandResult",
    "UpgradeCommandRunner",
    "UpgradeManager",
    "UpgradeReport",
    "UvCliRunner",
]
