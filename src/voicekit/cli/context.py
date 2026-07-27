"""Project discovery and deterministic next-step guidance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voicekit.cli.checkpoint import InitCheckpointStore
from voicekit.cli.environment import EnvFileStore, merged_environment
from voicekit.cli.keys import required_entries
from voicekit.config.manifest import ManifestStore, ProjectManifest
from voicekit.errors import VoicekitError


@dataclass(frozen=True, slots=True)
class ProjectContext:
    root: Path
    manifest: ProjectManifest | None
    checkpoint: bool
    environment: dict[str, str]


def discover_project(start: Path, process_environment: dict[str, str]) -> ProjectContext:
    """Find the nearest manifest without importing untrusted project code."""
    root = _manifest_root(start.resolve())
    manifest_path = root / "voicekit.jsonc"
    manifest: ProjectManifest | None = None
    checkpoint = False
    if manifest_path.exists():
        try:
            manifest = ManifestStore(manifest_path).load()
        except VoicekitError:
            InitCheckpointStore(manifest_path).load()
            checkpoint = True
    file_values = EnvFileStore(root / ".env").read()
    return ProjectContext(
        root=root,
        manifest=manifest,
        checkpoint=checkpoint,
        environment=merged_environment(file_values, process_environment),
    )


def require_manifest(context: ProjectContext) -> ProjectManifest:
    if context.manifest is not None:
        return context.manifest
    next_step = "voicekit init --resume" if context.checkpoint else "voicekit init"
    raise VoicekitError(
        "VK-CLI-007",
        detail=f"no completed voicekit project is active. Next: `{next_step}`.",
    )


def next_step(context: ProjectContext) -> str:
    if context.checkpoint:
        return "voicekit init --resume"
    manifest = context.manifest
    if manifest is None:
        return "voicekit init"
    for entry in required_entries(
        cast("dict[str, str]", manifest.models),
        carrier=manifest.carriers[0] if manifest.carriers else None,
    ):
        missing = [name for name in entry.key_env_vars if not context.environment.get(name)]
        if missing:
            provider = entry.id.split("/", maxsplit=1)[0]
            return f"voicekit keys add {provider}"
    if not context.environment.get("VOICEKIT_WEBHOOK_SECRET"):
        return "voicekit doctor --fix"
    return "voicekit dev"


def _manifest_root(start: Path) -> Path:
    candidate = start if start.is_dir() else start.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "voicekit.jsonc").exists():
            return directory
    return candidate
