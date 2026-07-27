"""Fail-closed Docker persistence topology and rolling-generation probes."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from voicekit.errors import VoicekitError
from voicekit.obs.records import NewCall
from voicekit.security.files import ensure_private_directory
from voicekit.storage.artifacts import LocalArtifactStore
from voicekit.storage.models import ResultDeliveryConfig, ResultSnapshot, TerminalRequest
from voicekit.storage.sqlite import SQLiteRepository

_REMOTE_FILESYSTEMS = frozenset(
    {
        "9p",
        "ceph",
        "cifs",
        "fuse.ceph",
        "fuse.glusterfs",
        "fuse.sshfs",
        "glusterfs",
        "nfs",
        "nfs4",
        "smb",
        "smb2",
        "smb3",
        "smbfs",
        "sshfs",
        "virtiofs",
    }
)


@dataclass(frozen=True, slots=True)
class PersistencePreflightReport:
    """Machine-readable proof that Docker is using its assigned storage mode."""

    data_dir: Path
    database_path: Path
    artifact_root: Path
    filesystem_type: str | None
    journal_mode: str
    synchronous: int
    schema_ready: bool
    artifact_round_trip: bool


@dataclass(frozen=True, slots=True)
class RollingGenerationReport:
    """Evidence that a replacement generation fences the stale writer."""

    old_generation: int
    new_generation: int
    stale_writer_rejected: bool
    terminal_event_count: int


async def docker_persistence_preflight(
    data_dir: Path,
    *,
    deploy_target: str,
    storage_backend: str,
    sqlite_local_only: bool,
    replica_count: int,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> PersistencePreflightReport:
    """Validate the locked Docker/SQLite/local-artifact matrix before serving."""
    if (
        deploy_target != "docker"
        or storage_backend != "sqlite"
        or not sqlite_local_only
        or replica_count != 1
    ):
        raise VoicekitError(
            "VK-DEP-002",
            detail=(
                "Docker requires target=docker, backend=sqlite, "
                "VOICEKIT_SQLITE_LOCAL_ONLY=1, and replica_count=1."
            ),
        )
    root = await asyncio.to_thread(_prepare_data_root, data_dir)
    filesystem_type = _filesystem_type(root, mountinfo_path)
    if filesystem_type is not None and (
        filesystem_type in _REMOTE_FILESYSTEMS
        or filesystem_type.startswith(("nfs", "cifs", "smb", "ceph", "gluster"))
    ):
        raise VoicekitError(
            "VK-DEP-002",
            detail=(
                f"{root} is mounted as remote filesystem {filesystem_type!r}; "
                "Docker SQLite requires one host-local volume."
            ),
        )

    database_path = root / "calls.sqlite3"
    async with SQLiteRepository(database_path) as repository:
        pragmas = await repository.pragmas()
    journal_mode = str(pragmas.get("journal_mode", "")).casefold()
    synchronous = int(pragmas.get("synchronous", -1))
    if journal_mode != "wal" or synchronous != 2:
        raise VoicekitError(
            "VK-DEP-002",
            detail=(
                f"SQLite durability is journal_mode={journal_mode!r}, "
                f"synchronous={synchronous}; expected WAL/FULL."
            ),
        )

    artifact_root = root / "artifacts"
    artifacts = LocalArtifactStore(artifact_root)
    marker = f"preflight/{uuid.uuid4().hex}.bin"
    expected = os.urandom(32)
    await artifacts.put(marker, expected)
    observed = await artifacts.read(marker)
    await artifacts.delete(marker)
    if observed != expected:
        raise VoicekitError("VK-DEP-002", detail="local artifact round trip changed bytes.")

    return PersistencePreflightReport(
        data_dir=root,
        database_path=database_path,
        artifact_root=artifact_root,
        filesystem_type=filesystem_type,
        journal_mode=journal_mode,
        synchronous=synchronous,
        schema_ready=True,
        artifact_round_trip=True,
    )


async def rolling_generation_invariant(data_dir: Path) -> RollingGenerationReport:
    """Prove overlapping same-host generations share schema and enforce fencing."""
    root = ensure_private_directory(data_dir).resolve()
    database_path = root / f".rolling-{uuid.uuid4().hex}.sqlite3"
    call_id = f"call_deploy_{uuid.uuid4().hex}"
    started = datetime.now(UTC)
    delivery = ResultDeliveryConfig(endpoint="https://example.invalid/voicekit-results")
    try:
        async with (
            SQLiteRepository(database_path) as old_repository,
            SQLiteRepository(database_path) as new_repository,
        ):
            old_lease = await old_repository.begin_call(
                NewCall(
                    call_id=call_id,
                    agent_name="deployment-preflight",
                    runtime="pipecat",
                    channel="web",
                    direction="inbound",
                    config_hash="sha256:" + ("0" * 64),
                    started_at=started,
                ),
                owner_id="generation-old",
                delivery=delivery,
                lease_ttl=timedelta(seconds=1),
                now=started,
            )
            new_lease = await new_repository.takeover_expired_call(
                call_id,
                owner_id="generation-new",
                lease_ttl=timedelta(seconds=30),
                now=started + timedelta(seconds=2),
            )
            stale_writer_rejected = False
            try:
                await old_repository.flush_results(old_lease, ResultSnapshot())
            except VoicekitError as exc:
                if exc.code != "VK-RES-006":
                    raise
                stale_writer_rejected = True
            if not stale_writer_rejected:
                raise VoicekitError(
                    "VK-DEP-002",
                    detail="stale rolling-generation writer was not fenced.",
                )
            event = await new_repository.terminalize(
                new_lease,
                TerminalRequest(
                    event_type="call.completed",
                    ended_reason="agent_hangup",
                    ended_at=started + timedelta(seconds=3),
                ),
            )
            terminal = await old_repository.get_terminal_event_for_call(call_id)
            if terminal.event_id != event.event_id:
                raise VoicekitError(
                    "VK-DEP-002",
                    detail="overlapping generations observed different terminal events.",
                )
        return RollingGenerationReport(
            old_generation=old_lease.generation,
            new_generation=new_lease.generation,
            stale_writer_rejected=True,
            terminal_event_count=1,
        )
    finally:
        await asyncio.to_thread(_remove_database_files, database_path)


def _filesystem_type(path: Path, mountinfo_path: Path) -> str | None:
    try:
        payload = mountinfo_path.read_text(encoding="utf-8")
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for line in payload.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        trailing = after.split()
        if not separator or len(fields) < 5 or not trailing:
            continue
        mount_point = Path(_unescape_mountinfo(fields[4]))
        try:
            contains = path == mount_point or path.is_relative_to(mount_point)
        except (OSError, ValueError):
            contains = False
        if not contains:
            continue
        candidate = (len(mount_point.parts), trailing[0].casefold())
        if best is None or candidate[0] > best[0]:
            best = candidate
    return None if best is None else best[1]


def _prepare_data_root(data_dir: Path) -> Path:
    if data_dir.is_symlink():
        raise VoicekitError("VK-DEP-002", detail=f"{data_dir} must not be a symbolic link.")
    return ensure_private_directory(data_dir).resolve()


def _remove_database_files(database_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)


def _unescape_mountinfo(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )
