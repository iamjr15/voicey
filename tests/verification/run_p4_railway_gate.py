"""Aggregate the credential-free P4.3 Railway deployment gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_MINIMUM = (5, 30, 1)
_MAXIMUM = (6, 0, 0)


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
        default=Path(".voicekit/verification/p4-railway-report.json"),
    )
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    pytest = [sys.executable, "-m", "pytest", "--no-cov", "-q"]

    results = [
        _railway_cli_contract(),
        _run(
            "railway_artifacts_resumption_ownership_and_cli",
            [
                *pytest,
                "tests/unit/test_deploy_railway.py",
                "tests/unit/test_results_service_runtime.py",
                "tests/unit/test_cli.py",
                "-k",
                "railway or results_service",
            ],
        ),
        _managed_preflight(pytest),
        GateResult(
            name="authenticated_railway_deploy",
            status="pending-live",
            command=(
                'uv run voicekit deploy railway --project "$VOICEKIT_RAILWAY_PROJECT" '
                '--workspace "$VOICEKIT_RAILWAY_WORKSPACE" --environment production '
                '--service "$VOICEKIT_RAILWAY_SERVICE" '
                '--bucket "$VOICEKIT_RAILWAY_BUCKET" --service-region us-east '
                '--bucket-region iad --engine-wheel "$VOICEKIT_ENGINE_WHEEL" '
                "--yes --json"
            ),
            detail=(
                "requires an authenticated billed Railway workspace; local evidence "
                "does not promote platform provisioning, deployment, or signed smoke"
            ),
        ),
    ]
    failures = [row for row in results if row.status == "failed"]
    local_pending = [row for row in results if row.status == "pending-local-environment"]
    report = {
        "phase": "P4.3",
        "status": ("failed" if failures else "pending-local" if local_pending else "pending-live"),
        "local_automated_status": (
            "failed" if failures else "pending" if local_pending else "green"
        ),
        "results": [asdict(row) for row in results],
        "truthfulness": (
            "the authenticated Railway project and paid resources remain pending-live"
        ),
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 1 if failures or local_pending else 0


def _railway_cli_contract() -> GateResult:
    executable = shutil.which("railway")
    command = (
        [executable, "--version"]
        if executable is not None
        else (
            ["npx", "--yes", "@railway/cli@5.30.1", "--version"]
            if shutil.which("npx") is not None
            else []
        )
    )
    if not command:
        return GateResult(
            name="railway_cli_contract",
            status="pending-local-environment",
            command="railway --version",
            detail="install Railway CLI >=5.30.1,<6 or provide npx",
        )
    rendered = shlex.join(command)
    print(f"[P4.3 gate] railway_cli_contract: {rendered}", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(
            name="railway_cli_contract",
            status="failed",
            command=rendered,
            duration_s=round(time.monotonic() - started, 3),
            detail=f"{type(exc).__name__}: command did not complete",
        )
    duration = round(time.monotonic() - started, 3)
    if completed.returncode != 0:
        return GateResult(
            name="railway_cli_contract",
            status="failed",
            command=rendered,
            duration_s=duration,
            detail=f"exit code {completed.returncode}",
        )
    match = _VERSION.search(f"{completed.stdout}\n{completed.stderr}")
    if match is None:
        return GateResult(
            name="railway_cli_contract",
            status="failed",
            command=rendered,
            duration_s=duration,
            detail="Railway CLI did not report a semantic version",
        )
    version = tuple(int(part) for part in match.groups())
    return GateResult(
        name="railway_cli_contract",
        status="green" if _MINIMUM <= version < _MAXIMUM else "failed",
        command=rendered,
        duration_s=duration,
        detail=f"verified Railway CLI {'.'.join(match.groups())}",
    )


def _managed_preflight(pytest: list[str]) -> GateResult:
    if not os.environ.get("VOICEKIT_TEST_POSTGRES_DSN"):
        return GateResult(
            name="railway_postgres_migration_and_rolling_preflight",
            status="pending-local-environment",
            command=(
                "VOICEKIT_TEST_POSTGRES_DSN=postgresql://... uv run pytest --no-cov -q "
                "tests/integration/test_managed_results_service.py"
            ),
            detail="requires a disposable PostgreSQL 17 database",
        )
    return _run(
        "railway_postgres_migration_and_rolling_preflight",
        [
            *pytest,
            "tests/integration/test_managed_results_service.py",
            "-k",
            "railway",
        ],
    )


def _run(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 900,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P4.3 gate] {name}: {rendered}", flush=True)
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
