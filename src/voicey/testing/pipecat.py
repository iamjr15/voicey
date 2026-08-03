"""Compiler from the shared schema to Pipecat Evals 1.6.0 YAML."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voicey.errors import VoiceyError
from voicey.testing.models import (
    JudgeConfig,
    ScenarioDefinition,
    ScenarioTurn,
    TestProfile,
)


@dataclass(frozen=True, slots=True)
class PipecatCompilation:
    """Written native EvalSuite inputs for one profile-expanded run."""

    manifest: Path
    scenarios: tuple[Path, ...]
    runner_bodies: tuple[Path, ...]


def compile_pipecat(
    scenarios: tuple[ScenarioDefinition, ...],
    *,
    output_dir: Path,
    bot: Path | None,
    project_root: Path | None = None,
    audio: bool,
    judge: JudgeConfig,
    planned_turns: dict[tuple[str, str], tuple[ScenarioTurn, ...]] | None = None,
) -> PipecatCompilation:
    """Write native scenario files and a native suite manifest."""
    yaml = _yaml()
    output_dir.mkdir(parents=True, exist_ok=True)
    if bot is None:
        if project_root is None:
            raise VoiceyError("VY-TST-002", detail="Pipecat compiler requires project_root.")
        bot = _write_bot(output_dir, project_root)
    scenario_dir = output_dir / "scenarios"
    body_dir = output_dir / "runner-bodies"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    body_dir.mkdir(parents=True, exist_ok=True)
    scenario_paths: list[Path] = []
    body_paths: list[Path] = []
    suite: list[dict[str, Any]] = []
    for definition in scenarios:
        for profile in definition.profiles:
            key = (definition.name, profile.name)
            turns = definition.turns or (planned_turns or {}).get(key, ())
            turns = tuple(turn for turn in turns if "pipecat" in turn.runtimes)
            if not turns:
                raise VoiceyError(
                    "VY-TST-002",
                    detail=(
                        f"{definition.name}[{profile.name}] has no scripted or "
                        "sim-caller-planned turns."
                    ),
                )
            native_name = _case_name(definition.name, profile.name)
            scenario_path = scenario_dir / f"{native_name}.yaml"
            scenario_path.write_text(
                yaml.safe_dump(
                    _scenario_document(definition, profile, turns, audio=audio, judge=judge),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            body_path = body_dir / f"{native_name}.json"
            body_path.write_text(
                json.dumps(
                    {
                        "voicey_call_id": f"call_eval_{native_name}",
                        "voicey_repository": str(output_dir / "results.sqlite3"),
                        "voicey_transfer_mode": _transfer_mode(turns),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            scenario_paths.append(scenario_path)
            body_paths.append(body_path)
            suite.append(
                {
                    "bot": str(bot.resolve()),
                    "runner_body": str(body_path.resolve()),
                    "scenarios": [native_name],
                }
            )
    manifest_path = output_dir / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "concurrency": 1 if audio else min(4, max(1, len(suite))),
                "runs_dir": str((output_dir / "runs").resolve()),
                "scenarios_dir": str(scenario_dir.resolve()),
                "base_port": 7900,
                "timeout": 90 if audio else 45,
                "spawn": "{python} {bot} -t eval --port {port}",
                "suite": suite,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _validate_native(manifest_path, tuple(scenario_paths))
    return PipecatCompilation(
        manifest=manifest_path,
        scenarios=tuple(scenario_paths),
        runner_bodies=tuple(body_paths),
    )


def _scenario_document(
    definition: ScenarioDefinition,
    profile: TestProfile,
    turns: tuple[ScenarioTurn, ...],
    *,
    audio: bool,
    judge: JudgeConfig,
) -> dict[str, Any]:
    judge_config: dict[str, Any] = {
        "service": judge.service,
        "model": judge.model,
        **({"endpoint": judge.base_url} if judge.service != "ollama" else {}),
    }
    if judge.service == "anthropic":
        judge_config.update(
            {
                "api_key_env": judge.api_key_env,
                "factory": "voicey.testing.pipecat_judge.anthropic_service",
            }
        )
    document: dict[str, Any] = {
        "name": _case_name(definition.name, profile.name),
        "judge": {
            "modality": "audio" if audio else "text",
            "eval": judge_config,
        },
        "turns": [
            _turn_document(
                turn,
                profile,
                definition,
                audio=audio,
                is_final=index == len(turns) - 1,
            )
            for index, turn in enumerate(turns)
        ],
    }
    if audio:
        document["user"] = {
            "modality": "audio",
            "speech": {
                "service": "kokoro",
                "voice": "af_heart",
                "sample_rate": 16000,
            },
        }
        document["judge"]["transcription"] = {
            "service": "moonshine",
            "model": "small-streaming",
            "padding_secs": 0,
        }
    return document


def _turn_document(
    turn: ScenarioTurn,
    profile: TestProfile,
    definition: ScenarioDefinition,
    *,
    audio: bool,
    is_final: bool,
) -> dict[str, Any]:
    native: dict[str, Any] = {}
    if turn.user is not None:
        native["user"] = _render(turn.user, profile)
    if turn.send_after is not None:
        native["send_after"] = {
            **({"event": turn.send_after.event} if turn.send_after.event else {}),
            "delay_ms": turn.send_after.delay_ms,
        }
    expectation = turn.expect
    if expectation is None:
        return native
    events: list[dict[str, Any]] = []
    tools = [tool for tool in expectation.tools if "pipecat" in tool.runtimes]
    if tools:
        events.append(
            {
                "event": "function_call",
                **({"within_ms": expectation.within_ms} if expectation.within_ms else {}),
                "calls": [
                    {
                        "name": tool.name,
                        **({"args": tool.arguments} if tool.arguments else {}),
                    }
                    for tool in tools
                ],
            }
        )
    criteria = [*expectation.judge]
    if expectation.text_contains:
        criteria.append(f"contains the phrase {expectation.text_contains!r}")
    if expectation.handoff:
        criteria.append(f"moves the conversation to {expectation.handoff}")
    criteria.extend(definition.judge if is_final else ())
    if criteria:
        events.append(
            {
                "event": "response",
                **({"within_ms": expectation.within_ms} if expectation.within_ms else {}),
                "eval": "; ".join(criteria),
            }
        )
    elif tools:
        # Pipecat Evals advances as soon as every event for the turn matches.
        # A function_call arrives before the model's post-tool reply, so a
        # tool-only turn must also wait for that reply or the next simulated
        # caller utterance can race the in-flight tool-result completion.
        events.append(
            {
                "event": "response",
                **({"within_ms": expectation.within_ms} if expectation.within_ms else {}),
            }
        )
    native["expect"] = events
    if audio and turn.user is not None:
        native["expect"].insert(0, {"event": "user_transcription"})
    return native


def _render(value: str, profile: TestProfile) -> str:
    try:
        return value.format_map(profile.identity)
    except KeyError as exc:
        raise VoiceyError(
            "VY-TST-001",
            detail=f"profile {profile.name!r} is missing template value {exc.args[0]!r}.",
        ) from exc


def _transfer_mode(turns: tuple[ScenarioTurn, ...]) -> str | None:
    names = {
        tool.name
        for turn in turns
        if turn.expect is not None
        for tool in turn.expect.tools
        if "pipecat" in tool.runtimes
    }
    if "warm_transfer_to_human" in names:
        return "warm"
    if "transfer_to_human" in names:
        return "cold"
    return None


def _case_name(scenario_name: str, profile_name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", f"{scenario_name}_{profile_name}".casefold()).strip("_")


def _yaml() -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise VoiceyError(
            "VY-TST-002",
            detail="Pipecat Evals is unavailable. Install `voicey[pipecat]`.",
        ) from exc
    return yaml


def _validate_native(manifest: Path, scenarios: tuple[Path, ...]) -> None:
    try:
        from pipecat.evals.scenario import EvalScenario
        from pipecat.evals.suite import EvalManifest

        for path in scenarios:
            EvalScenario.load(path)
        EvalManifest.load(manifest)
    except Exception as exc:
        raise VoiceyError(
            "VY-TST-002",
            detail=f"installed Pipecat {sys.version_info.major} compiler rejected generated evals.",
        ) from exc


def _write_bot(output_dir: Path, project_root: Path) -> Path:
    path = output_dir / "voicey_eval_bot.py"
    path.write_text(
        "\n".join(
            (
                '"""Generated native Pipecat Eval transport entrypoint."""',
                "from __future__ import annotations",
                "import sys",
                f"sys.path.insert(0, {str(project_root.resolve())!r})",
                "from agent import agent",
                "from pipecat.runner.types import EvalRunnerArguments, RunnerArguments",
                "from voicey.errors import VoiceyError",
                "from voicey.runtimes.pipecat import run_eval_agent",
                "",
                "async def bot(runner_args: RunnerArguments) -> None:",
                "    if not isinstance(runner_args, EvalRunnerArguments):",
                '        raise VoiceyError("VY-TST-002", detail="expected eval transport")',
                "    await run_eval_agent(agent, runner_args)",
                "",
                'if __name__ == "__main__":',
                "    from pipecat.runner.run import main",
                "    main()",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path
