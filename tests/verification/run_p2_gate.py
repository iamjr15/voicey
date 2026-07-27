"""Aggregate every automatable P2 exit check without promoting live/manual gaps."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    command: str
    duration_s: float | None = None
    detail: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p2-gate-report.json"),
    )
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    if not wheel.is_file():
        parser.error("--wheel must point to the wheel under test")

    python = sys.executable
    pytest = [python, "-m", "pytest", "--no-cov", "-q"]
    results = [
        _run_gate(
            "p1_local_regression",
            [
                python,
                str(ROOT / "tests" / "verification" / "run_p1_gate.py"),
                "--wheel",
                str(wheel),
                "--report",
                str(report_path.with_name("p1-from-p2-report.json")),
            ],
            timeout_s=900,
        ),
        _run_gate(
            "runtime_parity_and_config_mapping",
            [*pytest, "tests/parity"],
        ),
        _run_gate(
            "livekit_runtime_and_crash_guarantee",
            [
                *pytest,
                "tests/unit/test_livekit_runtime.py",
                "tests/unit/test_livekit_runtime_edges.py",
                "tests/unit/test_livekit_host.py",
                "tests/integration/test_livekit_sigkill.py",
            ],
        ),
        _run_gate(
            "unified_native_testing",
            [
                *pytest,
                "tests/unit/test_testing.py",
                "tests/unit/test_recipe_appointment.py",
            ],
        ),
        _run_gate(
            "twilio_livekit_local_certification",
            [*pytest, "tests/certification/test_twilio_livekit_sip.py"],
        ),
        _run_gate(
            "telnyx_both_paths_local_certification",
            [
                *pytest,
                "tests/certification/test_telnyx_adapter.py",
                "tests/certification/test_telnyx_media.py",
                "tests/certification/test_telnyx_livekit_sip.py",
            ],
        ),
    ]
    results.extend(_pending_external_results())

    failed = [result for result in results if result.status == "failed"]
    pending = [result for result in results if result.status.startswith("pending")]
    report = {
        "phase": "P2",
        "status": "failed" if failed else ("pending-live" if pending else "green"),
        "local_automated_status": "failed" if failed else "green",
        "results": [asdict(result) for result in results],
        "failed_count": len(failed),
        "pending_count": len(pending),
        "truthfulness": (
            "provider, paid-PSTN, microphone, and physical-handset gates remain "
            "pending until their exact commands genuinely pass"
        ),
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 1 if failed else 0


def _run_gate(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 600,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P2 gate] {name}: {rendered}", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            name=name,
            status="failed",
            command=rendered,
            duration_s=round(time.monotonic() - started, 3),
            detail=f"timed out after {timeout_s:g}s",
        )
    return GateResult(
        name=name,
        status="green" if completed.returncode == 0 else "failed",
        command=rendered,
        duration_s=round(time.monotonic() - started, 3),
        detail=None if completed.returncode == 0 else f"exit code {completed.returncode}",
    )


def _pending_external_results() -> list[GateResult]:
    return [
        GateResult(
            name="unified_reference_conversations",
            status="pending-live",
            command=(
                "run the two runtime-specific `voicekit test`, `voicekit test --audio`, "
                "and `voicekit test --report junit` commands in docs/GAPS.md"
            ),
            detail="requires reference-provider keys, Ollama, and both disposable projects",
        ),
        GateResult(
            name="livekit_browser_and_recipe_conversations",
            status="pending-human",
            command=(
                "run the P2 LiveKit playground and appointment conversation commands "
                "in docs/GAPS.md"
            ),
            detail="requires LiveKit/reference-provider credentials, microphone, and speech",
        ),
        GateResult(
            name="twilio_livekit_sip",
            status="pending-live",
            command=("uv run pytest -m live --no-cov tests/live/test_twilio_livekit_live.py"),
            detail="requires funded Twilio/LiveKit accounts, route mutation, and PSTN",
        ),
        GateResult(
            name="telnyx_both_paths",
            status="pending-live",
            command=(
                "uv run pytest -m live --no-cov tests/live/test_telnyx_live.py "
                "tests/live/test_telnyx_livekit_live.py"
            ),
            detail="requires funded Telnyx/LiveKit accounts, both paths, and PSTN",
        ),
        GateResult(
            name="physical_handsets",
            status="pending-human",
            command="complete the P2 Twilio and Telnyx handset checklists in docs/GAPS.md",
            detail="requires real caller and destination handsets",
        ),
    ]


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
