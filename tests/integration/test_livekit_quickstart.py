from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from livekit.agents import Agent as NativeLiveKitAgent

from voicekit.cli.scaffold import ScaffoldWriter, ScratchScaffold
from voicekit.config.manifest import ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.runtimes.livekit.flow import load_native_agent


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
