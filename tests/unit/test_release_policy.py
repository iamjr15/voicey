from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from voicekit.release.policy import inspect_wheel, validate_canary_promotion


def _wheel(tmp_path: Path, version: str, *, name: str = "voicekit") -> Path:
    path = tmp_path / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
        )
    return path


def _canary_report(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "phase": "P4.5",
        "status": "green",
        "channel": "canary",
        "release_line": "1.2.0",
        "artifact_sha256": "a" * 64,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_inspect_wheel_enforces_release_channel(tmp_path: Path) -> None:
    canary = inspect_wheel(_wheel(tmp_path, "1.2.0rc1"), "canary")
    stable = inspect_wheel(_wheel(tmp_path, "1.2.0"), "stable")

    assert canary.release_line == stable.release_line == "1.2.0"
    assert len(canary.sha256) == 64
    with pytest.raises(ValueError, match="prerelease version"):
        inspect_wheel(stable.path, "canary")
    with pytest.raises(ValueError, match="final version"):
        inspect_wheel(canary.path, "stable")


def test_inspect_wheel_rejects_invalid_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a wheel"):
        inspect_wheel(tmp_path / "missing.whl", "canary")
    with pytest.raises(ValueError, match="exactly one"):
        inspect_wheel(_empty_wheel(tmp_path), "canary")
    with pytest.raises(ValueError, match="voicekit package metadata"):
        inspect_wheel(_wheel(tmp_path, "1.2.0rc1", name="other"), "canary")


def _empty_wheel(tmp_path: Path) -> Path:
    path = tmp_path / "empty.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("README", "empty")
    return path


def test_stable_promotion_requires_matching_green_canary(tmp_path: Path) -> None:
    stable = inspect_wheel(_wheel(tmp_path, "1.2.0"), "stable")
    report = _canary_report(tmp_path / "canary.json")

    assert validate_canary_promotion(stable, report)["status"] == "green"
    with pytest.raises(ValueError, match="stable artifact"):
        validate_canary_promotion(
            inspect_wheel(_wheel(tmp_path, "1.2.0rc2"), "canary"),
            report,
        )
    with pytest.raises(ValueError, match="different release line"):
        validate_canary_promotion(
            stable,
            _canary_report(tmp_path / "wrong.json", release_line="1.3.0"),
        )
    with pytest.raises(ValueError, match="could not read"):
        validate_canary_promotion(stable, tmp_path / "missing.json")


def test_stable_promotion_rejects_invalid_sha(tmp_path: Path) -> None:
    stable = inspect_wheel(_wheel(tmp_path, "1.2.0"), "stable")
    report = _canary_report(tmp_path / "invalid.json", artifact_sha256="short")

    with pytest.raises(ValueError, match="artifact_sha256"):
        validate_canary_promotion(stable, report)
