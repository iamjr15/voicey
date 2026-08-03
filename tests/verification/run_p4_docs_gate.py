"""Run the source-derived and fresh-wheel P4.6 documentation gates."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

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
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicey/verification/p4-docs-report.json"),
    )
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file():
        parser.error("--wheel must point to a built wheel")
    report_path = args.report.expanduser().resolve()
    pytest = [sys.executable, "-m", "pytest", "-q", "-o", "addopts="]
    results = [
        _run(
            "launch_docs_contract_and_links",
            [
                *pytest,
                "tests/unit/test_docs_contract.py",
                "tests/unit/test_generated_docs.py",
            ],
        ),
        _run(
            "generated_api_reference",
            [sys.executable, "scripts/update_api_reference.py", "--check"],
        ),
        _run(
            "public_contract_snapshots",
            [sys.executable, "scripts/update_public_snapshots.py", "--check"],
        ),
        _run(
            "recipe_demo_audio",
            [sys.executable, "scripts/generate_recipe_demo_audio.py", "--check"],
        ),
        _run(
            "fresh_wheel_verbatim_quickstarts",
            [
                sys.executable,
                "tests/verification/docs_quickstarts.py",
                "--wheel",
                str(wheel),
                "--budget-seconds",
                "300",
                "--report",
                str(report_path.with_name("p4-docs-quickstarts.json")),
            ],
            timeout_s=1500,
        ),
    ]
    failures = [result for result in results if result.status != "green"]
    report = {
        "schema_version": 1,
        "phase": "P4.6",
        "gate": "launch_documentation",
        "status": "failed" if failures else "green",
        "results": [asdict(result) for result in results],
        "truthfulness": (
            "fresh-wheel quickstarts use provider-mocked native runtime/media paths; "
            "credentialed microphone conversations remain pending-live"
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 1 if failures else 0


def _run(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 600,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P4.6 docs] {name}: {rendered}", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
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
    output = completed.stdout.strip() or completed.stderr.strip()
    return GateResult(
        name=name,
        status="green" if completed.returncode == 0 else "failed",
        command=rendered,
        duration_s=round(time.monotonic() - started, 3),
        detail=output[-1000:],
    )


if __name__ == "__main__":
    raise SystemExit(main())
