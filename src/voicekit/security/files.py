"""Create and verify local protected storage with fail-closed permissions."""

from __future__ import annotations

import os
import stat
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> Path:
    """Create a private directory and enforce owner-only access."""
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(PRIVATE_DIRECTORY_MODE)
    _assert_mode(path, PRIVATE_DIRECTORY_MODE)
    return path


def ensure_private_file(path: Path) -> Path:
    """Create a private file without following an existing symlink."""
    ensure_private_directory(path.parent)
    if path.is_symlink():
        msg = f"refusing to open protected symlink: {path}"
        raise OSError(msg)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    os.close(descriptor)
    path.chmod(PRIVATE_FILE_MODE)
    _assert_mode(path, PRIVATE_FILE_MODE)
    return path


def _assert_mode(path: Path, expected: int) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != expected:
        msg = f"protected path {path} has mode {actual:o}; expected {expected:o}"
        raise PermissionError(msg)
