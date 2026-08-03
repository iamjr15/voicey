"""Run the P1 reference voice stack and gate its persisted end-to-end latency."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from voicey.config.models import Agent
from voicey.obs import LatencySeries, LatencySummary

_REQUIRED_ENV = (
    "DEEPGRAM_API_KEY",
    "ANTHROPIC_API_KEY",
    "CARTESIA_API_KEY",
)
_REFERENCE_MODELS = {
    "stt": "deepgram/nova-3",
    "llm": "anthropic/claude-sonnet-5",
    "tts": "cartesia/sonic-3.5",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--p50-budget-ms", type=float, default=800)
    parser.add_argument("--p95-budget-ms", type=float, default=1500)
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicey/verification/p1-latency-report.json"),
    )
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    report_path = args.report.expanduser().resolve()

    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        report = {
            "gate": "p1_reference_audio_latency",
            "status": "pending-live",
            "missing_environment": missing,
            "next_step": ("inject the three reference-provider keys and rerun this exact command"),
        }
        _write_report(report_path, report)
        print(json.dumps(report, sort_keys=True))
        return 2

    if not project.is_dir():
        parser.error("--project must be a generated voicey project directory")
    actual_models = _load_models(project)
    if actual_models != _REFERENCE_MODELS:
        report = {
            "gate": "p1_reference_audio_latency",
            "status": "failed",
            "reason": "project does not use the locked P1 reference model set",
            "expected_models": _REFERENCE_MODELS,
            "actual_models": actual_models,
        }
        _write_report(report_path, report)
        print(json.dumps(report, sort_keys=True))
        return 1

    with tempfile.TemporaryDirectory(prefix="voicey-p1-latency-") as temporary:
        copied_project = Path(temporary) / "project"
        shutil.copytree(
            project,
            copied_project,
            ignore=shutil.ignore_patterns(
                ".env",
                ".env.*",
                ".git",
                ".voicey",
                "eval-runs",
                "__pycache__",
                "*.pyc",
            ),
        )
        manifest = copied_project / "evals" / "latency-suite.yaml"
        if not manifest.is_file():
            parser.error("--project is missing evals/latency-suite.yaml")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pipecat.evals",
                "suite",
                str(manifest),
                "--runs-dir",
                str(copied_project / "eval-runs" / "latency"),
                "--name",
                "p1-reference",
            ],
            cwd=copied_project,
            check=False,
        )
        database = copied_project / ".voicey" / "evals.db"
        samples = _read_e2e_samples(database) if database.is_file() else []

    summary = summarize_latency(samples)
    distinct_turns = len({(call_id, turn_index) for call_id, turn_index, _ in samples})
    enough_samples = (
        summary is not None
        and summary.count >= args.minimum_samples
        and distinct_turns >= args.minimum_samples
    )
    within_budget = (
        summary is not None
        and summary.p50_ms <= args.p50_budget_ms
        and summary.p95_ms <= args.p95_budget_ms
    )
    passed = completed.returncode == 0 and enough_samples and within_budget
    report = {
        "gate": "p1_reference_audio_latency",
        "status": "green" if passed else "failed",
        "models": actual_models,
        "eval_exit_code": completed.returncode,
        "minimum_samples": args.minimum_samples,
        "p50_budget_ms": args.p50_budget_ms,
        "p95_budget_ms": args.p95_budget_ms,
        "summary": None if summary is None else summary.model_dump(mode="json"),
        "distinct_turns": distinct_turns,
        "source": "native Pipecat UserBotLatencyObserver persisted e2e samples",
    }
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if passed else 1


def _load_models(project: Path) -> dict[str, str]:
    module_path = project / "agent.py"
    spec = importlib.util.spec_from_file_location("voicey_p1_latency_agent", module_path)
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = _module_agent(module)
    return {
        "stt": agent.models.stt,
        "llm": agent.models.llm,
        "tts": agent.models.tts,
    }


def _module_agent(module: ModuleType) -> Agent:
    candidate = cast(Any, module).agent
    if not isinstance(candidate, Agent):
        raise TypeError("agent.py must expose a voicey Agent as `agent`")
    return candidate


def _read_e2e_samples(database: Path) -> list[tuple[str, int, float]]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT call_id, turn_index, duration_ms
            FROM call_latency
            WHERE metric = 'e2e'
            ORDER BY sequence
            """
        ).fetchall()
    return [
        (str(call_id), int(turn_index), float(duration_ms))
        for call_id, turn_index, duration_ms in rows
    ]


def summarize_latency(samples: list[tuple[str, int, float]]) -> LatencySummary | None:
    if not samples:
        return None
    series = LatencySeries()
    observed_at = datetime.now(UTC)
    for call_id, turn_index, duration_ms in samples:
        series.record(
            turn_id=f"{call_id}:turn_{turn_index:04d}",
            turn_index=turn_index,
            metric="e2e",
            duration_ms=duration_ms,
            observed_at=observed_at,
        )
    return series.summaries()["e2e"]


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
