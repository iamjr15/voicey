"""Three-way recipe drift analysis with no project-source writes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voicekit.config.manifest import ProjectManifest
from voicekit.errors import VoicekitError
from voicekit.recipes.registry import DEFAULT_RECIPE_REGISTRY, RecipeRegistry
from voicekit.recipes.source import (
    RECIPE_LOCK_NAME,
    RecipeBaseline,
    RecipeBaselineStore,
    recipe_files,
)

DriftStatus = Literal[
    "unchanged",
    "local-only",
    "upstream-only",
    "converged",
    "conflict",
]
RecipeStatus = Literal[
    "scratch",
    "current",
    "update-available",
    "ahead",
    "baseline-missing",
]
BaselineSource = Literal["tracked", "reconstructed-current", "missing", "not-applicable"]
_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True, slots=True)
class RecipeFileDrift:
    """One recipe-owned path compared across base, local, and upstream."""

    path: str
    status: DriftStatus
    base_sha256: str | None
    local_sha256: str | None
    upstream_sha256: str | None


@dataclass(frozen=True, slots=True)
class RecipeDriftReport:
    """Machine-readable non-mutating update report."""

    recipe: str
    runtime: str
    installed_version: str
    upstream_version: str | None
    status: RecipeStatus
    baseline_source: BaselineSource
    files: tuple[RecipeFileDrift, ...]
    local_changes: int
    upstream_changes: int
    conflicts: int
    ai_merge_prompt: str | None
    next_step: str


class RecipeDriftAnalyzer:
    """Compare tracked baseline, current project files, and packaged upstream."""

    def __init__(
        self,
        project_root: Path,
        *,
        registry: RecipeRegistry = DEFAULT_RECIPE_REGISTRY,
    ) -> None:
        self.project_root = project_root.resolve()
        self.registry = registry
        self.store = RecipeBaselineStore(self.project_root / RECIPE_LOCK_NAME)

    def analyze(self, manifest: ProjectManifest) -> RecipeDriftReport:
        selection = manifest.recipe
        if selection.name == "scratch":
            return RecipeDriftReport(
                recipe="scratch",
                runtime=manifest.runtime,
                installed_version=selection.version,
                upstream_version=None,
                status="scratch",
                baseline_source="not-applicable",
                files=(),
                local_changes=0,
                upstream_changes=0,
                conflicts=0,
                ai_merge_prompt=None,
                next_step="voicekit doctor",
            )

        upstream_definition = self.registry.require(selection.name, manifest.runtime)
        upstream = recipe_files(selection.name, manifest.runtime)
        baseline, baseline_source = self._baseline(
            manifest,
            upstream,
            upstream_definition.version,
        )
        rows = tuple(
            self._compare_path(
                path,
                None if baseline is None else baseline.files.get(path),
                upstream.get(path),
            )
            for path in sorted(set(upstream) | (set() if baseline is None else set(baseline.files)))
        )
        local_changes = sum(row.status in {"local-only", "converged", "conflict"} for row in rows)
        upstream_changes = sum(
            row.status in {"upstream-only", "converged", "conflict"} for row in rows
        )
        conflicts = sum(row.status == "conflict" for row in rows)
        status = self._status(
            selection.version,
            upstream_definition.version,
            baseline_source,
        )
        prompt = (
            self._merge_prompt(
                manifest,
                upstream_definition.version,
                rows,
            )
            if status == "update-available" or conflicts
            else None
        )
        next_step = (
            "review the AI merge guidance, apply the recipe update, then run voicekit test"
            if status == "update-available"
            else "review voicekit.recipe-lock.json and restore the missing baseline"
            if status == "baseline-missing"
            else "review the AI merge guidance, then run voicekit test"
            if conflicts
            else "voicekit doctor"
        )
        return RecipeDriftReport(
            recipe=selection.name,
            runtime=manifest.runtime,
            installed_version=selection.version,
            upstream_version=upstream_definition.version,
            status=status,
            baseline_source=baseline_source,
            files=rows,
            local_changes=local_changes,
            upstream_changes=upstream_changes,
            conflicts=conflicts,
            ai_merge_prompt=prompt,
            next_step=next_step,
        )

    def _baseline(
        self,
        manifest: ProjectManifest,
        upstream: dict[str, str],
        upstream_version: str,
    ) -> tuple[RecipeBaseline | None, BaselineSource]:
        baseline = self.store.load()
        if baseline is None:
            if manifest.recipe.version == upstream_version:
                return (
                    RecipeBaseline(
                        schema_version=1,
                        name=manifest.recipe.name,
                        version=manifest.recipe.version,
                        runtime=manifest.runtime,
                        files=upstream,
                    ),
                    "reconstructed-current",
                )
            return None, "missing"
        if (
            baseline.name != manifest.recipe.name
            or baseline.version != manifest.recipe.version
            or baseline.runtime != manifest.runtime
        ):
            raise VoicekitError(
                "VK-UPG-003",
                detail=(
                    "voicekit.recipe-lock.json does not match the manifest recipe, "
                    "version, or runtime."
                ),
            )
        return baseline, "tracked"

    def _compare_path(
        self,
        path: str,
        base: str | None,
        upstream: str | None,
    ) -> RecipeFileDrift:
        local = self._local_source(path)
        if local == base and upstream == base:
            status: DriftStatus = "unchanged"
        elif local == upstream and local != base:
            status = "converged"
        elif local != base and upstream == base:
            status = "local-only"
        elif local == base and upstream != base:
            status = "upstream-only"
        else:
            status = "conflict"
        return RecipeFileDrift(
            path=path,
            status=status,
            base_sha256=_digest(base),
            local_sha256=_digest(local),
            upstream_sha256=_digest(upstream),
        )

    def _local_source(self, relative: str) -> str | None:
        path = self.project_root / relative
        if path.is_symlink():
            raise VoicekitError("VK-SEC-002", detail=str(path))
        if not path.exists():
            return None
        if not path.is_file():
            raise VoicekitError(
                "VK-UPG-003",
                detail=f"recipe-owned path {relative!r} is not a regular file.",
            )
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise VoicekitError(
                "VK-UPG-003",
                detail=f"could not inspect recipe-owned path {relative!r}.",
            ) from exc

    @staticmethod
    def _status(
        installed: str,
        upstream: str,
        baseline_source: BaselineSource,
    ) -> RecipeStatus:
        if baseline_source == "missing":
            return "baseline-missing"
        installed_key = _semver_key(installed)
        upstream_key = _semver_key(upstream)
        if installed_key == upstream_key:
            return "current"
        return "update-available" if installed_key < upstream_key else "ahead"

    @staticmethod
    def _merge_prompt(
        manifest: ProjectManifest,
        upstream_version: str,
        rows: tuple[RecipeFileDrift, ...],
    ) -> str:
        changed = [row for row in rows if row.status != "unchanged"]
        facts = "\n".join(f"- {row.path}: {row.status}" for row in changed)
        return (
            "Merge this voicekit recipe update without overwriting project code.\n"
            f"Recipe: {manifest.recipe.name} {manifest.recipe.version} -> "
            f"{upstream_version}; runtime: {manifest.runtime}.\n"
            "The base sources are in voicekit.recipe-lock.json; local sources are "
            "the project files; upstream sources are the installed voicekit "
            f"recipe_data/{manifest.recipe.name} package.\n"
            "Preserve local integrations, prompts, policies, and typed tool behavior. "
            "Keep conversation logic native to the selected runtime; introduce no "
            "flow DSL and no MCP. Never copy secrets or overwrite a file wholesale. "
            "Merge each hunk, run the recipe scenario suite and `voicekit test`, then "
            "update the manifest and recipe lock only after review.\n"
            f"Changed paths:\n{facts}"
        )


def _digest(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()


def _semver_key(value: str) -> tuple[int, int, int, int, tuple[str, ...]]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise VoicekitError("VK-UPG-003", detail=f"recipe version {value!r} is not SemVer.")
    prerelease = match.group("pre")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        1 if prerelease is None else 0,
        () if prerelease is None else tuple(prerelease.split(".")),
    )


__all__ = [
    "RecipeDriftAnalyzer",
    "RecipeDriftReport",
    "RecipeFileDrift",
]
