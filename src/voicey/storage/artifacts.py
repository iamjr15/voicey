"""Protected local artifact store and crash-visible retention worker."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from voicey.errors import VoiceyError
from voicey.security.files import (
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    ensure_private_file,
)
from voicey.storage.repository import StorageRepository


def validate_artifact_key(storage_key: str) -> PurePosixPath:
    """Return one safe, relative artifact key shared by every store backend."""
    pure = PurePosixPath(storage_key)
    if (
        not storage_key
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or "\\" in storage_key
    ):
        raise VoiceyError("VY-ART-001", detail=storage_key)
    return pure


class ArtifactStore(Protocol):
    """Durable recording/backup bytes owned by the deployment target."""

    async def put(self, storage_key: str, content: bytes) -> None: ...

    async def read(self, storage_key: str) -> bytes: ...

    async def delete(self, storage_key: str) -> None: ...


class LocalArtifactStore:
    """Traversal-safe protected filesystem artifact implementation."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def put(self, storage_key: str, content: bytes) -> None:
        """Atomically persist bytes without following a target symlink."""
        destination = self._path(storage_key)
        ensure_private_directory(destination.parent)
        if destination.is_symlink():
            raise VoiceyError("VY-ART-001", detail=storage_key)
        temporary_path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(name)
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(PRIVATE_FILE_MODE)
            os.replace(temporary_path, destination)
            ensure_private_file(destination)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise VoiceyError("VY-ART-002", detail=f"{storage_key}: {exc}") from exc

    async def read(self, storage_key: str) -> bytes:
        """Read protected artifact bytes."""
        path = self._path(storage_key)
        try:
            if path.is_symlink():
                raise VoiceyError("VY-ART-001", detail=storage_key)
            return path.read_bytes()
        except VoiceyError:
            raise
        except OSError as exc:
            raise VoiceyError("VY-ART-002", detail=f"{storage_key}: {exc}") from exc

    async def delete(self, storage_key: str) -> None:
        """Idempotently delete exactly one validated artifact path."""
        path = self._path(storage_key)
        try:
            if path.is_symlink():
                raise VoiceyError("VY-ART-001", detail=storage_key)
            path.unlink(missing_ok=True)
        except VoiceyError:
            raise
        except OSError as exc:
            raise VoiceyError("VY-ART-002", detail=f"{storage_key}: {exc}") from exc

    def _path(self, storage_key: str) -> Path:
        pure = validate_artifact_key(storage_key)
        root = ensure_private_directory(self.root).resolve()
        candidate = root
        for part in pure.parts:
            candidate /= part
            if candidate.is_symlink():
                raise VoiceyError("VY-ART-001", detail=storage_key)
        if not candidate.is_relative_to(root):
            raise VoiceyError("VY-ART-001", detail=storage_key)
        return candidate


class RetentionWorker:
    """Finish database-queued artifact deletion with replay-safe acknowledgements."""

    def __init__(
        self,
        repository: StorageRepository,
        artifact_store: ArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store

    async def run_once(self) -> int:
        """Delete every due artifact, leaving failures visibly queued."""
        items = await self._repository.queue_retention()
        deleted = 0
        for item in items:
            await self._artifact_store.delete(item.storage_key)
            await self._repository.acknowledge_purge(item.storage_key)
            deleted += 1
        return deleted
