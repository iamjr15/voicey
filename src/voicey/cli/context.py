"""Project discovery and deterministic next-step guidance."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voicey.cli.checkpoint import InitCheckpointStore
from voicey.cli.environment import EnvFileStore, merged_environment
from voicey.cli.keys import required_entries
from voicey.config.manifest import ManifestStore, ProjectManifest
from voicey.config.models import Agent
from voicey.errors import VoiceyError


@dataclass(frozen=True, slots=True)
class ProjectContext:
    root: Path
    manifest: ProjectManifest | None
    checkpoint: bool
    environment: dict[str, str]


def discover_project(start: Path, process_environment: dict[str, str]) -> ProjectContext:
    """Find the nearest manifest without importing untrusted project code."""
    root = _manifest_root(start.resolve())
    manifest_path = root / "voicey.jsonc"
    manifest: ProjectManifest | None = None
    checkpoint = False
    if manifest_path.exists():
        try:
            manifest = ManifestStore(manifest_path).load()
        except VoiceyError:
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
    next_step = "voicey init --resume" if context.checkpoint else "voicey init"
    raise VoiceyError(
        "VY-CLI-007",
        detail=f"no completed voicey project is active. Next: `{next_step}`.",
    )


def load_project_agent(context: ProjectContext) -> Agent:
    """Import the configured project Agent with its root and environment active."""
    manifest = require_manifest(context)
    try:
        with _project_import_path(context.root), _project_environment(context.environment):
            importlib.invalidate_caches()
            existing = sys.modules.get(manifest.agent_module)
            existing_file = getattr(existing, "__file__", None)
            if existing is not None and (
                existing_file is None
                or not Path(existing_file).resolve().is_relative_to(context.root)
            ):
                del sys.modules[manifest.agent_module]
                existing = None
            module = (
                importlib.import_module(manifest.agent_module)
                if existing is None
                else importlib.reload(existing)
            )
            agent: object = module.agent
    except (ImportError, AttributeError) as exc:
        raise VoiceyError(
            "VY-CLI-007",
            detail=f"{manifest.agent_module}.py must export an Agent named `agent`.",
        ) from exc
    except Exception as exc:
        raise VoiceyError(
            "VY-CLI-007",
            detail=f"{manifest.agent_module}.py failed to load ({type(exc).__name__}).",
        ) from exc
    if not isinstance(agent, Agent):
        raise VoiceyError(
            "VY-CLI-007",
            detail=f"{manifest.agent_module}.agent is not a voicey Agent.",
        )
    if agent.runtime != manifest.runtime:
        raise VoiceyError(
            "VY-CLI-007",
            detail="voicey.jsonc and agent.py select different runtimes.",
        )
    return agent


def next_step(context: ProjectContext) -> str:
    if context.checkpoint:
        return "voicey init --resume"
    manifest = context.manifest
    if manifest is None:
        return "voicey init"
    for entry in required_entries(
        cast("dict[str, str]", manifest.models),
        carrier=manifest.carriers[0] if manifest.carriers else None,
    ):
        missing = [name for name in entry.key_env_vars if not context.environment.get(name)]
        if missing:
            provider = entry.id.split("/", maxsplit=1)[0]
            return f"voicey keys add {provider}"
    if not context.environment.get("VOICEY_WEBHOOK_SECRET"):
        return "voicey doctor --fix"
    return "voicey dev"


def _manifest_root(start: Path) -> Path:
    candidate = start if start.is_dir() else start.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "voicey.jsonc").exists():
            return directory
    return candidate


@contextmanager
def _project_import_path(root: Path) -> Generator[None, None, None]:
    text = str(root)
    sys.path.insert(0, text)
    try:
        yield
    finally:
        with suppress(ValueError):
            sys.path.remove(text)


@contextmanager
def _project_environment(values: dict[str, str]) -> Generator[None, None, None]:
    original = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
