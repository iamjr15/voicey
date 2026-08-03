"""Aggregate the credential-free P4.1 hardening gates."""

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
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicey/verification/p4-hardening-report.json"),
    )
    parser.add_argument("--short-soak-s", type=float, default=2.0)
    parser.add_argument("--max-concurrent", type=int, default=4)
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    pytest = [sys.executable, "-m", "pytest", "--no-cov", "-q"]

    results = [
        _run(
            "terminal_invariant_chaos",
            [
                *pytest,
                "tests/chaos",
                "tests/integration/test_results_sigkill.py",
                "tests/integration/test_livekit_sigkill.py",
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
            ],
        ),
        _run(
            "runtime_failure_mapping",
            [
                *pytest,
                (
                    "tests/unit/test_pipecat_runtime.py::"
                    "test_session_wait_terminalizes_worker_failure"
                ),
                (
                    "tests/unit/test_pipecat_runtime.py::"
                    "test_worker_events_map_failures_and_idle_timeout"
                ),
                (
                    "tests/unit/test_livekit_runtime_edges.py::"
                    "test_livekit_session_start_wait_end_and_failure_paths"
                ),
                ("tests/unit/test_livekit_runtime_edges.py::test_livekit_close_reason_matrix"),
                ("tests/unit/test_livekit_runtime_edges.py::test_livekit_error_reason_matrix"),
            ],
        ),
        _run(
            "graceful_drain",
            [
                *pytest,
                (
                    "tests/unit/test_pipecat_host.py::"
                    "test_drain_closes_admission_terminalizes_pending_and_changes_readiness"
                ),
                (
                    "tests/unit/test_livekit_host.py::"
                    "test_livekit_drain_rejects_new_work_but_honors_visible_reservation"
                ),
                (
                    "tests/unit/test_deploy_docker.py::"
                    "test_supervisor_owns_signal_drains_then_stops_both_services"
                ),
                "tests/unit/test_results_service_runtime.py",
            ],
        ),
        _run(
            "short_dual_runtime_soak",
            [
                sys.executable,
                str(ROOT / "tests" / "verification" / "p4_soak.py"),
                "--duration-s",
                str(args.short_soak_s),
                "--max-concurrent",
                str(args.max_concurrent),
                "--call-hold-s",
                "0.02",
                "--runtime",
                "both",
                "--report",
                str(report_path.with_name("p4-short-soak-report.json")),
            ],
            timeout_s=max(120.0, args.short_soak_s + 60),
        ),
        _backend_chaos(pytest),
    ]
    results.append(
        GateResult(
            name="full_24h_soak",
            status="pending-time",
            command=(
                "uv run python tests/verification/p4_soak.py --duration-s 86400 "
                f"--max-concurrent {args.max_concurrent} --runtime both"
            ),
            detail="the full 24-hour wall-clock run is not represented by the short CI soak",
        )
    )

    failed = [item for item in results if item.status == "failed"]
    local_pending = [item for item in results if item.status == "pending-local-environment"]
    report = {
        "phase": "P4.1",
        "status": ("failed" if failed else "pending-local" if local_pending else "pending-time"),
        "local_automated_status": ("failed" if failed else "pending" if local_pending else "green"),
        "results": [asdict(item) for item in results],
        "truthfulness": (
            "the bounded CI soak does not promote the required 24-hour wall-clock gate"
        ),
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 1 if failed or local_pending else 0


def _backend_chaos(pytest: list[str]) -> GateResult:
    if not os.environ.get("VOICEY_TEST_POSTGRES_DSN"):
        return GateResult(
            name="sqlite_postgres_chaos_equivalence",
            status="pending-local-environment",
            command=(
                "VOICEY_TEST_POSTGRES_DSN=postgresql://... uv run pytest --no-cov -q "
                "tests/integration/test_repository_backends.py::"
                "test_repository_backend_chaos_invariants"
            ),
            detail="requires a disposable PostgreSQL 17 database",
        )
    return _run(
        "sqlite_postgres_chaos_equivalence",
        [
            *pytest,
            (
                "tests/integration/test_repository_backends.py::"
                "test_repository_backend_chaos_invariants"
            ),
        ],
    )


def _run(
    name: str,
    command: Sequence[str],
    *,
    timeout_s: float = 900,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P4.1 gate] {name}: {rendered}", flush=True)
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
