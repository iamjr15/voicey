from __future__ import annotations

import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from livekit.agents import Agent as NativeLiveKitAgent
from livekit.agents import llm

from voicey.cli.scaffold import ScaffoldWriter, ScratchScaffold
from voicey.config.manifest import ProjectManifest, RecipeSelection
from voicey.config.models import ModelAxis
from voicey.runtimes.livekit.flow import load_native_agent


def _agent_handoff(
    agent: NativeLiveKitAgent,
    name: str,
) -> Callable[[], Awaitable[NativeLiveKitAgent]]:
    for native_tool in agent.tools:
        if isinstance(native_tool, llm.FunctionTool) and native_tool.info.name == name:
            return cast("Callable[[], Awaitable[NativeLiveKitAgent]]", native_tool)
    raise AssertionError(f"missing native handoff {name}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_livekit_scratch_project_reaches_native_agent_in_under_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="livekit-quickstart",
        runtime="livekit",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    scaffold = ScratchScaffold(
        project_name=manifest.project_name,
        description="Answer concise product questions.",
        stt=models["stt"],
        llm=models["llm"],
        tts=models["tts"],
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
        runtime="livekit",
    )

    ScaffoldWriter().write(tmp_path, scaffold, manifest)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent", None)
    sys.modules.pop("flow", None)

    try:
        configured = importlib.import_module("agent").agent
        native = await load_native_agent(configured.flow, shared_tools=[])

        assert configured.runtime == "livekit"
        assert isinstance(native, NativeLiveKitAgent)
        assert time.monotonic() - started < 300
    finally:
        sys.modules.pop("agent", None)
        sys.modules.pop("flow", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_livekit_appointment_recipe_loads_native_handoffs_from_scaffold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="livekit-appointments",
        runtime="livekit",
        recipe=RecipeSelection(name="appointment-booking", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    scaffold = ScratchScaffold(
        project_name=manifest.project_name,
        description="Book, reschedule, and cancel appointments.",
        stt=models["stt"],
        llm=models["llm"],
        tts=models["tts"],
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
        runtime="livekit",
        recipe_name="appointment-booking",
    )

    ScaffoldWriter().write(tmp_path, scaffold, manifest)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent", None)
    sys.modules.pop("flow", None)

    try:
        configured = importlib.import_module("agent").agent
        native = await load_native_agent(configured.flow, shared_tools=[])
        booking_handoff = _agent_handoff(native, "start_booking")
        booking = await booking_handoff()

        assert configured.runtime == "livekit"
        assert configured.flow == "flow:entrypoint"
        assert native.id == "appointment_intake_agent"
        assert isinstance(booking, NativeLiveKitAgent)
        assert booking.id == "booking_agent"
        assert any(
            isinstance(tool, llm.FunctionTool) and tool.info.name == "return_to_intake"
            for tool in booking.tools
        )
    finally:
        sys.modules.pop("agent", None)
        sys.modules.pop("flow", None)
