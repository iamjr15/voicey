"""Artifact channel and canary-promotion policy."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Literal, TypeAlias, cast

from packaging.version import InvalidVersion, Version

ReleaseChannel: TypeAlias = Literal["canary", "stable"]


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    """Validated wheel identity used in promotion evidence."""

    path: Path
    version: str
    release_line: str
    sha256: str
    channel: ReleaseChannel


def inspect_wheel(path: Path, channel: ReleaseChannel) -> ReleaseArtifact:
    """Validate wheel metadata, bytes, and the requested release channel."""
    if not path.is_file() or path.suffix != ".whl":
        msg = f"{path} is not a wheel."
        raise ValueError(msg)
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            msg = f"{path} must contain exactly one dist-info/METADATA file."
            raise ValueError(msg)
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    if metadata["Name"] != "voicey" or not metadata["Version"]:
        msg = f"{path} does not contain voicey package metadata."
        raise ValueError(msg)
    raw_version = metadata["Version"]
    try:
        version = Version(raw_version)
    except InvalidVersion as exc:
        msg = f"wheel version {raw_version!r} is invalid."
        raise ValueError(msg) from exc
    prerelease = version.is_prerelease or version.is_devrelease
    if channel == "canary" and not prerelease:
        msg = f"canary artifacts require a prerelease version; received {version}."
        raise ValueError(msg)
    if channel == "stable" and prerelease:
        msg = f"stable artifacts require a final version; received {version}."
        raise ValueError(msg)
    return ReleaseArtifact(
        path=path.resolve(),
        version=str(version),
        release_line=version.base_version,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        channel=channel,
    )


def validate_canary_promotion(
    stable: ReleaseArtifact,
    canary_report_path: Path,
) -> dict[str, object]:
    """Require green first-party evidence for the same release line before stable."""
    if stable.channel != "stable":
        raise ValueError("canary promotion validation requires a stable artifact.")
    try:
        payload = cast(
            "dict[str, object]",
            json.loads(canary_report_path.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not read canary evidence from {canary_report_path}."
        raise ValueError(msg) from exc
    required = {
        "schema_version": 1,
        "phase": "P4.5",
        "status": "green",
        "channel": "canary",
        "release_line": stable.release_line,
    }
    mismatches = [key for key, expected in required.items() if payload.get(key) != expected]
    sha256 = payload.get("artifact_sha256")
    if mismatches or not isinstance(sha256, str) or len(sha256) != 64:
        msg = (
            "canary evidence is missing, not green, or targets a different release line: "
            + ", ".join(mismatches or ["artifact_sha256"])
        )
        raise ValueError(msg)
    return payload
