"""Build-independent P4.5 release artifact and first-party canary gate."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from voicekit.release.policy import inspect_wheel, validate_canary_promotion

ROOT = Path(__file__).parents[2]


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: str
    command: str
    duration_s: float
    detail: str


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--channel", choices=("canary", "stable"), required=True)
    parser.add_argument("--canary-report", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p4-release-report.json"),
    )
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    results: list[GateResult] = []
    try:
        artifact = inspect_wheel(args.wheel.expanduser().resolve(), args.channel)
        results.append(
            GateResult(
                name="artifact_channel",
                status="green",
                command=f"inspect {artifact.path.name}",
                duration_s=0.0,
                detail=f"{artifact.version} ({artifact.sha256[:12]})",
            )
        )
        if args.channel == "stable":
            if args.canary_report is None:
                raise ValueError("--canary-report is required for stable promotion.")
            validate_canary_promotion(artifact, args.canary_report.expanduser().resolve())
            results.append(
                GateResult(
                    name="canary_before_stable",
                    status="green",
                    command=f"validate {args.canary_report}",
                    duration_s=0.0,
                    detail=f"green canary evidence for {artifact.release_line}",
                )
            )
        results.extend(_run_local_contracts())
        results.append(_run_installed_recipes(artifact.path))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        results.append(
            GateResult(
                name="release_policy",
                status="failed",
                command="release gate",
                duration_s=0.0,
                detail=str(exc),
            )
        )
        artifact = None
    failures = [result for result in results if result.status != "green"]
    report = {
        "schema_version": 1,
        "phase": "P4.5",
        "status": "failed" if failures else "green",
        "channel": args.channel,
        "version": None if artifact is None else artifact.version,
        "release_line": None if artifact is None else artifact.release_line,
        "artifact_sha256": None if artifact is None else artifact.sha256,
        "results": [asdict(result) for result in results],
        "truthfulness": (
            "This report validates a local wheel and first-party recipes. It does not "
            "claim that an artifact was uploaded to a public package index."
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 1 if failures else 0


def _run_local_contracts() -> list[GateResult]:
    pytest = [sys.executable, "-m", "pytest", "-q", "-o", "addopts="]
    return [
        _run(
            "release_policy_and_snapshots",
            [
                *pytest,
                "tests/unit/test_version.py",
                "tests/unit/test_versioning.py",
                "tests/unit/test_compatibility.py",
                "tests/unit/test_public_snapshots.py",
                "tests/unit/test_results_schema.py",
            ],
        ),
        _run(
            "public_snapshot_check",
            [sys.executable, "scripts/update_public_snapshots.py", "--check"],
        ),
    ]


def _run_installed_recipes(wheel: Path) -> GateResult:
    started = time.monotonic()
    command = "installed wheel: canary_recipes.py"
    try:
        with tempfile.TemporaryDirectory(prefix="voicekit-release-wheel-") as directory:
            root = Path(directory)
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            environment.pop("VIRTUAL_ENV", None)
            subprocess.run(
                ["uv", "venv", "--python", f"{sys.version_info.major}.{sys.version_info.minor}"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )
            python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    f"{wheel}[pipecat,livekit]",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=1200,
                env=environment,
            )
            completed = subprocess.run(
                [
                    str(python),
                    str(ROOT / "tests/verification/canary_recipes.py"),
                    "--report",
                    str(root / "recipes-report.json"),
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
                env=environment,
            )
            payload: dict[str, object] = (
                cast(
                    "dict[str, object]",
                    json.loads((root / "recipes-report.json").read_text(encoding="utf-8")),
                )
                if (root / "recipes-report.json").is_file()
                else {}
            )
            green = (
                completed.returncode == 0
                and payload.get("status") == "green"
                and payload.get("recipe_count") == 4
                and payload.get("runtime_count") == 2
            )
            detail = (
                "four packaged recipes compiled and instantiated on both native runtimes"
                if green
                else f"installed recipe verifier exited {completed.returncode}"
            )
            return GateResult(
                name="installed_first_party_recipes",
                status="green" if green else "failed",
                command=command,
                duration_s=round(time.monotonic() - started, 3),
                detail=detail,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return GateResult(
            name="installed_first_party_recipes",
            status="failed",
            command=command,
            duration_s=round(time.monotonic() - started, 3),
            detail=str(exc),
        )


def _run(name: str, command: list[str]) -> GateResult:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    return GateResult(
        name=name,
        status="green" if completed.returncode == 0 else "failed",
        command=shlex.join(command),
        duration_s=round(time.monotonic() - started, 3),
        detail=output[-500:],
    )


if __name__ == "__main__":
    raise SystemExit(main())
