"""Deterministic snapshots for Voicekit's versioned public contracts."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, cast

from typer._click import Command, Parameter
from typer.core import TyperGroup, TyperOption
from typer.main import get_command

from voicekit.cli.app import app
from voicekit.config.models import Agent
from voicekit.results.schema import WebhookEvent

PUBLIC_SNAPSHOT_PATHS = (
    Path("docs/api/snapshots/config-schema.json"),
    Path("docs/api/snapshots/webhook-schema.json"),
    Path("docs/api/snapshots/cli-surface.json"),
)


def build_public_snapshots() -> dict[Path, dict[str, Any]]:
    """Build all public contracts from their executable sources of truth."""
    return {
        PUBLIC_SNAPSHOT_PATHS[0]: {
            "contract": "voicekit.Agent serialized configuration",
            "schema": Agent.model_json_schema(mode="serialization"),
        },
        PUBLIC_SNAPSHOT_PATHS[1]: {
            "contract": "voicekit result webhook",
            "schema": WebhookEvent.model_json_schema(by_alias=True, mode="serialization"),
        },
        PUBLIC_SNAPSHOT_PATHS[2]: {
            "contract": "voicekit command-line interface",
            "commands": _command_snapshot(get_command(app), path=("voicekit",)),
        },
    }


def snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    return (
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    )


def changed_snapshots(root: Path) -> tuple[Path, ...]:
    """Return missing or stale committed snapshots without modifying the tree."""
    snapshots = build_public_snapshots()
    return tuple(
        path
        for path, snapshot in snapshots.items()
        if not (root / path).is_file() or (root / path).read_bytes() != snapshot_bytes(snapshot)
    )


def write_public_snapshots(root: Path) -> tuple[Path, ...]:
    """Update snapshots explicitly for an intentional public contract change."""
    snapshots = build_public_snapshots()
    changed = changed_snapshots(root)
    for path in changed:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot_bytes(snapshots[path]))
    return changed


def public_surface_docs_errors(changed_paths: set[Path]) -> tuple[str, ...]:
    """Require changelog and explanatory docs when a snapshot changes in a PR."""
    snapshot_changed = any(path in changed_paths for path in PUBLIC_SNAPSHOT_PATHS)
    if not snapshot_changed:
        return ()
    errors: list[str] = []
    if Path("CHANGELOG.md") not in changed_paths:
        errors.append("CHANGELOG.md must change with a public-surface snapshot.")
    explanatory_docs = [
        path
        for path in changed_paths
        if path.suffix == ".md"
        and path.parts
        and path.parts[0] == "docs"
        and path not in PUBLIC_SNAPSHOT_PATHS
    ]
    if not explanatory_docs:
        errors.append("At least one explanatory docs/*.md page must change with a snapshot.")
    return tuple(errors)


def _command_snapshot(command: Command, *, path: tuple[str, ...]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": " ".join(path),
        "help": command.help or "",
        "params": [_parameter_snapshot(parameter) for parameter in command.params],
    }
    if isinstance(command, TyperGroup):
        snapshot["subcommands"] = [
            _command_snapshot(child, path=(*path, name))
            for name, child in sorted(command.commands.items())
            if not child.hidden
        ]
    return snapshot


def _parameter_snapshot(parameter: Parameter) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": parameter.name,
        "kind": parameter.param_type_name,
        "required": parameter.required,
        "multiple": parameter.multiple,
        "nargs": parameter.nargs,
        "type": parameter.type.name,
        "default": _json_default(parameter.default),
    }
    if isinstance(parameter, TyperOption):
        result.update(
            {
                "flags": [*parameter.opts, *parameter.secondary_opts],
                "is_flag": parameter.is_flag,
                "count": parameter.count,
                "help": parameter.help or "",
                "hidden": parameter.hidden,
                "envvar": parameter.envvar,
            }
        )
    return result


def _json_default(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _json_default(value.value)
    if isinstance(value, (list, tuple)):
        return [_json_default(item) for item in cast("list[object] | tuple[object, ...]", value)]
    return {"python_type": f"{type(value).__module__}.{type(value).__qualname__}"}
