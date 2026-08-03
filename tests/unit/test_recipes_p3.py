from __future__ import annotations

import importlib.util
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from livekit.agents import Agent as NativeLiveKitAgent
from livekit.agents import function_tool, llm
from pipecat.evals.scenario import EvalScenario
from pipecat.evals.suite import EvalManifest

from voicey import results
from voicey.recipes.registry import DEFAULT_RECIPE_REGISTRY
from voicey.recipes.source import RECIPE_LOCK_NAME, install_recipe, recipe_files
from voicey.testing import JudgeConfig, discover_scenarios
from voicey.testing.livekit import compile_livekit
from voicey.testing.pipecat import compile_pipecat

ROOT = Path(__file__).parents[2]
RECIPES = {
    "restaurant-reservations": {
        "scenarios": 5,
        "node": "restaurant-reservation-coordinator",
        "handoff": "discuss_waitlist",
    },
    "front-desk": {
        "scenarios": 6,
        "node": "front-desk-receptionist",
        "handoff": "start_message",
    },
    "lead-intake": {
        "scenarios": 6,
        "node": "lead-intake-coordinator",
        "handoff": "start_contact_capture",
    },
}


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _native_function(
    agent: NativeLiveKitAgent,
    name: str,
) -> Callable[[], Awaitable[NativeLiveKitAgent]]:
    for native_tool in agent.tools:
        if isinstance(native_tool, llm.FunctionTool) and native_tool.info.name == name:
            return cast("Callable[[], Awaitable[NativeLiveKitAgent]]", native_tool)
    raise AssertionError(f"missing native handoff {name}")


def test_p3_recipes_are_offline_dual_runtime_registry_entries() -> None:
    expected = {"appointment-booking", *RECIPES}
    available = DEFAULT_RECIPE_REGISTRY.list(include_unavailable=False)

    assert {recipe.name for recipe in available} == expected
    for name in RECIPES:
        definition = DEFAULT_RECIPE_REGISTRY.require(name, "pipecat")
        assert definition.version == "1.0.0"
        assert definition.runtimes == {"pipecat", "livekit"}
        assert definition.source_available is True


@pytest.mark.parametrize("name", RECIPES)
def test_p3_voicemail_prompts_switch_from_answering_to_leaving(name: str) -> None:
    prompt = (ROOT / "recipes" / name / "prompts" / "voicemail.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "you are leaving a message" in normalized
    assert "do not ask the recipient to leave a message" in normalized
    assert "End immediately" in normalized


def test_front_desk_prompt_makes_successful_transfer_terminal() -> None:
    prompt = (ROOT / "recipes/front-desk/prompts/system.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "state that the handoff is complete and end the response" in normalized
    assert "do not ask the caller to wait" in normalized


def test_lead_prompt_qualifies_complete_facts_without_extra_confirmation() -> None:
    prompt = (ROOT / "recipes/lead-intake/prompts/system.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "call `qualify_inquiry` immediately" in normalized
    assert "do not ask the caller to reconfirm" in normalized


@pytest.mark.parametrize("name", RECIPES)
def test_p3_recipe_source_selects_only_the_native_runtime(name: str, tmp_path: Path) -> None:
    pipecat_files = recipe_files(name, "pipecat")
    livekit_files = recipe_files(name, "livekit")

    assert "from pipecat.flows import FlowManager, NodeConfig" in pipecat_files["flow.py"]
    assert "from livekit.agents import Agent" in livekit_files["flow.py"]
    assert "eval_bot.py" in pipecat_files
    assert "eval_bot.py" not in livekit_files
    assert "tools.py" in pipecat_files
    assert "tests/scenarios.py" in livekit_files
    assert all(not path.startswith(("pipecat/", "livekit/")) for path in pipecat_files)
    assert all(not path.startswith(("pipecat/", "livekit/")) for path in livekit_files)

    written = install_recipe(
        tmp_path,
        name=name,
        version="1.0.0",
        runtime="livekit",
    )
    assert written
    assert tmp_path / RECIPE_LOCK_NAME in written
    assert (
        install_recipe(
            tmp_path,
            name=name,
            version="1.0.0",
            runtime="livekit",
        )
        == ()
    )


@pytest.mark.parametrize(("name", "facts"), RECIPES.items())
def test_p3_recipe_scenarios_compile_to_both_native_runtimes(
    name: str,
    facts: dict[str, Any],
    tmp_path: Path,
) -> None:
    root = ROOT / "recipes" / name
    scenarios = discover_scenarios(root)
    pipecat = compile_pipecat(
        scenarios,
        output_dir=tmp_path / name,
        bot=root / "pipecat" / "eval_bot.py",
        audio=False,
        judge=JudgeConfig(),
    )
    manifest = EvalManifest.load(pipecat.manifest)
    livekit = compile_livekit(scenarios)

    assert len(scenarios) == facts["scenarios"]
    assert len(manifest.runs) == facts["scenarios"]
    assert len(livekit) == facts["scenarios"]
    assert all(EvalScenario.load(path).turns for path in pipecat.scenarios)
    assert all(case.turns for case in livekit)


@pytest.mark.parametrize(("name", "facts"), RECIPES.items())
def test_p3_pipecat_entrypoints_return_native_nodes(
    name: str,
    facts: dict[str, Any],
) -> None:
    flow = _load(
        ROOT / "recipes" / name / "pipecat" / "flow.py",
        f"voicey_test_{name.replace('-', '_')}_pipecat",
    )
    node = flow.entry(None)

    assert node["name"] == facts["node"]
    assert node["respond_immediately"] is True
    assert node["role_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "facts"), RECIPES.items())
async def test_p3_livekit_entrypoints_use_native_handoffs_and_preserve_tools(
    name: str,
    facts: dict[str, Any],
) -> None:
    flow = _load(
        ROOT / "recipes" / name / "livekit" / "flow.py",
        f"voicey_test_{name.replace('-', '_')}_livekit",
    )

    async def shared_lookup() -> str:
        """Return deterministic evidence for tool preservation."""
        return "ok"

    shared = function_tool(shared_lookup)
    intake = cast(NativeLiveKitAgent, flow.entrypoint([shared]))
    specialist = await _native_function(intake, facts["handoff"])()

    assert isinstance(intake, NativeLiveKitAgent)
    assert isinstance(specialist, NativeLiveKitAgent)
    assert shared in intake.tools
    assert shared in specialist.tools
    assert specialist.chat_ctx.items == intake.chat_ctx.items


@pytest.mark.asyncio
async def test_restaurant_livekit_waitlist_mutation_is_specialist_owned() -> None:
    flow = _load(
        ROOT / "recipes/restaurant-reservations/livekit/flow.py",
        "voicey_test_restaurant_waitlist_tool_owner",
    )

    async def join_waitlist() -> dict[str, str]:
        """Return a deterministic waitlist result."""
        return {"status": "waitlisted"}

    waitlist_tool = function_tool(join_waitlist)
    intake = cast(NativeLiveKitAgent, flow.entrypoint([waitlist_tool]))
    specialist = await _native_function(intake, "discuss_waitlist")()

    assert waitlist_tool not in intake.tools
    assert waitlist_tool in specialist.tools


def test_restaurant_stub_records_reservation_and_waitlist() -> None:
    tools = _load(
        ROOT / "recipes/restaurant-reservations/tools.py",
        "voicey_test_restaurant_tools",
    )
    buffer = results.CallResultBuffer(call_id="call_restaurant")
    assert tools.search_tables("2026-08-08", "19:00:00", "America/New_York", 10) == {
        "status": "unavailable",
        "times": [],
    }

    with results.result_context(buffer):
        reserved = tools.create_reservation(
            "2026-08-08T19:30:00",
            "America/New_York",
            4,
            "Casey Morgan",
            "+14155550142",
            "window if available",
        )
        waitlisted = tools.join_waitlist(
            "2026-08-08",
            "19:00:00",
            "America/New_York",
            10,
            "Casey Morgan",
            "+14155550142",
        )

    assert str(reserved["reference"]).startswith("RES-")
    assert str(waitlisted["reference"]).startswith("WAIT-")
    assert buffer.outcome == "restaurant_waitlisted"


def test_front_desk_stub_is_fail_closed_and_records_messages() -> None:
    tools = _load(ROOT / "recipes/front-desk/tools.py", "voicey_test_front_desk_tools")
    buffer = results.CallResultBuffer(call_id="call_front_desk")

    assert tools.lookup_answer("office hours")["status"] == "found"
    assert tools.lookup_answer("invoice 42") == {"status": "not_found", "answer": None}
    with results.result_context(buffer):
        message = tools.take_message(
            "Jordan Lee",
            "+14155550143",
            " Billing ",
            "Please call about invoice 42.",
        )

    assert str(message["reference"]).startswith("MSG-")
    assert message["department"] == "billing"
    assert buffer.outcome == "front_desk_message_taken"


def test_lead_stub_requires_consent_and_records_followup() -> None:
    tools = _load(ROOT / "recipes/lead-intake/tools.py", "voicey_test_lead_tools")
    buffer = results.CallResultBuffer(call_id="call_lead")
    qualification = tools.qualify_inquiry("call automation", "this week", "20k", 50)

    with pytest.raises(ValueError, match="consent"):
        tools.capture_lead(
            "Riley Chen",
            "riley@example.com",
            "Acme",
            "call automation",
            "this week",
            "20k",
            50,
            qualification["fit"],
            False,
        )
    with results.result_context(buffer):
        lead = tools.capture_lead(
            "Riley Chen",
            "riley@example.com",
            "Acme",
            "call automation",
            "this week",
            "20k",
            50,
            qualification["fit"],
            True,
        )
        followup = tools.schedule_lead_followup(
            lead["reference"],
            "2026-08-10T14:00:00",
            "America/Los_Angeles",
        )

    assert str(lead["reference"]).startswith("LEAD-")
    assert str(followup["reference"]).startswith("FOLLOW-")
    assert buffer.outcome == "lead_followup_scheduled"
