"""Aggregate every automatable P4 exit gate without promoting external evidence."""

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
from typing import cast

ROOT = Path(__file__).parents[2]


@dataclass(frozen=True, slots=True)
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
        default=Path(".voicekit/verification/p4-gate-report.json"),
    )
    parser.add_argument("--short-soak-s", type=float, default=5.0)
    parser.add_argument("--max-concurrent", type=int, default=8)
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file():
        parser.error("--wheel must point to a built wheel")
    report_path = args.report.expanduser().resolve()
    python = sys.executable
    verification = ROOT / "tests" / "verification"
    children = report_path.parent
    results = [
        _run_child(
            "p3_local_regression",
            [
                python,
                str(verification / "run_p3_gate.py"),
                "--wheel",
                str(wheel),
                "--report",
                str(children / "p3-from-p4-report.json"),
            ],
            children / "p3-from-p4-report.json",
            timeout_s=2400,
        ),
        _run_child(
            "chaos_drain_and_bounded_soak",
            [
                python,
                str(verification / "run_p4_hardening_gate.py"),
                "--short-soak-s",
                str(args.short_soak_s),
                "--max-concurrent",
                str(args.max_concurrent),
                "--report",
                str(children / "p4-hardening-report.json"),
            ],
            children / "p4-hardening-report.json",
            timeout_s=1800,
        ),
        _run_child(
            "prometheus_and_otlp",
            [
                python,
                str(verification / "run_p4_observability_gate.py"),
                "--report",
                str(children / "p4-observability-report.json"),
            ],
            children / "p4-observability-report.json",
        ),
        _run_child(
            "railway_local_automation",
            [
                python,
                str(verification / "run_p4_railway_gate.py"),
                "--report",
                str(children / "p4-railway-report.json"),
            ],
            children / "p4-railway-report.json",
            timeout_s=1800,
        ),
        _run_child(
            "upgrade_and_recipe_drift",
            [
                python,
                str(verification / "run_p4_upgrade_gate.py"),
                "--report",
                str(children / "p4-upgrade-report.json"),
            ],
            children / "p4-upgrade-report.json",
            timeout_s=2400,
        ),
        _run_child(
            "release_canary",
            [
                python,
                str(verification / "run_p4_release_gate.py"),
                "--wheel",
                str(wheel),
                "--channel",
                "canary",
                "--report",
                str(children / "p4-release-report.json"),
            ],
            children / "p4-release-report.json",
            timeout_s=2400,
        ),
        _run_child(
            "launch_documentation",
            [
                python,
                str(verification / "run_p4_docs_gate.py"),
                "--wheel",
                str(wheel),
                "--report",
                str(children / "p4-docs-report.json"),
            ],
            children / "p4-docs-report.json",
            timeout_s=2400,
        ),
        _run_child(
            "security",
            [
                python,
                str(verification / "run_p4_security_gate.py"),
                "--wheel",
                str(wheel),
                "--report",
                str(children / "p4-security-report.json"),
            ],
            children / "p4-security-report.json",
            timeout_s=5400,
        ),
    ]
    results.extend(_pending_external_results(args.max_concurrent))
    failures = [result for result in results if result.status == "failed"]
    local_pending = [result for result in results if result.status == "pending-local-environment"]
    external_pending = [
        result
        for result in results
        if result.status in {"pending-live", "pending-human", "pending-time"}
    ]
    local_status = "failed" if failures else "pending" if local_pending else "green"
    report = {
        "schema_version": 1,
        "phase": "P4",
        "status": (
            "failed"
            if failures
            else "pending-local"
            if local_pending
            else "pending-live"
            if external_pending
            else "green"
        ),
        "local_automated_status": local_status,
        "results": [asdict(result) for result in results],
        "failed_count": len(failures),
        "pending_count": len(local_pending) + len(external_pending),
        "truthfulness": (
            "green local automation does not promote paid providers, cloud accounts, "
            "physical handsets, public publishing, or the unshortened 24-hour soak"
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 1 if failures or local_pending else 0


def _run_child(
    name: str,
    command: Sequence[str],
    report_path: Path,
    *,
    timeout_s: float = 1200,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P4 aggregate] {name}: {rendered}", flush=True)
    started = time.monotonic()
    report_path.unlink(missing_ok=True)
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
            detail=f"{type(exc).__name__}: child gate did not complete",
        )
    duration = round(time.monotonic() - started, 3)
    try:
        payload = cast(
            "dict[str, object]",
            json.loads(report_path.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as exc:
        return GateResult(
            name=name,
            status="failed",
            command=rendered,
            duration_s=duration,
            detail=f"child report unavailable: {type(exc).__name__}",
        )
    child_local = payload.get("local_automated_status", payload.get("status"))
    green = completed.returncode == 0 and child_local == "green"
    return GateResult(
        name=name,
        status="green" if green else "failed",
        command=rendered,
        duration_s=duration,
        detail=f"child status={payload.get('status')}; local={child_local}",
    )


def _pending_external_results(max_concurrent: int) -> list[GateResult]:
    return [
        GateResult(
            name="full_24h_soak",
            status="pending-time",
            command=(
                "uv run python tests/verification/p4_soak.py --duration-s 86400 "
                f"--max-concurrent {max_concurrent} --runtime both "
                "--report .voicekit/verification/p4-24h-soak-report.json"
            ),
            detail="the aggregate uses a bounded soak and never represents 24 hours",
        ),
        GateResult(
            name="credentialed_provider_carrier_and_cloud_gates",
            status="pending-live",
            command="execute every pending-live command in docs/GAPS.md",
            detail=(
                "requires reference-provider keys, funded carriers, authenticated "
                "cloud projects, public ingress, and paid calls"
            ),
        ),
        GateResult(
            name="physical_audio_and_handset_checks",
            status="pending-human",
            command="execute every pending-human checklist in docs/GAPS.md",
            detail="requires microphones, human speech, and physical endpoints",
        ),
        GateResult(
            name="name_and_publication",
            status="pending-human",
            command="select the public name, execute RENAME.md, then publish manually",
            detail="repository creation, registration, and publishing are human-only",
        ),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
