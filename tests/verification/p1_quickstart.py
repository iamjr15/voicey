"""Time a wheel-only install through the first provider-mocked browser session."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=300)
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file() or not wheel.name.startswith("voicey-"):
        parser.error("--wheel must be a built voicey wheel")
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is required for the fresh-environment quickstart")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="voicey-p1-quickstart-") as temporary:
        root = Path(temporary)
        environment = root / ".venv"
        project = root / "quickstart-agent"
        python = _venv_python(environment)
        _run([uv, "venv", "--python", sys.executable, str(environment)])
        _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                f"{wheel}[pipecat]",
            ]
        )
        probe = Path(__file__).with_name("p1_quickstart_inner.py").resolve()
        completed = _run([str(python), str(probe), str(project)], capture=True)
        evidence = json.loads(completed.stdout)

    duration_s = time.monotonic() - started
    passed = duration_s <= args.budget_seconds
    report = {
        "gate": "p1_fresh_wheel_quickstart",
        "status": "green" if passed else "failed",
        "duration_s": round(duration_s, 3),
        "budget_s": args.budget_seconds,
        "evidence": evidence,
        "scope": "provider-mocked native runtime and local browser peer",
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if passed else 1


def _venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _run(argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        text=True,
        capture_output=capture,
        timeout=300,
    )
    if completed.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        raise RuntimeError(
            f"quickstart command failed ({completed.returncode}): {' '.join(argv)}\n{detail}"
        )
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
