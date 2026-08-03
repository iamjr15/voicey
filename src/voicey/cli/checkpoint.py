"""Secret-free wizard checkpoint stored in voicey.jsonc until completion."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import json5
from pydantic import Field, JsonValue, ValidationError

from voicey.config.models import VoiceyModel
from voicey.errors import VoiceyError


class InitCheckpoint(VoiceyModel):
    """Partial explicit answers; credentials are deliberately impossible here."""

    schema_version: Literal[1] = 1
    kind: Literal["init-checkpoint"] = "init-checkpoint"
    project_name: str
    answers: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    completed_steps: list[str] = Field(default_factory=list[str])


class InitCheckpointStore:
    """Atomically persist and resume an interrupted setup."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> InitCheckpoint:
        try:
            return InitCheckpoint.model_validate(json5.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            raise VoiceyError(
                "VY-CLI-002",
                detail=f"{self.path} is not a resumable init checkpoint.",
            ) from exc

    def save(self, checkpoint: InitCheckpoint) -> None:
        payload = (
            "// Incomplete voicey setup. Resume with: voicey init --resume\n"
            + json.dumps(checkpoint.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n"
        )
        _atomic_replace(self.path, payload, mode=0o644)


def _atomic_replace(path: Path, payload: str, *, mode: int) -> None:
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
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        path.chmod(mode)
        _fsync_directory(path.parent)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise VoiceyError(
            "VY-CLI-003",
            detail=f"could not save setup checkpoint {path}.",
        ) from exc


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
