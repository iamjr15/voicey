from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from pipecat.evals.scenario import EvalScenario
from pipecat.evals.suite import EvalManifest
from pipecat.evals.transport import EvalTransportParams
from pipecat.runner.types import EvalRunnerArguments

from voicekit import Agent, Behavior, Models, Phone, Results, Web, results
from voicekit.cli.scaffold import ScaffoldWriter, ScratchScaffold
from voicekit.config.manifest import ManifestStore, ProjectManifest, RecipeSelection
from voicekit.errors import VoicekitError
from voicekit.recipes.source import install_recipe, recipe_files
from voicekit.runtimes.pipecat.evals import run_eval_agent
from voicekit.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).parents[2]
RECIPE = ROOT / "recipes" / "appointment-booking"


def _agent(*, runtime: str = "pipecat") -> Agent:
    return Agent(
        name="appointment-agent",
        runtime=runtime,  # type: ignore[arg-type]
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Help callers manage appointments.",
        flow="flow:entry",
        tools="tools",
        phone=Phone(
            provider="twilio",
            number="+15555550198",
            inbound=True,
            outbound=False,
        ),
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        behavior=Behavior(
            voicemail="leave_message",
            transfer_number="+15555550199",
        ),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


def _load_recipe_tools() -> ModuleType:
    name = "voicekit_test_appointment_tools"
    spec = importlib.util.spec_from_file_location(name, RECIPE / "tools.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_recipe_source_selects_native_variant_and_never_overwrites(tmp_path: Path) -> None:
    files = recipe_files("appointment-booking", "pipecat")

    assert "flow.py" in files
    assert "eval_bot.py" in files
    assert "pipecat.flows" in files["flow.py"]
    assert all(not path.startswith("pipecat/") for path in files)
    assert "livekit" not in files

    written = install_recipe(tmp_path, name="appointment-booking", runtime="pipecat")
    assert written
    assert install_recipe(tmp_path, name="appointment-booking", runtime="pipecat") == ()

    (tmp_path / "flow.py").write_text("# user-owned\n", encoding="utf-8")
    with pytest.raises(VoicekitError) as conflict:
        install_recipe(tmp_path, name="appointment-booking", runtime="pipecat")
    assert conflict.value.code == "VK-CLI-003"
    assert (tmp_path / "flow.py").read_text(encoding="utf-8") == "# user-owned\n"

    with pytest.raises(VoicekitError, match="livekit"):
        recipe_files("appointment-booking", "livekit")


def test_recipe_scaffold_is_complete_native_and_manifested(tmp_path: Path) -> None:
    manifest = ProjectManifest(
        project_name="appointment-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="appointment-booking", version="1.0.0"),
        channels=frozenset({"web"}),
        models={
            "stt": "deepgram/nova-3",
            "llm": "anthropic/claude-sonnet-5",
            "tts": "cartesia/sonic-3.5",
        },
    )
    scaffold = ScratchScaffold(
        project_name="appointment-agent",
        description="Book, reschedule, and cancel appointments.",
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
        recipe_name="appointment-booking",
    )

    written = ScaffoldWriter().write(tmp_path, scaffold, manifest)

    assert len(written) >= 29
    for relative in ("agent.py", "flow.py", "tools.py", "eval_bot.py"):
        compile((tmp_path / relative).read_text(encoding="utf-8"), relative, "exec")
    assert "Behavior(" in (tmp_path / "agent.py").read_text(encoding="utf-8")
    assert "VOICEKIT_TRANSFER_NUMBER=" in (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert ManifestStore(tmp_path / "voicekit.jsonc").load().recipe.name == ("appointment-booking")


def test_calendar_stub_is_typed_deterministic_and_records_outcomes() -> None:
    tools = _load_recipe_tools()
    first = tools.search_available_slots("2026-08-05", "America/New_York")
    second = tools.search_available_slots("2026-08-05", "America/New_York")
    buffer = results.CallResultBuffer(call_id="call_recipe")

    with results.result_context(buffer):
        booked = tools.book_appointment(
            "2026-08-05T11:30:00",
            "America/New_York",
            "Alex Rivera",
            "alex@example.com",
            "consultation",
        )
        moved = tools.reschedule_appointment(
            booked["reference"],
            "2026-08-05T15:00:00",
            "America/New_York",
        )
        cancelled = tools.cancel_appointment(booked["reference"])

    assert first == second
    assert first["status"] == "available"
    assert str(booked["reference"]).startswith("APT-")
    assert moved["status"] == "rescheduled"
    assert cancelled["status"] == "cancelled"
    assert buffer.outcome == "appointment_cancelled"
    assert buffer.data["appointment"] == cancelled
    with pytest.raises(ValueError, match="ISO date"):
        tools.search_available_slots("2026-13-40", "America/New_York")


def test_pipecat_eval_scenarios_and_manifests_load_on_the_installed_pin() -> None:
    evals = RECIPE / "pipecat" / "evals"
    scenarios = [
        EvalScenario.load(path)
        for directory in ("text", "audio")
        for path in sorted((evals / directory).glob("*.yaml"))
    ]
    text_manifest = EvalManifest.load(evals / "text-suite.yaml")
    audio_manifest = EvalManifest.load(evals / "audio-suite.yaml")
    latency_manifest = EvalManifest.load(evals / "latency-suite.yaml")

    assert len(scenarios) == 11
    assert len(text_manifest.runs) == 7
    assert len(audio_manifest.runs) == 3
    assert len(latency_manifest.runs) == 1
    assert all(
        manifest.spawn.endswith("-t eval --port {port}")
        for manifest in (
            text_manifest,
            audio_manifest,
            latency_manifest,
        )
    )
    assert any(scenario.user_audio is not None and scenario.bot_audio for scenario in scenarios)
    assert any(
        call.name == "transfer_to_human"
        for scenario in scenarios
        for turn in scenario.turns
        for expectation in turn.expect
        for call in (expectation.calls or [])
    )
    assert any(turn.send_after is not None for scenario in scenarios for turn in scenario.turns)
    assert {cast(str, scenario.judge.get("service")) for scenario in scenarios} == {"ollama"}


def test_pipecat_evals_cli_exit_code_contract(tmp_path: Path) -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "pipecat.evals", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    failure_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pipecat.evals",
            "suite",
            str(tmp_path / "missing.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert help_result.returncode == 0
    assert "Run behavioral evals" in help_result.stdout
    assert failure_result.returncode == 1


@pytest.mark.asyncio
async def test_eval_runtime_uses_native_transport_and_terminal_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = await SQLiteRepository(tmp_path / "evals.sqlite3").open()
    observed: dict[str, Any] = {}

    async def fake_create_transport(
        runner_args: EvalRunnerArguments,
        params: dict[str, Any],
    ) -> object:
        observed["runner_args"] = runner_args
        eval_params = params["eval"]()
        assert isinstance(eval_params, EvalTransportParams)
        return object()

    class FakeSession:
        def __init__(self, lifecycle: Any) -> None:
            self.lifecycle = lifecycle

        async def start(self, _runner: object) -> None:
            observed["started"] = True

        async def wait(self) -> object:
            return await self.lifecycle.finish("agent_hangup", provider_state="completed")

        async def end(self, _reason: str) -> None:
            return

    class FakeBuilder:
        def __init__(self, _repository: object, *, transfer_handler: object) -> None:
            observed["transfer_handler"] = transfer_handler

        def build(self, **kwargs: Any) -> FakeSession:
            observed["sample_rate"] = kwargs["sample_rate"]
            return FakeSession(kwargs["lifecycle"])

    class FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            observed["runner_options"] = kwargs

        async def run(self) -> None:
            return

    monkeypatch.setattr("voicekit.runtimes.pipecat.evals.create_transport", fake_create_transport)
    monkeypatch.setattr("voicekit.runtimes.pipecat.evals.PipecatSessionBuilder", FakeBuilder)
    monkeypatch.setattr("voicekit.runtimes.pipecat.evals.WorkerRunner", FakeRunner)

    await run_eval_agent(
        _agent(),
        EvalRunnerArguments(session_id="unit"),
        repository=repository,
    )
    record = await repository.get_call("call_eval_unit")
    await repository.close()

    assert observed["started"] is True
    assert observed["sample_rate"] == 16000
    assert record.status == "completed"


@pytest.mark.asyncio
async def test_eval_runtime_rejects_the_other_runtime() -> None:
    with pytest.raises(VoicekitError) as caught:
        await run_eval_agent(_agent(runtime="livekit"), EvalRunnerArguments())
    assert caught.value.code == "VK-RUN-001"


@pytest.mark.asyncio
async def test_eval_runtime_terminalizes_transport_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = await SQLiteRepository(tmp_path / "failed-evals.sqlite3").open()

    async def fail_transport(
        _runner_args: EvalRunnerArguments,
        _params: dict[str, Any],
    ) -> object:
        raise RuntimeError("eval port unavailable")

    monkeypatch.setattr("voicekit.runtimes.pipecat.evals.create_transport", fail_transport)

    with pytest.raises(RuntimeError, match="port unavailable"):
        await run_eval_agent(
            _agent(),
            EvalRunnerArguments(session_id="setup_failure"),
            repository=repository,
        )
    record = await repository.get_call("call_eval_setup_failure")
    await repository.close()

    assert record.status == "failed"
