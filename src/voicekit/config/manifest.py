"""Resumable JSON5 project manifest with atomic durable writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import json5
from pydantic import Field, ValidationError

from voicekit.config.models import ModelAxis, RuntimeName, VoicekitModel
from voicekit.errors import VoicekitError

ChannelName = Literal["phone", "web"]
DeployTarget = Literal[
    "docker",
    "pipecat-cloud",
    "livekit-cloud",
    "fly",
    "railway",
]


class RecipeSelection(VoicekitModel):
    """Recipe source copied into the project."""

    name: str
    version: str


class ManifestState(VoicekitModel):
    """Wizard and command checkpoint used for safe resume."""

    completed_steps: list[str] = Field(default_factory=list[str])
    last_command: str | None = None


class ProjectManifest(VoicekitModel):
    """Engine-owned record of project choices; never contains secret values."""

    schema_version: Literal[1] = 1
    project_name: str
    agent_module: str = "agent"
    runtime: RuntimeName
    recipe: RecipeSelection
    channels: Annotated[frozenset[ChannelName], Field(min_length=1)]
    models: dict[ModelAxis, str]
    carriers: list[str] = Field(default_factory=list[str])
    deploy_target: DeployTarget | None = None
    state: ManifestState = Field(default_factory=ManifestState)


class ManifestStore:
    """Read and atomically replace one voicekit.jsonc manifest."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ProjectManifest:
        """Parse JSON5 comments/trailing commas and validate the schema."""
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json5.loads(raw)
            return ProjectManifest.model_validate(parsed)
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            raise VoicekitError("VK-CFG-002", detail=f"{self.path}: {exc}") from exc

    def save(self, manifest: ProjectManifest) -> None:
        """Write, fsync, and atomically replace without exposing partial state."""
        payload = (
            "// Managed by voicekit. Secret values never belong in this file.\n"
            + json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n"
        )
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                text=True,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o644)
            os.replace(temporary_path, self.path)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise VoicekitError("VK-CFG-003", detail=f"{self.path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
