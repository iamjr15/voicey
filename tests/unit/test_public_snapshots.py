# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from voicekit.release.snapshots import (
    PUBLIC_SNAPSHOT_PATHS,
    _json_default,
    build_public_snapshots,
    changed_snapshots,
    public_surface_docs_errors,
    write_public_snapshots,
)

ROOT = Path(__file__).parents[2]


def test_committed_public_snapshots_are_current() -> None:
    assert changed_snapshots(ROOT) == ()
    snapshots = build_public_snapshots()
    assert tuple(snapshots) == PUBLIC_SNAPSHOT_PATHS
    assert snapshots[PUBLIC_SNAPSHOT_PATHS[0]]["contract"].startswith("voicekit.Agent")
    assert snapshots[PUBLIC_SNAPSHOT_PATHS[1]]["contract"] == "voicekit result webhook"
    assert snapshots[PUBLIC_SNAPSHOT_PATHS[2]]["commands"]["path"] == "voicekit"


def test_snapshot_writer_only_updates_drift(tmp_path: Path) -> None:
    assert write_public_snapshots(tmp_path) == PUBLIC_SNAPSHOT_PATHS
    assert changed_snapshots(tmp_path) == ()

    target = tmp_path / PUBLIC_SNAPSHOT_PATHS[0]
    target.write_text("{}\n", encoding="utf-8")

    assert changed_snapshots(tmp_path) == (PUBLIC_SNAPSHOT_PATHS[0],)
    assert write_public_snapshots(tmp_path) == (PUBLIC_SNAPSHOT_PATHS[0],)
    assert json.loads(target.read_text(encoding="utf-8"))["contract"].startswith("voicekit.Agent")


def test_public_surface_change_requires_changelog_and_docs() -> None:
    snapshot = PUBLIC_SNAPSHOT_PATHS[0]
    assert public_surface_docs_errors({Path("src/voicekit/config/models.py")}) == ()
    errors = public_surface_docs_errors({snapshot})
    assert len(errors) == 2
    assert public_surface_docs_errors({snapshot, Path("CHANGELOG.md")}) == (
        "At least one explanatory docs/*.md page must change with a snapshot.",
    )
    assert (
        public_surface_docs_errors({snapshot, Path("CHANGELOG.md"), Path("docs/configuration.md")})
        == ()
    )


def test_snapshot_defaults_are_stable_json_values() -> None:
    class Value(Enum):
        ENABLED = "enabled"

    class Sentinel:
        pass

    assert _json_default(None) is None
    assert _json_default(Value.ENABLED) == "enabled"
    assert _json_default((1, Value.ENABLED)) == [1, "enabled"]
    assert _json_default(Sentinel()) == {
        "python_type": f"{Sentinel.__module__}.{Sentinel.__qualname__}"
    }
