from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from xml.etree import ElementTree

import httpx
import pytest
from livekit import rtc
from pipecat.evals.scenario import EvalScenario
from pipecat.evals.suite import EvalManifest
from typer.testing import CliRunner

from voicey.cli.app import app
from voicey.errors import VoiceyError
from voicey.testing import (
    JudgeConfig,
    Persona,
    ResultExpectation,
    ScenarioDefinition,
    ScenarioTurn,
    SendAfter,
    ToolExpectation,
    TurnExpectation,
    discover_scenarios,
    scenario,
)
from voicey.testing import TestProfile as ScenarioProfile
from voicey.testing import livekit_audio as audio_testing
from voicey.testing import runner as testing_runner
from voicey.testing.discovery import ScenarioFunction
from voicey.testing.livekit import assert_native_turn, compile_livekit
from voicey.testing.livekit_audio import CapturingAudioOutput, QueueAudioInput
from voicey.testing.models import matches_expected_data
from voicey.testing.pipecat import compile_pipecat
from voicey.testing.reporting import (
    AttemptResult,
    CaseResult,
    SuiteResult,
    result_json,
    write_junit,
)
from voicey.testing.runner import (
    CaseExecutor,
    LiveKitExecutor,
    PipecatExecutor,
    run_project_tests,
)
from voicey.testing.sim_caller import (
    AnthropicMessagesClient,
    JudgeDecision,
    OpenAICompatibleClient,
    SimCaller,
    TranscriptJudge,
    build_model_client,
    load_testing_config,
)

cli_runner = CliRunner()


def test_public_scenario_contract_accepts_spec_shape() -> None:
    def ends_at_eight(value: Any) -> bool:
        return str(value).endswith("20:00")

    @scenario
    def changes_mind() -> Mapping[str, Any]:
        return {
            "caller": "Busy parent, slightly distracted.",
            "goals": ["end with a confirmed booking"],
            "expect": {
                "outcome": "booked",
                "data": {"slot": ends_at_eight},
            },
            "judge": ["confirmed the final time"],
            "max_turns": 24,
        }

    definition = ScenarioDefinition.model_validate(
        {"name": changes_mind.__name__, **changes_mind()}
    )
    assert definition.persona.description.startswith("Busy parent")
    assert definition.metrics.max_turns == 24
    assert definition.expect is not None
    assert (
        matches_expected_data(
            definition.expect.data,
            {"slot": "2026-08-05T20:00"},
        )
        == []
    )


def test_scenario_rejects_parameters_and_predicate_failures_are_safe() -> None:
    def invalid(value: str) -> Mapping[str, Any]:
        return {"caller": value, "goals": ["x"]}

    with pytest.raises(VoiceyError, match="VY-TST-001"):
        scenario(cast(ScenarioFunction, invalid))

    def false_predicate(_value: Any) -> bool:
        return False

    def raising_predicate(_value: Any) -> bool:
        raise ZeroDivisionError

    assert matches_expected_data(
        {
            "missing": 1,
            "bad": false_predicate,
            "raises": raising_predicate,
        },
        {"bad": 1, "raises": 2},
    ) == [
        "data.missing is missing",
        "data.bad did not satisfy its predicate",
        "data.raises predicate raised ZeroDivisionError",
    ]


def test_scenario_models_reject_every_ambiguous_empty_contract() -> None:
    invalid_factories = (
        lambda: Persona(description=""),
        lambda: ScenarioProfile(name=""),
        lambda: JudgeConfig(service="openai"),
        lambda: JudgeConfig(service="anthropic"),
        lambda: JudgeConfig(api_key_env="UNEXPECTED"),
        lambda: ToolExpectation(name=""),
        lambda: TurnExpectation(),
        lambda: ScenarioTurn(),
        lambda: ScenarioTurn(
            expect=TurnExpectation(judge=("waits",)),
            send_after=SendAfter(delay_ms=1),
        ),
        lambda: ScenarioTurn(user="hello", runtimes=frozenset()),
        lambda: ResultExpectation(),
        lambda: ScenarioDefinition(name="", caller="caller", goals=("help",)),
        lambda: ScenarioDefinition(name="x", caller="caller", goals=("",)),
        lambda: ScenarioDefinition(name="x", caller="caller", goals=()),
        lambda: ScenarioDefinition(
            name="x",
            caller="caller",
            goals=("help",),
            profiles=(ScenarioProfile(name="same"), ScenarioProfile(name="same")),
        ),
        lambda: ScenarioDefinition(
            name="x",
            caller="caller",
            goals=("help",),
            max_turns=1,
            turns=(ScenarioTurn(user="one"), ScenarioTurn(user="two")),
        ),
    )
    for factory in invalid_factories:
        with pytest.raises(ValueError, match=r".+"):
            factory()

    persona = Persona(description="A careful caller.")
    definition = ScenarioDefinition(
        name="valid",
        caller=persona,
        goals=("get help",),
    )
    assert definition.persona is persona
    assert matches_expected_data({"value": 2}, {"value": 1}) == ["data.value expected 2, got 1"]


def test_discovery_loads_documented_locations_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "scenarios.py").write_text(
        "\n".join(
            (
                "from voicey.testing import scenario",
                "@scenario",
                "def alpha():",
                "    return {'caller': 'A', 'goals': ['help']}",
            )
        ),
        encoding="utf-8",
    )
    nested = tests / "scenarios"
    nested.mkdir()
    (nested / "more.py").write_text(
        "\n".join(
            (
                "from voicey.testing import scenario",
                "@scenario",
                "def beta():",
                "    return {'caller': 'B', 'goals': ['help']}",
            )
        ),
        encoding="utf-8",
    )
    assert [item.name for item in discover_scenarios(tmp_path)] == ["alpha", "beta"]

    (nested / "more.py").write_text(
        "\n".join(
            (
                "from voicey.testing import scenario, ScenarioDefinition",
                "@scenario",
                "def duplicate():",
                "    return ScenarioDefinition(name='alpha', caller='B', goals=('help',))",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(VoiceyError, match="duplicate scenario names"):
        discover_scenarios(tmp_path)


def test_discovery_reports_missing_empty_import_and_invalid_return(
    tmp_path: Path,
) -> None:
    with pytest.raises(VoiceyError, match="no tests/scenarios"):
        discover_scenarios(tmp_path)

    tests = tmp_path / "tests"
    tests.mkdir()
    source = tests / "scenarios.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(VoiceyError, match="contain no @scenario"):
        discover_scenarios(tmp_path)

    source.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with pytest.raises(VoiceyError, match="raised RuntimeError"):
        discover_scenarios(tmp_path)

    source.write_text(
        "\n".join(
            (
                "from voicey.testing import scenario",
                "@scenario",
                "def invalid():",
                "    return {'caller': 'caller'}",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(VoiceyError, match="returned an invalid scenario"):
        discover_scenarios(tmp_path)


def _definition() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="booking",
        caller="Caller who books a slot.",
        goals=("book the selected slot",),
        expect=ResultExpectation(
            outcome="appointment_booked",
            data={"appointment.status": "booked"},
        ),
        judge=("agent confirms the final slot",),
        profiles=(ScenarioProfile(name="alex", identity={"email": "alex@example.com"}),),
        turns=(
            ScenarioTurn(
                user="Book for {email}.",
                expect=TurnExpectation(
                    tools=(
                        ToolExpectation(
                            name="book_appointment",
                            arguments={"email": "alex@example.com"},
                        ),
                    ),
                    judge=("confirms a booking",),
                    within_ms=1500,
                ),
            ),
        ),
    )


def test_pipecat_compiler_emits_installed_native_text_and_audio(
    tmp_path: Path,
) -> None:
    bot = Path("recipes/appointment-booking/pipecat/eval_bot.py")
    text = compile_pipecat(
        (_definition(),),
        output_dir=tmp_path / "text",
        bot=bot,
        audio=False,
        judge=JudgeConfig(),
    )
    native_text = EvalScenario.load(text.scenarios[0])
    manifest = EvalManifest.load(text.manifest)
    assert native_text.turns[0].user == "Book for alex@example.com."
    assert native_text.turns[0].expect[0].within_ms == 1500
    assert manifest.runs[0].runner_body_path == text.runner_bodies[0]

    audio = compile_pipecat(
        (_definition(),),
        output_dir=tmp_path / "audio",
        bot=bot,
        audio=True,
        judge=JudgeConfig(),
    )
    native_audio = EvalScenario.load(audio.scenarios[0])
    assert native_audio.user_audio == {
        "service": "kokoro",
        "voice": "af_heart",
        "sample_rate": 16000,
    }
    assert native_audio.transcriber is not None


def test_pipecat_tool_only_turn_waits_for_post_tool_response(tmp_path: Path) -> None:
    definition = _definition().model_copy(
        update={
            "judge": (),
            "turns": (
                ScenarioTurn(
                    user="Search for {email}.",
                    expect=TurnExpectation(
                        tools=(ToolExpectation(name="search_available_slots"),),
                        within_ms=1500,
                    ),
                ),
                ScenarioTurn(
                    user="Choose the first slot.",
                    expect=TurnExpectation(judge=("asks for confirmation",)),
                ),
            ),
        }
    )
    compiled = compile_pipecat(
        (definition,),
        output_dir=tmp_path / "tool-only",
        bot=Path("recipes/appointment-booking/pipecat/eval_bot.py"),
        audio=False,
        judge=JudgeConfig(),
    )

    native = EvalScenario.load(compiled.scenarios[0])
    events = native.turns[0].expect
    assert [event.event for event in events] == ["function_call", "llm_response"]
    assert events[1].eval is None
    assert events[1].within_ms == 1500


def test_pipecat_compiler_selects_warm_transfer_eval_handler(tmp_path: Path) -> None:
    definition = _definition().model_copy(
        update={
            "turns": (
                ScenarioTurn(
                    user="I consent to the handoff.",
                    expect=TurnExpectation(tools=(ToolExpectation(name="warm_transfer_to_human"),)),
                ),
            )
        }
    )
    compiled = compile_pipecat(
        (definition,),
        output_dir=tmp_path / "warm-transfer",
        bot=Path("recipes/appointment-booking/pipecat/eval_bot.py"),
        audio=False,
        judge=JudgeConfig(),
    )

    body = json.loads(compiled.runner_bodies[0].read_text(encoding="utf-8"))
    assert body["voicey_transfer_mode"] == "warm"


def test_pipecat_compiler_generates_bot_and_requires_resolved_turns(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    definition = _definition().model_copy(update={"turns": ()})
    with pytest.raises(VoiceyError, match="no scripted or sim-caller-planned turns"):
        compile_pipecat(
            (definition,),
            output_dir=tmp_path / "missing",
            bot=None,
            project_root=project,
            audio=False,
            judge=JudgeConfig(),
        )

    compiled = compile_pipecat(
        (definition,),
        output_dir=tmp_path / "generated",
        bot=None,
        project_root=project,
        audio=False,
        judge=JudgeConfig(),
        planned_turns={
            ("booking", "alex"): (
                ScenarioTurn(
                    user="Hello",
                    expect=TurnExpectation(judge=("responds",)),
                ),
            )
        },
    )
    generated_bot = compiled.manifest.parent / "voicey_eval_bot.py"
    assert "run_eval_agent(agent, runner_args)" in generated_bot.read_text(encoding="utf-8")


def test_pipecat_compiler_covers_timing_handoff_cloud_and_template_errors(
    tmp_path: Path,
) -> None:
    with pytest.raises(VoiceyError, match="requires project_root"):
        compile_pipecat(
            (_definition(),),
            output_dir=tmp_path / "no-project",
            bot=None,
            audio=False,
            judge=JudgeConfig(),
        )

    profile = ScenarioProfile(name="cloud", identity={"email": "cloud@example.com"})
    definition = ScenarioDefinition(
        name="Cloud / handoff",
        caller="caller",
        goals=("reach a specialist",),
        judge=("the handoff is appropriate",),
        profiles=(profile,),
        turns=(
            ScenarioTurn(
                user="Hello {email}",
                send_after=SendAfter(event="bot_ready", delay_ms=10),
            ),
            ScenarioTurn(
                expect=TurnExpectation(handoff="SpecialistAgent"),
            ),
        ),
    )
    compilation = compile_pipecat(
        (definition,),
        output_dir=tmp_path / "cloud",
        bot=Path("recipes/appointment-booking/pipecat/eval_bot.py"),
        audio=False,
        judge=JudgeConfig(
            service="openai",
            model="cloud",
            base_url="https://example.test/v1",
            api_key_env="TEST_KEY",  # pragma: allowlist secret
        ),
    )
    native = EvalScenario.load(compilation.scenarios[0])
    assert native.name == "cloud_handoff_cloud"
    assert native.turns[0].send_after is not None
    criterion = native.turns[1].expect[0].eval
    assert criterion is not None
    assert "moves the conversation to SpecialistAgent" in criterion

    anthropic_compilation = compile_pipecat(
        (definition,),
        output_dir=tmp_path / "anthropic",
        bot=Path("recipes/appointment-booking/pipecat/eval_bot.py"),
        audio=False,
        judge=JudgeConfig(
            service="anthropic",
            model="claude-sonnet-5",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_API_KEY",  # pragma: allowlist secret
        ),
    )
    anthropic_native = EvalScenario.load(anthropic_compilation.scenarios[0])
    assert anthropic_native.judge["service"] == "anthropic"
    assert anthropic_native.judge["factory"] == "voicey.testing.pipecat_judge.anthropic_service"

    missing_value = definition.model_copy(
        update={
            "turns": (
                ScenarioTurn(user="Hello {missing}"),
                ScenarioTurn(expect=TurnExpectation(handoff="SpecialistAgent")),
            )
        }
    )
    with pytest.raises(VoiceyError, match="missing template value 'missing'"):
        compile_pipecat(
            (missing_value,),
            output_dir=tmp_path / "missing-value",
            bot=Path("recipes/appointment-booking/pipecat/eval_bot.py"),
            audio=False,
            judge=JudgeConfig(),
        )


def test_livekit_compiler_expands_profiles_without_conversation_dsl() -> None:
    compiled = compile_livekit((_definition(),))
    assert compiled[0].name == "booking[alex]"
    assert compiled[0].turns[0].user == "Book for alex@example.com."
    assert compiled[0].turns[0].source.expect is not None


@pytest.mark.asyncio
async def test_openai_compatible_planner_and_cited_judge() -> None:
    replies = iter(
        (
            '["Book Tuesday.", "Actually make it Wednesday.", "Confirm."]',
            '{"passed":true,"reason":"line 2 has the correction","citations":[2]}',
            '{"passed":true,"reason":"unsupported","citations":[]}',
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["seed"] == 7
        assert payload["think"] is False
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(replies)}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleClient(JudgeConfig(), client=http_client)
        definition = _definition().model_copy(update={"turns": ()})
        turns = await SimCaller(client).plan(definition, definition.profiles[0])
        assert len(turns) == 3
        assert turns[-1].expect is not None
        judge = TranscriptJudge(client)
        passed = await judge.evaluate(
            ("uses the correction",),
            ("caller: Tuesday", "caller: Wednesday"),
            seed=7,
        )
        assert passed.passed is True
        assert passed.citations == (2,)
        uncited = await judge.evaluate(
            ("uses the correction",),
            ("caller: Tuesday", "caller: Wednesday"),
            seed=7,
        )
        assert uncited.passed is False
        assert "without a valid transcript citation" in uncited.reason


class _SequenceCompletionClient:
    def __init__(self, *replies: str) -> None:
        self._replies = iter(replies)

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        seed: int,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        del seed, response_schema
        return next(self._replies)


@pytest.mark.asyncio
async def test_sim_caller_and_judge_reject_malformed_model_outputs() -> None:
    definition = _definition().model_copy(update={"turns": (), "max_turns": 1})
    invalid_plans = ("{}", "[]", '["one", "two"]')
    for reply in invalid_plans:
        client = cast(
            OpenAICompatibleClient,
            _SequenceCompletionClient(reply),
        )
        with pytest.raises(VoiceyError, match="VY-TST-003"):
            await SimCaller(client).plan(definition, definition.profiles[0])

    no_criteria = await TranscriptJudge(
        cast(OpenAICompatibleClient, _SequenceCompletionClient())
    ).evaluate((), (), seed=7)
    assert no_criteria.passed is True

    for reply in ('["not an object"]', '{"passed":"yes"}'):
        judge = TranscriptJudge(cast(OpenAICompatibleClient, _SequenceCompletionClient(reply)))
        with pytest.raises(VoiceyError, match="VY-TST-003"):
            await judge.evaluate(("criterion",), ("agent: answer",), seed=7)


@pytest.mark.asyncio
async def test_model_client_requires_cloud_key_and_maps_bad_response() -> None:
    cloud = JudgeConfig(
        service="openai",
        model="cloud",
        base_url="https://example.test/v1",
        api_key_env="CLOUD_KEY",  # pragma: allowlist secret
    )
    with pytest.raises(VoiceyError, match="CLOUD_KEY is required"):
        await OpenAICompatibleClient(cloud, environment={}).complete([], seed=7)

    async def invalid_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_handler)) as http_client:
        with pytest.raises(VoiceyError, match="returned invalid output"):
            await OpenAICompatibleClient(
                JudgeConfig(),
                client=http_client,
            ).complete([], seed=7)


@pytest.mark.asyncio
async def test_native_anthropic_model_client_separates_system_and_omits_temperature() -> None:
    captured: dict[str, Any] = {}

    class FakeMessages:
        async def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"passed":true}')])

    client = SimpleNamespace(messages=FakeMessages())
    config = JudgeConfig(
        service="anthropic",
        model="claude-sonnet-5",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",  # pragma: allowlist secret
    )
    adapter = AnthropicMessagesClient(
        config,
        environment={"ANTHROPIC_API_KEY": "test"},  # pragma: allowlist secret
        client=client,
    )

    response = await adapter.complete(
        [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Evaluate."},
        ],
        seed=7,
        response_schema={"type": "object"},
    )

    assert response == '{"passed":true}'
    assert captured["system"] == "Return JSON."
    assert captured["messages"] == [{"role": "user", "content": "Evaluate."}]
    assert "temperature" not in captured
    assert captured["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }
    assert isinstance(build_model_client(config, environment={}), AnthropicMessagesClient)


def test_testing_config_defaults_local_and_validates_cloud(tmp_path: Path) -> None:
    default_config = load_testing_config(tmp_path)
    assert default_config.judge.service == "ollama"
    assert default_config.judge.model == "qwen3:8b"
    assert default_config.sim_caller.model == "qwen3:8b"
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "voicey-test.jsonc").write_text(
        """
        {
          judge: {
            service: "openai",
            model: "gpt-5-mini",
            base_url: "https://api.openai.com/v1",
            api_key_env: "OPENAI_API_KEY", // pragma: allowlist secret
          },
        }
        """,
        encoding="utf-8",
    )
    assert (
        load_testing_config(tmp_path).judge.api_key_env
        == "OPENAI_API_KEY"  # pragma: allowlist secret
    )

    (tests / "voicey-test.jsonc").write_text("{ judge: false }", encoding="utf-8")
    with pytest.raises(VoiceyError, match="VY-TST-001"):
        load_testing_config(tmp_path)


def test_reporting_preserves_stability_and_junit_failures(tmp_path: Path) -> None:
    failed = AttemptResult(False, ("wrong outcome",), 100, 2)
    passed = AttemptResult(
        True,
        (),
        90,
        2,
        evidence={"provider_call_id": "CA-safe-evidence"},
    )
    case = CaseResult("booking[alex]", "livekit", "text", (failed, passed, passed, passed))
    result = SuiteResult("livekit", "text", (case,))
    payload = json.loads(result_json(result, next_step="voicey dev"))
    assert payload["passed"] is False
    assert payload["cases"][0]["stability"] == 75
    assert payload["next_step"] == "voicey dev"
    junit = write_junit(result, tmp_path / "results.xml")
    tree = ElementTree.parse(junit)
    assert tree.getroot().attrib["failures"] == "1"
    assert tree.find(".//failure") is not None
    evidence = tree.find(".//property[@name='evidence.provider_call_id']")
    assert evidence is not None
    assert evidence.attrib["value"] == "CA-safe-evidence"


class _FakeExecutor(CaseExecutor):
    def __init__(self, results: list[bool]) -> None:
        self.results = iter(results)
        self.attempts: list[int] = []

    async def execute(
        self,
        case_name: str,
        definition: ScenarioDefinition,
        turns: tuple[ScenarioTurn, ...],
        *,
        attempt: int,
    ) -> AttemptResult:
        del case_name, definition
        self.attempts.append(attempt)
        passed = next(self.results)
        return AttemptResult(
            passed=passed,
            failures=() if passed else ("failed",),
            duration_ms=10,
            turn_count=len(turns),
        )


class _ClosableFakeExecutor(_FakeExecutor):
    def __init__(self, results: list[bool]) -> None:
        super().__init__(results)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _write_project(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "tests" / "scenarios.py").write_text(
        "\n".join(
            (
                "from voicey.testing import scenario, ScenarioTurn",
                "@scenario",
                "def hello():",
                "    return dict(caller='Caller', goals=['be helped'],",
                "      turns=(ScenarioTurn(user='Hello'),",
                "        ScenarioTurn(user='Pipecat only', runtimes=frozenset({'pipecat'}))))",
            )
        ),
        encoding="utf-8",
    )
    (root / "voicey.jsonc").write_text(
        """
        {
          schema_version: 1,
          project_name: "test",
          runtime: "livekit",
          recipe: {name: "scratch", version: "0.1.0"},
          channels: ["web"],
          models: {stt: "deepgram/nova-3", llm: "anthropic/claude-sonnet-5",
                   tts: "cartesia/sonic-3.5"},
        }
        """,
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_runner_retries_initial_failure_three_times_and_never_hides_it(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    executor = _FakeExecutor([False, True, True, True])
    result = await run_project_tests(tmp_path, executor=executor)
    assert executor.attempts == [1, 2, 3, 4]
    assert result.passed is False
    assert result.cases[0].stability == 75


@pytest.mark.asyncio
async def test_runner_passes_once_filters_and_keeps_live_distinct(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    executor = _FakeExecutor([True])
    result = await run_project_tests(tmp_path, filter_text="hell", executor=executor)
    assert result.passed is True
    assert result.cases[0].attempts[0].turn_count == 1
    assert executor.attempts == [1]
    live_executor = _FakeExecutor([True])
    live_result = await run_project_tests(tmp_path, live=True, executor=live_executor)
    assert live_result.tier == "live"
    assert live_result.cases[0].tier == "live"
    with pytest.raises(VoiceyError, match="distinct paid and local tiers"):
        await run_project_tests(tmp_path, live=True, audio=True, executor=live_executor)


@pytest.mark.asyncio
async def test_runner_builds_and_closes_default_paid_live_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    executor = _ClosableFakeExecutor([True])
    captured: dict[str, object] = {}

    def fake_builder(
        root: Path,
        *,
        runtime: str,
        config: object,
        judge: object,
        environment: Mapping[str, str],
        case_count: int,
    ) -> _ClosableFakeExecutor:
        del config, judge
        captured.update(
            root=root,
            runtime=runtime,
            environment=environment,
            case_count=case_count,
        )
        return executor

    monkeypatch.setattr("voicey.testing.live.build_live_executor", fake_builder)
    result = await run_project_tests(
        tmp_path,
        live=True,
        environment={"VOICEY_TEST_SENTINEL": "present"},
    )
    assert result.tier == "live"
    assert captured == {
        "root": tmp_path,
        "runtime": "livekit",
        "environment": {"VOICEY_TEST_SENTINEL": "present"},
        "case_count": 1,
    }
    assert executor.closed


@pytest.mark.asyncio
async def test_runner_validates_paid_acknowledgement_before_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)

    async def unexpected_plan(*_args: object, **_kwargs: object) -> tuple[ScenarioTurn, ...]:
        raise AssertionError("the planner must not run before paid-call preflight")

    monkeypatch.setattr(SimCaller, "plan", unexpected_plan)
    with pytest.raises(VoiceyError, match="no paid PSTN call was placed"):
        await run_project_tests(tmp_path, live=True, environment={})


@pytest.mark.asyncio
async def test_livekit_pcm_bridge_preserves_input_and_output_frames() -> None:
    microphone = QueueAudioInput()
    speak = asyncio.create_task(
        microphone.speak(b"\x01\x02" * 4, sample_rate=50, trailing_silence_s=0)
    )
    frame = await microphone.__anext__()
    await speak
    assert bytes(frame.data).startswith(b"\x01\x02")

    speaker = CapturingAudioOutput()
    await speaker.capture_frame(
        rtc.AudioFrame(
            data=b"\x03\x04" * 10,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=10,
        )
    )
    speaker.flush()
    pcm, rate = await speaker.next_segment(0.1)
    assert pcm == b"\x03\x04" * 10
    assert rate == 16000
    speaker.flush()

    await speaker.capture_frame(
        rtc.AudioFrame(
            data=b"\x05\x06" * 2,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=2,
        )
    )
    speaker.clear_buffer()
    speaker.clear_buffer()

    closed_microphone = QueueAudioInput()
    await closed_microphone.aclose()
    with pytest.raises(StopAsyncIteration):
        await closed_microphone.__anext__()


class _FakeMessageAssert:
    delay_s = 0.0

    def __init__(self, text: str) -> None:
        self._text = text
        self.criteria: list[str] = []

    def event(self) -> Any:
        return SimpleNamespace(item=SimpleNamespace(text_content=self._text))

    async def judge(self, _judge: object, *, intent: str) -> _FakeMessageAssert:
        await asyncio.sleep(self.delay_s)
        self.criteria.append(intent)
        return self


class _FakeNativeExpect:
    def __init__(self, text: str) -> None:
        self.tools: list[tuple[str, dict[str, Any]]] = []
        self.handoffs = 0
        self.message = _FakeMessageAssert(text)
        self.reverse_requested = False

    def __getitem__(self, key: slice) -> _FakeNativeExpect:
        assert key == slice(None, None, -1)
        self.reverse_requested = True
        return self

    def contains_function_call(self, *, name: str, **kwargs: Any) -> object:
        self.tools.append((name, cast(dict[str, Any], kwargs.get("arguments", {}))))
        return object()

    def contains_agent_handoff(self) -> object:
        self.handoffs += 1
        return object()

    def contains_message(self, *, role: str) -> _FakeMessageAssert:
        assert role == "assistant"
        return self.message


@pytest.mark.asyncio
async def test_livekit_native_assertions_cover_tools_handoff_text_and_judge() -> None:
    expectation = TurnExpectation(
        tools=(
            ToolExpectation(name="shared", arguments={"value": 1}),
            ToolExpectation(name="pipecat_only", runtimes=frozenset({"pipecat"})),
        ),
        text_contains="confirmed",
        judge=("is concise",),
        handoff="BookingAgent",
    )
    turn = compile_livekit(
        (
            ScenarioDefinition(
                name="native",
                caller="caller",
                goals=("book",),
                turns=(ScenarioTurn(user="hello", expect=expectation),),
            ),
        )
    )[0].turns[0]
    native_expect = _FakeNativeExpect("The booking is confirmed.")
    result = SimpleNamespace(expect=native_expect)

    await assert_native_turn(result, turn, judge_llm=object())

    assert native_expect.tools == [("shared", {"value": 1})]
    assert native_expect.handoffs == 1
    assert native_expect.reverse_requested is True
    assert native_expect.message.criteria == ["is concise"]

    _FakeMessageAssert.delay_s = 0.01
    with pytest.raises(AssertionError, match=r"native judge exceeded 0\.001s timeout"):
        await assert_native_turn(
            result,
            turn,
            judge_llm=object(),
            judge_timeout_s=0.001,
        )
    _FakeMessageAssert.delay_s = 0

    failing = _FakeNativeExpect("No matching phrase.")
    with pytest.raises(AssertionError, match="does not contain"):
        await assert_native_turn(
            SimpleNamespace(expect=failing),
            turn,
            judge_llm=object(),
        )


class _FakeTranscriptJudge:
    next_decision = JudgeDecision(True, "supported by line 1", (1,))
    fail = False

    def __init__(self, _client: object) -> None:
        return

    async def evaluate(
        self,
        criteria: tuple[str, ...],
        transcript: tuple[str, ...],
        *,
        seed: int,
    ) -> JudgeDecision:
        del criteria, transcript, seed
        if self.fail:
            raise VoiceyError("VY-TST-003", detail="judge unavailable")
        return self.next_decision


@pytest.mark.asyncio
async def test_pipecat_executor_combines_native_durable_latency_and_cited_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeManifest:
        @classmethod
        def load(cls, _path: Path) -> object:
            return object()

    native_result = SimpleNamespace(
        failures=[],
        events_seen=[
            {"type": "user_transcription", "transcript": "book Tuesday"},
            {"type": "llm_response", "text": "confirmed Tuesday"},
        ],
        debug_log=[],
        duration_ms=20,
    )

    class FakeSuite:
        def __init__(self, _manifest: object) -> None:
            self.runs = [
                SimpleNamespace(
                    error=None,
                    result=native_result,
                    duration_ms=25,
                )
            ]

        async def run(self, _logs: Path) -> None:
            return

    def fake_compile(*_args: object, **kwargs: object) -> Any:
        output = cast(Path, kwargs["output_dir"])
        output.mkdir(parents=True)
        manifest = output / "manifest.yaml"
        manifest.write_text("suite: []\n", encoding="utf-8")
        return SimpleNamespace(manifest=manifest)

    async def fake_snapshot(
        _path: Path,
        _call_id: str,
        **_kwargs: object,
    ) -> dict[str, Any]:
        return {
            "outcome": "appointment_booked",
            "data": {"appointment": {"status": "booked"}},
        }

    monkeypatch.setattr("pipecat.evals.suite.EvalManifest", FakeManifest)
    monkeypatch.setattr("pipecat.evals.suite.EvalSuite", FakeSuite)
    monkeypatch.setattr(testing_runner, "compile_pipecat", fake_compile)
    monkeypatch.setattr(testing_runner, "result_snapshot", fake_snapshot)
    monkeypatch.setattr(testing_runner, "TranscriptJudge", _FakeTranscriptJudge)
    executor = PipecatExecutor(
        tmp_path,
        audio=False,
        judge=JudgeConfig(),
        environment={},
    )

    result = await executor.execute(
        "booking[alex]",
        _definition(),
        _definition().turns,
        attempt=1,
    )

    assert result.passed is True
    assert result.transcript == (
        "caller: book Tuesday",
        "agent: confirmed Tuesday",
    )

    native_result.failures = ["native failure"]
    native_result.events_seen = []
    native_result.skipped = True
    _FakeTranscriptJudge.next_decision = JudgeDecision(False, "no support", ())
    too_slow = _definition().model_copy(update={"max_duration_ms": 1})
    failed = await executor.execute(
        "booking[alex]",
        too_slow,
        too_slow.turns,
        attempt=2,
    )
    assert failed.passed is False
    assert "native failure" in failed.failures
    assert "Pipecat Evals skipped the native scenario" in failed.failures
    assert any("exceeds" in failure for failure in failed.failures)
    assert "judge: no support" in failed.failures
    _FakeTranscriptJudge.next_decision = JudgeDecision(True, "supported", (1,))


class _FakeLiveKitLLM:
    constructors: ClassVar[list[str]] = []
    options: ClassVar[list[dict[str, object]]] = []
    closed = 0

    def __init__(self, **kwargs: object) -> None:
        self.constructors.append("ollama" if kwargs.get("api_key") == "ollama" else "cloud")
        self.options.append(kwargs)

    @classmethod
    def with_ollama(cls, **_kwargs: object) -> _FakeLiveKitLLM:
        cls.constructors.append("ollama")
        return cls.__new__(cls)

    async def aclose(self) -> None:
        type(self).closed += 1


class _FakeRunResult:
    def __init__(self, text: str = "confirmed") -> None:
        item = SimpleNamespace(role="assistant", text_content=text)
        self.events = [SimpleNamespace(type="message", item=item)]
        self.expect = _FakeNativeExpect(text)


class _FakeAgentSession:
    delay_s = 0.0
    instances: ClassVar[list[_FakeAgentSession]] = []

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False
        self.interrupts = 0
        self.callbacks: dict[str, list[Any]] = {}
        self.instances.append(self)

    def on(self, event: str, callback: Any) -> None:
        self.callbacks.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Any) -> None:
        self.callbacks[event].remove(callback)

    async def start(self, _native: object, **_kwargs: object) -> _FakeRunResult:
        return _FakeRunResult("opening")

    def run(self, *, user_input: str) -> Any:
        del user_input
        for callback in self.callbacks.get("agent_state_changed", []):
            callback(SimpleNamespace(new_state="thinking"))

        async def complete() -> _FakeRunResult:
            await asyncio.sleep(self.delay_s)
            return _FakeRunResult()

        return complete()

    async def interrupt(self, *, force: bool = False) -> None:
        assert force is True
        self.interrupts += 1

    async def aclose(self) -> None:
        self.closed = True


def _null_project_modules(*_args: object) -> Any:
    return nullcontext()


def _empty_native_tools(*_args: object, **_kwargs: object) -> list[Any]:
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["ollama", "openai", "anthropic"])
async def test_livekit_executor_uses_native_runs_and_enforces_latency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    import livekit.agents
    import livekit.plugins.openai

    import voicey.runtimes.livekit.flow
    import voicey.runtimes.livekit.providers
    import voicey.runtimes.livekit.tools

    agent = SimpleNamespace(tools="tools", flow="flow:entrypoint")
    monkeypatch.setattr(testing_runner, "project_modules", _null_project_modules)
    monkeypatch.setattr(testing_runner, "load_project_agent", lambda: agent)
    monkeypatch.setattr(livekit.agents, "AgentSession", _FakeAgentSession)
    monkeypatch.setattr(livekit.plugins.openai, "LLM", _FakeLiveKitLLM)
    monkeypatch.setattr(
        voicey.runtimes.livekit.providers,
        "Sonnet5AnthropicLLM",
        _FakeLiveKitLLM,
    )

    def fake_factory(_environment: object) -> object:
        return object()

    def fake_services(*_args: object, **_kwargs: object) -> Any:
        return SimpleNamespace(llm=object())

    monkeypatch.setattr(
        voicey.runtimes.livekit.providers,
        "DefaultLiveKitProviderFactory",
        fake_factory,
    )
    monkeypatch.setattr(
        voicey.runtimes.livekit.providers,
        "build_livekit_services",
        fake_services,
    )
    monkeypatch.setattr(
        voicey.runtimes.livekit.tools,
        "shared_livekit_tools",
        _empty_native_tools,
    )

    native_tools: list[Any] = []

    async def native_agent(*_args: object, **kwargs: object) -> object:
        native_tools.extend(cast(list[Any], kwargs["shared_tools"]))
        return object()

    def native_events(*_args: object, **_kwargs: object) -> None:
        return

    async def native_judge(*_args: object, **_kwargs: object) -> None:
        assert _FakeAgentSession.instances[-1].closed is True
        return

    monkeypatch.setattr(
        voicey.runtimes.livekit.flow,
        "load_native_agent",
        native_agent,
    )
    monkeypatch.setattr(testing_runner, "assert_native_turn_events", native_events)
    monkeypatch.setattr(testing_runner, "judge_native_turn", native_judge)
    monkeypatch.setattr(testing_runner, "TranscriptJudge", _FakeTranscriptJudge)
    config = JudgeConfig()
    if service != "ollama":
        config = JudgeConfig(
            service=cast(Any, service),
            model="claude-sonnet-5" if service == "anthropic" else "cloud",
            base_url=(
                "https://api.anthropic.com" if service == "anthropic" else "https://example.test/v1"
            ),
            api_key_env="TEST_KEY",  # pragma: allowlist secret
        )
    definition = ScenarioDefinition(
        name="native",
        caller="caller",
        goals=("get help",),
        judge=("is helpful",),
        turns=(
            ScenarioTurn(
                user="hello",
                expect=TurnExpectation(judge=("responds",), within_ms=1),
            ),
            ScenarioTurn(
                user="stop",
                send_after=SendAfter(event="llm_started", delay_ms=0),
            ),
        ),
    )
    _FakeAgentSession.delay_s = 0.003
    _FakeAgentSession.instances.clear()
    _FakeLiveKitLLM.constructors.clear()
    _FakeLiveKitLLM.options.clear()
    _FakeLiveKitLLM.closed = 0
    executor = LiveKitExecutor(
        tmp_path,
        audio=False,
        judge=config,
        environment={"TEST_KEY": "test"},
    )

    result = await executor.execute(
        "native[default]",
        definition,
        definition.turns,
        attempt=1,
    )

    assert result.passed is False
    assert any("turn duration" in failure for failure in result.failures)
    assert _FakeAgentSession.instances[-1].interrupts == 1
    assert ("ollama" if service == "ollama" else "cloud") in _FakeLiveKitLLM.constructors
    assert any(tool.info.name == "transfer_to_human" for tool in native_tools)
    assert _FakeLiveKitLLM.closed == 1
    if service == "ollama":
        assert _FakeLiveKitLLM.options[0]["extra_body"] == {"think": False}

    native_tools.clear()
    warm_definition = definition.model_copy(
        update={
            "turns": (
                ScenarioTurn(
                    user="I consent to a warm transfer.",
                    expect=TurnExpectation(tools=(ToolExpectation(name="warm_transfer_to_human"),)),
                ),
            )
        }
    )
    await executor.execute(
        "native-warm-transfer[default]",
        warm_definition,
        warm_definition.turns,
        attempt=1,
    )
    warm_transfer = next(
        tool for tool in native_tools if tool.info.name == "warm_transfer_to_human"
    )
    assert await warm_transfer() == {"ok": True, "status": "transferred"}
    assert all(tool.info.name != "transfer_to_human" for tool in native_tools)

    _FakeAgentSession.delay_s = 0.01
    timed = definition.model_copy(update={"max_duration_ms": 1})
    timed_out = await executor.execute(
        "native-timeout[default]",
        timed,
        timed.turns,
        attempt=1,
    )
    assert timed_out.passed is False
    assert "scenario exceeded 1ms hard timeout" in timed_out.failures

    _FakeAgentSession.delay_s = 0
    _FakeTranscriptJudge.fail = True
    judge_failed = await executor.execute(
        "native-judge-failure[default]",
        definition,
        definition.turns,
        attempt=1,
    )
    assert judge_failed.passed is False
    assert "judge failed with VY-TST-003" in judge_failed.failures
    _FakeTranscriptJudge.fail = False
    _FakeAgentSession.delay_s = 0


class _FakeAudioInput:
    async def speak(self, *_args: object, **_kwargs: object) -> None:
        return

    async def aclose(self) -> None:
        return


class _FakeAudioOutput:
    fail_after_opening = False

    def __init__(self) -> None:
        self.calls = 0

    async def next_segment(self, _timeout: float) -> tuple[bytes, int]:
        self.calls += 1
        if self.fail_after_opening and self.calls > 1:
            raise TimeoutError
        return b"\x01\x02" * 10, 16000


class _FakeAudioSession:
    def __init__(self, **_kwargs: object) -> None:
        self.input = SimpleNamespace(audio=None)
        self.output = SimpleNamespace(audio=None)

    def on(self, _event: str, _callback: object) -> None:
        return

    async def start(self, _native: object, **_kwargs: object) -> None:
        return

    async def aclose(self) -> None:
        return


class _FakeSpeech:
    @classmethod
    def from_config(cls, _config: object) -> _FakeSpeech:
        return cls()

    async def start(self) -> None:
        return

    async def generate(self, _text: str) -> tuple[bytes, int]:
        return b"\x01\x02", 16000

    async def aclose(self) -> None:
        return


class _FakeTranscriber:
    @classmethod
    def from_config(cls, _config: object) -> _FakeTranscriber:
        return cls()

    async def start(self) -> None:
        return

    async def transcribe(self, _pcm: bytes, _rate: int) -> str:
        return "agent confirmed the request"

    async def aclose(self) -> None:
        return


class _FakePolicy:
    def turn_handling(self, _mode: object) -> object:
        return object()


@pytest.mark.asyncio
async def test_livekit_audio_executor_drives_pcm_tools_judge_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SimpleNamespace(tools="tools", flow="flow:entrypoint")
    policy = _FakePolicy()
    services = SimpleNamespace(
        stt=object(),
        vad=object(),
        llm=object(),
        tts=object(),
        turn_detection=object(),
    )
    monkeypatch.setattr(testing_runner, "project_modules", _null_project_modules)
    monkeypatch.setattr(testing_runner, "load_project_agent", lambda: agent)

    def fake_audio_factory(_environment: object) -> object:
        return object()

    def fake_audio_services(*_args: object, **_kwargs: object) -> Any:
        return services

    def fake_policy(_agent: object) -> _FakePolicy:
        return policy

    def fake_detector(_detector: object) -> str:
        return "vad"

    monkeypatch.setattr(
        audio_testing,
        "DefaultLiveKitProviderFactory",
        fake_audio_factory,
    )
    monkeypatch.setattr(audio_testing, "build_livekit_services", fake_audio_services)
    monkeypatch.setattr(
        audio_testing.LiveKitPolicy,
        "from_agent",
        fake_policy,
    )
    monkeypatch.setattr(audio_testing, "detector_mode", fake_detector)
    monkeypatch.setattr(audio_testing, "shared_livekit_tools", _empty_native_tools)

    async def native_agent(*_args: object, **_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(audio_testing, "load_native_agent", native_agent)
    monkeypatch.setattr(audio_testing, "AgentSession", _FakeAudioSession)
    monkeypatch.setattr(audio_testing, "QueueAudioInput", _FakeAudioInput)
    monkeypatch.setattr(audio_testing, "CapturingAudioOutput", _FakeAudioOutput)
    monkeypatch.setattr(audio_testing, "EvalSpeech", _FakeSpeech)
    monkeypatch.setattr(audio_testing, "EvalTranscriber", _FakeTranscriber)
    monkeypatch.setattr(audio_testing, "TranscriptJudge", _FakeTranscriptJudge)
    definition = ScenarioDefinition(
        name="audio",
        caller="caller",
        goals=("get help",),
        turns=(
            ScenarioTurn(
                user="hello",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="missing_tool"),),
                    text_contains="confirmed",
                    judge=("is helpful",),
                    within_ms=100,
                ),
            ),
        ),
    )
    _FakeAudioOutput.fail_after_opening = False
    result = await audio_testing.execute_audio_case(
        tmp_path,
        definition,
        definition.turns,
        judge=JudgeConfig(),
        environment={},
    )
    assert result.passed is False
    assert "expected function call 'missing_tool'" in result.failures
    assert result.transcript[-1] == "agent: agent confirmed the request"

    _FakeAudioOutput.fail_after_opening = True
    timed_out = await audio_testing.execute_audio_case(
        tmp_path,
        definition.model_copy(update={"turns": (ScenarioTurn(user="hello"),)}),
        (ScenarioTurn(user="hello"),),
        judge=JudgeConfig(),
        environment={},
    )
    assert any("no agent audio" in failure for failure in timed_out.failures)
    _FakeAudioOutput.fail_after_opening = False


@pytest.mark.asyncio
async def test_runner_helpers_cover_snapshot_result_and_project_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await testing_runner.result_snapshot(tmp_path / "missing.sqlite3", "call") == {
        "outcome": None,
        "data": {},
    }
    failures = testing_runner.hard_result_failures(
        _definition(),
        {"outcome": "wrong", "data": "wrong"},
    )
    assert any("outcome expected" in failure for failure in failures)
    assert "result data is not an object" in failures
    assert testing_runner.pipecat_transcript(
        [
            {"type": "ignored"},
            {"type": "tts_response", "text": "hello"},
        ]
    ) == ["agent: hello"]
    assert testing_runner.pipecat_transcript(
        [
            {"type": "llm_response", "text": "opening"},
            {"type": "llm_response", "text": "switched"},
        ],
        debug_log=[
            " 0.1 [ --] event: llm_response 'opening'",
            " 0.2 [ t0] send: 'Book' (text)",
            " 0.3 [ t0] event: llm_response 'switched'",
        ],
        turns=(ScenarioTurn(user="Book for {email}."),),
        profile=ScenarioProfile(name="alex", identity={"email": "alex@example.com"}),
    ) == [
        "agent: opening",
        "caller: Book for alex@example.com.",
        "agent: switched",
    ]
    assert (
        testing_runner.livekit_transcript(
            [
                SimpleNamespace(type="tool"),
                SimpleNamespace(
                    type="message",
                    item=SimpleNamespace(role="assistant", text_content=None),
                ),
            ]
        )
        == []
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "agent.py").write_text("agent = object()\n", encoding="utf-8")
    with (
        testing_runner.project_modules(tmp_path, {"VOICEY_TEST_TEMP": "yes"}),
        pytest.raises(VoiceyError, match="must export"),
    ):
        testing_runner.load_project_agent()


def test_voicey_test_cli_terminal_and_junit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def fake_run(
        root: Path,
        *,
        filter_text: str | None = None,
        audio: bool = False,
        live: bool = False,
        executor: CaseExecutor | None = None,
        environment: dict[str, str] | None = None,
    ) -> SuiteResult:
        del filter_text, live, executor, environment
        assert root == tmp_path
        case = CaseResult(
            "hello[default]",
            "livekit",
            "audio" if audio else "text",
            (AttemptResult(True, (), 12, 1),),
        )
        return SuiteResult("livekit", case.tier, (case,))

    monkeypatch.setattr("voicey.cli.app.run_project_tests", fake_run)
    result = cli_runner.invoke(app, ["test", "--audio", "--report", "junit"])
    assert result.exit_code == 0
    assert "PASS" in result.stdout
    assert "Next: voicey dev" in result.stdout
    assert (tmp_path / ".voicey" / "test-results.xml").is_file()


def test_voicey_test_cli_json_failure_has_retry_next_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def fake_run(
        root: Path,
        *,
        filter_text: str | None = None,
        audio: bool = False,
        live: bool = False,
        executor: CaseExecutor | None = None,
        environment: dict[str, str] | None = None,
    ) -> SuiteResult:
        del root, audio, executor, environment
        assert filter_text == "hello"
        assert live
        case = CaseResult(
            "hello[default]",
            "livekit",
            "live",
            (AttemptResult(False, ("wrong outcome",), 12, 1),),
        )
        return SuiteResult("livekit", "live", (case,))

    monkeypatch.setattr("voicey.cli.app.run_project_tests", fake_run)
    result = cli_runner.invoke(
        app,
        ["test", "--filter", "hello", "--live", "--report", "json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["passed"] is False
    assert payload["next_step"] == "voicey test --filter hello --live"
