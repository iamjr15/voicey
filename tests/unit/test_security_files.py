import os
import stat
from pathlib import Path

import pytest

from voicey.errors import VoiceyError
from voicey.security.files import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    ensure_private_file,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_directory_repairs_existing_permissions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)

    ensure_private_directory(data_dir)

    assert mode(data_dir) == PRIVATE_DIRECTORY_MODE


def test_private_file_is_owner_only(tmp_path: Path) -> None:
    protected_file = tmp_path / "data" / "calls.sqlite3"

    ensure_private_file(protected_file)

    assert mode(protected_file.parent) == PRIVATE_DIRECTORY_MODE
    assert mode(protected_file) == PRIVATE_FILE_MODE


def test_private_file_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("do not mutate", encoding="utf-8")
    link = tmp_path / "data" / "calls.sqlite3"
    link.parent.mkdir()
    link.symlink_to(target)

    with pytest.raises(VoiceyError, match="VY-SEC-002"):
        ensure_private_file(link)

    assert target.read_text(encoding="utf-8") == "do not mutate"
