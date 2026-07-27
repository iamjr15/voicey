"""Aggregate every automatable P1 exit check without promoting live/manual gaps."""

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
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--latency-project", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p1-gate-report.json"),
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
            "fresh_wheel_quickstart",
            [
                python,
                str(ROOT / "tests" / "verification" / "p1_quickstart.py"),
                "--wheel",
                str(wheel),
            ],
            timeout_s=360,
        ),
        _run_gate(
            "cli_contract",
            [
                *pytest,
                "tests/verification/test_p1_cli_matrix.py",
                "tests/unit/test_cli.py",
                "tests/unit/test_cli_prompts.py",
            ],
        ),
        _run_gate(
            "parallel_call_isolation",
            [
                *pytest,
                (
                    "tests/unit/test_tools.py::"
                    "test_parallel_sync_tools_preserve_call_and_result_isolation"
                ),
                ("tests/unit/test_results_recorder.py::test_parallel_result_contexts_do_not_leak"),
            ],
        ),
        _run_gate(
            "terminal_event_chaos",
            [
                *pytest,
                (
                    "tests/unit/test_results_repository.py::"
                    "test_terminal_event_and_outbox_roll_back_together_on_insert_failure"
                ),
                (
                    "tests/unit/test_results_repository.py::"
                    "test_generation_fences_delayed_heartbeat_and_late_completion"
                ),
                (
                    "tests/unit/test_results_repository.py::"
                    "test_two_simultaneous_sweepers_emit_one_terminal_with_partial_transcript"
                ),
                (
                    "tests/integration/test_results_sigkill.py::"
                    "test_sigkill_recovery_persists_partial_transcript_and_one_terminal"
                ),
            ],
        ),
        _run_gate(
            "tunneled_admin_negative",
            [
                *pytest,
                (
                    "tests/unit/test_playground_service.py::"
                    "test_admin_host_origin_and_deployed_authorizer_are_enforced"
                ),
                (
                    "tests/unit/test_playground_service.py::"
                    "test_public_listener_only_exposes_cors_protected_signaling"
                ),
            ],
        ),
        _run_gate(
            "twilio_local_certification",
            [
                *pytest,
                "tests/certification/test_twilio_adapter.py",
                "tests/certification/test_twilio_media.py",
            ],
        ),
        _run_gate(
            "docker_contract",
            [*pytest, "tests/unit/test_deploy_docker.py"],
        ),
    ]

    if args.require_live:
        live_missing = required_live_prerequisites(os.environ)
        if args.latency_project is None:
            live_missing.append("--latency-project")
        if live_missing:
            results.append(
                GateResult(
                    name="credentialed_live_gates",
                    status="failed",
                    command="tests/verification/run_p1_gate.py --require-live",
                    detail="missing prerequisites: " + ", ".join(live_missing),
                )
            )
        else:
            latency_project = args.latency_project
            assert latency_project is not None
            results.extend(
                [
                    _run_gate(
                        "reference_audio_latency",
                        [
                            python,
                            str(ROOT / "tests" / "verification" / "p1_latency_gate.py"),
                            "--project",
                            str(latency_project.expanduser().resolve()),
                        ],
                        timeout_s=1800,
                    ),
                    _run_gate(
                        "twilio_live_certification",
                        [
                            *pytest,
                            "-m",
                            "live",
                            "tests/live/test_twilio_live.py",
                        ],
                        timeout_s=600,
                    ),
                ]
            )
    else:
        results.extend(_pending_external_results())

    failed = [result for result in results if result.status == "failed"]
    pending = [result for result in results if result.status.startswith("pending")]
    report = {
        "phase": "P1",
        "status": "failed" if failed else ("pending-live" if pending else "green"),
        "local_automated_status": "failed" if failed else "green",
        "results": [asdict(result) for result in results],
        "failed_count": len(failed),
        "pending_count": len(pending),
        "truthfulness": (
            "pending-live and pending-human results are never promoted by local substitutes"
        ),
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 1 if failed else 0


def required_live_prerequisites(environment: Mapping[str, str]) -> list[str]:
    required = (
        "DEEPGRAM_API_KEY",
        "ANTHROPIC_API_KEY",
        "CARTESIA_API_KEY",
        "TWILIO_TEST_ACCOUNT_SID",
        "TWILIO_TEST_AUTH_TOKEN",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "VOICEKIT_TWILIO_LIVE_FROM",
        "VOICEKIT_TWILIO_LIVE_TO",
        "VOICEKIT_TWILIO_TRANSFER_TO",
        "VOICEKIT_LIVE_PUBLIC_BASE",
    )
    missing = [name for name in required if not environment.get(name)]
    acknowledgements = {
        "VOICEKIT_LIVE_ROUTE_CONFIRM": "I_ACKNOWLEDGE_ROUTE_MUTATION",
        "VOICEKIT_LIVE_CONFIRM": "I_ACKNOWLEDGE_PSTN_CHARGES",
    }
    missing.extend(
        name for name, value in acknowledgements.items() if environment.get(name) != value
    )
    return missing


def _run_gate(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 300,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P1 gate] {name}: {rendered}", flush=True)
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
            name="reference_audio_latency",
            status="pending-live",
            command=(
                "uv run python tests/verification/p1_latency_gate.py "
                '--project "$VOICEKIT_EVAL_PROJECT"'
            ),
            detail="requires the three locked reference-provider credentials",
        ),
        GateResult(
            name="twilio_live_certification",
            status="pending-live",
            command="uv run pytest -m live --no-cov tests/live/test_twilio_live.py",
            detail=(
                "requires test/live credentials, funded PSTN, public runtime, and acknowledgements"
            ),
        ),
        GateResult(
            name="cloudflared_public_edge",
            status="pending-live",
            command=(
                "VOICEKIT_LIVE_TUNNEL_CONFIRM=I_ACKNOWLEDGE_PUBLIC_TUNNEL "
                "uv run pytest -m live --no-cov tests/live/test_tunnel_live.py"
            ),
            detail="latest generated quick-tunnel hostname did not resolve",
        ),
        GateResult(
            name="manual_human_surfaces",
            status="pending-human",
            command="follow the P1 CLI/playground/handset commands in docs/GAPS.md",
            detail="wizard usability, broken-machine doctor, microphone, and physical handset",
        ),
        GateResult(
            name="docker_public_paid_smoke",
            status="pending-live",
            command="follow the P1 Docker public deployment command in docs/GAPS.md",
            detail="requires public TLS ingress, funded Twilio resources, and a destination",
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
