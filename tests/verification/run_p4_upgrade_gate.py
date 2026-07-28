"""Run the credential-free P4.4 upgrade and recipe-drift gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from voicekit.config.manifest import ManifestStore, ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.recipes.source import RECIPE_LOCK_NAME, install_recipe, recipe_files

ROOT = Path(__file__).parents[2]
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_MINIMUM = (0, 11, 0)
_MAXIMUM = (1, 0, 0)


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    command: str
    duration_s: float | None = None
    detail: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p4-upgrade-report.json"),
    )
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    pytest = [sys.executable, "-m", "pytest", "-q", "-o", "addopts="]
    results = [
        _uv_cli_contract(),
        _run(
            "upgrade_drift_coverage_and_cli_contract",
            [
                *pytest,
                "tests/unit/test_upgrade.py",
                "tests/unit/test_recipe_drift.py",
                "tests/unit/test_recipe_appointment.py",
                "tests/unit/test_recipes_p3.py",
                "tests/verification/test_p1_cli_matrix.py",
                "--cov=voicekit.upgrade",
                "--cov=voicekit.recipes.drift",
                "--cov-branch",
                "--cov-fail-under=90",
            ],
        ),
        _real_local_upgrade(),
    ]
    failures = [row for row in results if row.status != "green"]
    report = {
        "phase": "P4.4",
        "status": "failed" if failures else "green",
        "local_automated_status": "failed" if failures else "green",
        "results": [asdict(row) for row in results],
        "truthfulness": (
            "the gate uses only a local path distribution and makes no claim about "
            "an unpublished public package index"
        ),
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 1 if failures else 0


def _uv_cli_contract() -> GateResult:
    executable = shutil.which("uv")
    if executable is None:
        return GateResult(
            name="uv_cli_contract",
            status="failed",
            command="uv --version",
            detail="install uv >=0.11,<1",
        )
    command = [executable, "--version"]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    duration = round(time.monotonic() - started, 3)
    match = _VERSION.search(f"{completed.stdout}\n{completed.stderr}")
    if completed.returncode != 0 or match is None:
        return GateResult(
            name="uv_cli_contract",
            status="failed",
            command=shlex.join(command),
            duration_s=duration,
            detail="uv did not report a semantic version",
        )
    version = tuple(int(part) for part in match.groups())
    return GateResult(
        name="uv_cli_contract",
        status="green" if _MINIMUM <= version < _MAXIMUM else "failed",
        command=shlex.join(command),
        duration_s=duration,
        detail=f"verified uv {'.'.join(match.groups())}",
    )


def _real_local_upgrade() -> GateResult:
    started = time.monotonic()
    command = ".venv/bin/voicekit upgrade --pre --yes --json"
    try:
        with tempfile.TemporaryDirectory(prefix="voicekit-p4-upgrade-") as directory:
            root = Path(directory)
            environment = dict(os.environ)
            environment.pop("VIRTUAL_ENV", None)
            environment["UV_NO_CACHE"] = "1"
            manifest = _manifest()
            ManifestStore(root / "voicekit.jsonc").save(manifest)
            install_recipe(
                root,
                name=manifest.recipe.name,
                version=manifest.recipe.version,
                runtime=manifest.runtime,
            )
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                (
                    '[project]\nname = "voicekit-upgrade-gate"\nversion = "0.0.0"\n'
                    'requires-python = ">=3.11,<3.15"\n'
                    f'dependencies = ["voicekit[pipecat] @ {ROOT.as_uri()}"]\n'
                ),
                encoding="utf-8",
            )
            locked = subprocess.run(
                ["uv", "lock"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=1200,
                env=environment,
            )
            if locked.returncode != 0:
                return GateResult(
                    name="real_local_lock_upgrade_and_fresh_drift",
                    status="failed",
                    command="uv lock",
                    duration_s=round(time.monotonic() - started, 3),
                    detail=f"initial uv lock exited {locked.returncode}",
                )
            initial_sync = subprocess.run(
                ["uv", "sync", "--locked"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=1200,
                env=environment,
            )
            if initial_sync.returncode != 0:
                return GateResult(
                    name="real_local_lock_upgrade_and_fresh_drift",
                    status="failed",
                    command="uv sync --locked",
                    duration_s=round(time.monotonic() - started, 3),
                    detail=f"initial sync exited {initial_sync.returncode}",
                )
            owned = recipe_files(manifest.recipe.name, manifest.runtime)
            before = {
                relative: (root / relative).read_bytes()
                for relative in (*owned, RECIPE_LOCK_NAME, "pyproject.toml")
            }
            voicekit_executable = (
                root / ".venv" / ("Scripts/voicekit.exe" if os.name == "nt" else "bin/voicekit")
            )
            completed = subprocess.run(
                [
                    str(voicekit_executable),
                    "upgrade",
                    "--pre",
                    "--yes",
                    "--json",
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=1200,
            )
            if completed.returncode != 0:
                return GateResult(
                    name="real_local_lock_upgrade_and_fresh_drift",
                    status="failed",
                    command=command,
                    duration_s=round(time.monotonic() - started, 3),
                    detail=(
                        f"upgrade exited {completed.returncode}: "
                        f"{completed.stdout.strip()[-500:] or completed.stderr.strip()[-500:]}"
                    ),
                )
            payload = cast("dict[str, object]", json.loads(completed.stdout))
            drift = payload.get("recipe_drift")
            after = {
                relative: (root / relative).read_bytes()
                for relative in (*owned, RECIPE_LOCK_NAME, "pyproject.toml")
            }
            if (
                before != after
                or not isinstance(drift, dict)
                or drift.get("status") != "current"
                or payload.get("pyproject_unchanged") is not True
                or payload.get("recipe_sources_unchanged") is not True
            ):
                raise AssertionError("upgrade did not preserve project source and current drift")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AssertionError) as exc:
        return GateResult(
            name="real_local_lock_upgrade_and_fresh_drift",
            status="failed",
            command=command,
            duration_s=round(time.monotonic() - started, 3),
            detail=f"{type(exc).__name__}: {exc}",
        )
    return GateResult(
        name="real_local_lock_upgrade_and_fresh_drift",
        status="green",
        command=command,
        duration_s=round(time.monotonic() - started, 3),
        detail="local path package resolved, synced, freshly inspected, and source-preserved",
    )


def _manifest() -> ProjectManifest:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    return ProjectManifest(
        project_name="upgrade-gate",
        runtime="pipecat",
        recipe=RecipeSelection(name="appointment-booking", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )


def _run(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 900,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P4.4 gate] {name}: {rendered}", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(
            name=name,
            status="failed",
            command=rendered,
            duration_s=round(time.monotonic() - started, 3),
            detail=f"{type(exc).__name__}: command did not complete",
        )
    return GateResult(
        name=name,
        status="green" if completed.returncode == 0 else "failed",
        command=rendered,
        duration_s=round(time.monotonic() - started, 3),
        detail=None if completed.returncode == 0 else f"exit code {completed.returncode}",
    )


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
