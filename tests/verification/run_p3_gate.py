"""Aggregate every automatable P3 exit check without promoting live/manual gaps."""

from __future__ import annotations

import argparse
import json
import os
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
        default=Path(".voicey/verification/p3-gate-report.json"),
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
            "p2_local_regression",
            [
                python,
                str(ROOT / "tests" / "verification" / "run_p2_gate.py"),
                "--wheel",
                str(wheel),
                "--report",
                str(report_path.with_name("p2-from-p3-report.json")),
            ],
            timeout_s=1200,
        ),
        _run_gate(
            "first_party_recipes",
            [
                *pytest,
                "tests/unit/test_recipes_p3.py",
                "tests/unit/test_recipe_appointment.py",
            ],
        ),
        _run_gate(
            "vobiz_both_paths",
            [
                *pytest,
                "tests/certification/test_vobiz_adapter.py",
                "tests/certification/test_vobiz_media.py",
                "tests/certification/test_vobiz_livekit_sip.py",
                "tests/unit/test_vobiz_pipecat_host.py",
            ],
        ),
        _run_gate(
            "plivo_and_generic_sip",
            [
                *pytest,
                "tests/certification/test_plivo_adapter.py",
                "tests/certification/test_plivo_media.py",
                "tests/certification/test_plivo_livekit_sip.py",
                "tests/certification/test_generic_livekit_sip.py",
                "tests/unit/test_plivo_pipecat_host.py",
            ],
        ),
        _run_gate(
            "tier3_pstn_harness",
            [*pytest, "tests/unit/test_live_testing.py"],
        ),
        _run_gate(
            "relay_and_managed_cloud",
            [
                *pytest,
                "tests/unit/test_relay.py",
                "tests/unit/test_s3_artifacts.py",
                "tests/unit/test_deploy_fly.py",
                "tests/unit/test_deploy_cloud.py",
                "tests/unit/test_cloud_runtime.py",
                "tests/unit/test_cloud_answer.py",
                "tests/unit/test_cloud_smoke.py",
            ],
        ),
        _run_gate(
            "twilio_warm_transfer",
            [
                *pytest,
                "tests/certification/test_twilio_warm_transfer.py",
                "tests/unit/test_pipecat_runtime.py",
                "tests/unit/test_pipecat_host.py",
                "tests/parity/test_matrix.py",
            ],
        ),
        _managed_storage_gate(pytest),
    ]
    results.extend(_pending_external_results())

    failed = [result for result in results if result.status == "failed"]
    local_pending = [result for result in results if result.status == "pending-local-environment"]
    external_pending = [
        result
        for result in results
        if result.status in {"pending-live", "pending-human", "pending-time"}
    ]
    if failed:
        local_status = "failed"
    elif local_pending:
        local_status = "pending"
    else:
        local_status = "green"
    report = {
        "phase": "P3",
        "status": (
            "failed"
            if failed
            else "pending-local"
            if local_pending
            else "pending-live"
            if external_pending
            else "green"
        ),
        "local_automated_status": local_status,
        "results": [asdict(result) for result in results],
        "failed_count": len(failed),
        "pending_count": len(local_pending) + len(external_pending),
        "truthfulness": (
            "provider, paid-PSTN, cloud-account, physical-handset, and wall-clock "
            "gates remain pending until their exact commands genuinely pass"
        ),
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 1 if failed or local_pending else 0


def _managed_storage_gate(pytest: list[str]) -> GateResult:
    if not os.environ.get("VOICEY_TEST_POSTGRES_DSN"):
        return GateResult(
            name="managed_postgres_and_results_service",
            status="pending-local-environment",
            command=(
                "VOICEY_TEST_POSTGRES_DSN=postgresql://... "
                "uv run pytest --no-cov -q tests/integration/test_postgres_repository.py "
                "tests/integration/test_repository_backends.py "
                "tests/integration/test_managed_results_service.py"
            ),
            detail="requires a disposable PostgreSQL 17 database",
        )
    return _run_gate(
        "managed_postgres_and_results_service",
        [
            *pytest,
            "tests/integration/test_postgres_repository.py",
            "tests/integration/test_repository_backends.py",
            "tests/integration/test_managed_results_service.py",
            "tests/unit/test_results_service_runtime.py",
        ],
    )


def _run_gate(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 900,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P3 gate] {name}: {rendered}", flush=True)
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
            name="p3_recipe_conversations",
            status="pending-live",
            command="run the six recipe/runtime audio and JUnit loops in docs/GAPS.md",
            detail=(
                "all 34 provider text cases are green through the Anthropic API override; "
                "audio/report and human warm-transfer evidence remain"
            ),
        ),
        GateResult(
            name="vobiz_and_plivo_both_paths",
            status="pending-live",
            command=(
                "uv run pytest -m live --no-cov tests/live/test_vobiz_live.py "
                "tests/live/test_vobiz_livekit_live.py tests/live/test_plivo_live.py "
                "tests/live/test_plivo_livekit_live.py tests/live/test_generic_sip_live.py"
            ),
            detail="requires funded carrier/LiveKit accounts and external SIP/PSTN",
        ),
        GateResult(
            name="tier3_paid_pstn",
            status="pending-live",
            command="run both P3 tier-3 one-case fixture commands in docs/GAPS.md",
            detail="requires explicit paid-call acknowledgement and two reachable agents",
        ),
        GateResult(
            name="managed_cloud_deployments",
            status="pending-live",
            command="run the Fly, Pipecat Cloud, and LiveKit Cloud commands in docs/GAPS.md",
            detail="requires authenticated paid cloud projects and a registry",
        ),
        GateResult(
            name="managed_object_store",
            status="pending-live",
            command="uv run pytest -m live --no-cov tests/live/test_s3_artifacts_live.py",
            detail="requires an S3-compatible bucket and mutation acknowledgement",
        ),
        GateResult(
            name="pipecat_twilio_warm_transfer",
            status="pending-human",
            command="run the P3.6 two-handset `voicey dev --phone` checklist in docs/GAPS.md",
            detail="requires funded Twilio, a public host, and two physical endpoints",
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
