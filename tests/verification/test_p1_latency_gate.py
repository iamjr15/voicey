from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pipecat.evals.scenario import EvalScenario
from pipecat.evals.suite import EvalManifest

ROOT = Path(__file__).parents[2]
EVALS = ROOT / "recipes" / "appointment-booking" / "pipecat" / "evals"
SCRIPT = Path(__file__).with_name("p1_latency_gate.py")


def test_reference_latency_scenario_is_native_audio_and_has_twenty_user_turns() -> None:
    scenario = EvalScenario.load(EVALS / "audio" / "latency-reference.yaml")
    manifest = EvalManifest.load(EVALS / "latency-suite.yaml")

    assert scenario.user_audio is not None
    assert scenario.bot_audio is True
    assert len([turn for turn in scenario.turns if turn.user is not None]) == 20
    assert all(
        expectation.event == "tts_response"
        for turn in scenario.turns
        for expectation in turn.expect
    )
    assert len(manifest.runs) == 1


def test_latency_gate_reports_missing_live_credentials_without_running(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    environment = os.environ.copy()
    for name in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY", "CARTESIA_API_KEY"):
        environment.pop(name, None)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project",
            str(tmp_path / "not-needed-until-live"),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "pending-live"
    assert payload["missing_environment"] == [
        "DEEPGRAM_API_KEY",
        "ANTHROPIC_API_KEY",
        "CARTESIA_API_KEY",
    ]
    assert json.loads(report.read_text(encoding="utf-8")) == payload
