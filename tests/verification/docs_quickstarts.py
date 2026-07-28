"""Execute both marked documentation quickstarts from a fresh wheel install."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=300)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p4-docs-quickstarts.json"),
    )
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file():
        parser.error("--wheel must point to a built wheel")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="voicekit-docs-quickstart-") as directory:
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
        quickstarts: list[dict[str, object]] = []
        failures: list[str] = []
        for runtime in ("pipecat", "livekit"):
            inner_report = root / f"{runtime}-report.json"
            completed = subprocess.run(
                [
                    str(python),
                    str(ROOT / "tests/verification/docs_quickstarts_inner.py"),
                    "--runtime",
                    runtime,
                    "--docs-root",
                    str(ROOT / "docs"),
                    "--workspace",
                    str(root / "projects"),
                    "--report",
                    str(inner_report),
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
                env=environment,
            )
            if completed.returncode != 0 or not inner_report.is_file():
                failures.append(f"{runtime}: {completed.stderr[-1000:]}")
                continue
            inner = json.loads(inner_report.read_text(encoding="utf-8"))
            if inner.get("status") != "green":
                failures.append(f"{runtime}: inner report was not green")
                continue
            quickstarts.extend(inner["quickstarts"])
        evidence = {
            "status": "failed" if failures else "green",
            "quickstarts": quickstarts,
            "failures": failures,
        }
    duration = round(time.monotonic() - started, 3)
    green = (
        evidence.get("status") == "green"
        and len(evidence.get("quickstarts", [])) == 2
        and duration <= args.budget_seconds
    )
    report = {
        "phase": "P4.6",
        "gate": "verbatim_docs_quickstarts",
        "status": "green" if green else "failed",
        "duration_s": duration,
        "budget_s": args.budget_seconds,
        "scope": "fresh wheel; provider-mocked native media/runtime paths",
        "evidence": evidence,
    }
    report_path = args.report.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
