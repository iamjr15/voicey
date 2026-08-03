"""Owner-only dotenv updates used by guided key collection."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from voicey.errors import VoiceyError

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ASSIGNMENT = re.compile(r"^(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)=")


class EnvFileStore:
    """Update `.env` atomically without rewriting unrelated user lines."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, str]:
        """Read simple dotenv values for validation without mutating `os.environ`."""
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(self.path))
        try:
            values: dict[str, str] = {}
            for line in self.path.read_text(encoding="utf-8").splitlines():
                match = _ASSIGNMENT.match(line.strip())
                if match is None:
                    continue
                name = match.group("name")
                raw = line.split("=", maxsplit=1)[1].strip()
                values[name] = _decode_value(raw)
            return values
        except (OSError, ValueError) as exc:
            raise VoiceyError(
                "VY-CLI-003",
                detail=f"could not read protected environment file {self.path}.",
            ) from exc

    def update(self, values: Mapping[str, str]) -> None:
        """Replace exact assignments and append missing names with mode 0600."""
        invalid = [name for name in values if not _ENV_NAME.fullmatch(name)]
        if invalid:
            raise VoiceyError(
                "VY-CLI-003",
                detail=f"invalid environment variable name {invalid[0]!r}.",
            )
        if any("\x00" in value for value in values.values()):
            raise VoiceyError("VY-CLI-003", detail="environment values cannot contain NUL.")
        if self.path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(self.path))
        try:
            existing = (
                self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
            )
            output: list[str] = []
            written: set[str] = set()
            for line in existing:
                match = _ASSIGNMENT.match(line.strip())
                name = None if match is None else match.group("name")
                if name is None or name not in values:
                    output.append(line)
                    continue
                if name in written:
                    continue
                output.append(f"{name}={json.dumps(values[name])}")
                written.add(name)
            for name in sorted(set(values) - written):
                output.append(f"{name}={json.dumps(values[name])}")
            self._replace("\n".join(output).rstrip() + "\n")
        except VoiceyError:
            raise
        except OSError as exc:
            raise VoiceyError(
                "VY-CLI-003",
                detail=f"could not update protected environment file {self.path}.",
            ) from exc

    def _replace(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                text=True,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
            if stat.S_IMODE(self.path.stat().st_mode) != 0o600:
                raise VoiceyError(
                    "VY-SEC-001",
                    detail=f"{self.path} is not owner-only.",
                )
            _fsync_directory(self.path.parent)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise


def ensure_env_ignored(project_dir: Path) -> None:
    """Ensure generated secret files cannot be accidentally committed."""
    path = project_dir / ".gitignore"
    try:
        if path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(path))
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if ".env*" not in lines:
            lines.extend(["", "# Local secrets", ".env*"])
        if "!.env.example" not in lines:
            lines.append("!.env.example")
        payload = "\n".join(lines).strip() + "\n"
        _replace_public(path, payload)
    except VoiceyError:
        raise
    except OSError as exc:
        raise VoiceyError(
            "VY-CLI-003",
            detail=f"could not protect generated environment files in {project_dir}.",
        ) from exc


def merged_environment(
    file_values: Mapping[str, str],
    process_values: Mapping[str, str],
) -> dict[str, str]:
    """Prefer process injection over local dotenv values."""
    return dict(file_values) | dict(process_values)


def _decode_value(raw: str) -> str:
    if not raw:
        return ""
    if raw[0] == '"':
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError("dotenv value is not a string")
        return value
    if raw[0] == "'" and raw.endswith("'"):
        return raw[1:-1]
    return raw.split(" #", maxsplit=1)[0]


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_public(path: Path, payload: str) -> None:
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            descriptor = None
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o644)
        _fsync_directory(path.parent)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
