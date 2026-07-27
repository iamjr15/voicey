# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from voicekit import Agent, Models, Results, Web, tool
from voicekit.playground.reload import (
    ReloadController,
    _relevant,
    _requires_worker_restart,
)


@tool
def identify() -> str:
    """Return a stable identity."""
    return "reload-test"


def _agent() -> Agent:
    return Agent(
        name="reload-test",
        runtime="pipecat",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Reload safely.",
        flow="flow:entry",
        tools=[identify],
        web=Web(enabled=True, allowed_origins=["http://127.0.0.1:7861"]),
        results=Results(
            webhook="https://receiver.example/results",
            secret_env="RESULT_SECRET",  # pragma: allowlist secret
        ),
    )


class FakeRuntime:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.restarts: list[bool] = []

    async def reload_agent(self, agent: Agent, *, restart_runner: bool) -> bool:
        assert agent.name == "reload-test"
        self.restarts.append(restart_runner)
        return self.outcomes.pop(0)


@pytest.mark.asyncio
async def test_prompt_reload_is_in_process_and_visible(tmp_path: Path) -> None:
    runtime = FakeRuntime([True])
    loaded: list[str] = []
    controller = ReloadController(
        root=tmp_path,
        agent_module="agent",
        runtime=runtime,
        load_agent=_agent,
        on_loaded=lambda agent: loaded.append(agent.name),
        retry_s=0,
    )

    await controller.apply({tmp_path / "prompts" / "system.md"})

    assert runtime.restarts == [False]
    assert loaded == ["reload-test"]
    assert controller.snapshot() == {
        "revision": 1,
        "state": "ready",
        "message": "configuration loaded",
    }


@pytest.mark.asyncio
async def test_flow_reload_waits_for_call_boundary_then_restarts_worker(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime([False, True])
    controller = ReloadController(
        root=tmp_path,
        agent_module="agent",
        runtime=runtime,
        load_agent=_agent,
        retry_s=0,
    )

    await controller.apply({tmp_path / "flow.py"})

    assert runtime.restarts == [True, True]
    assert controller.snapshot()["revision"] == 1
    assert controller.snapshot()["message"] == "runtime worker restarted"


@pytest.mark.asyncio
async def test_reload_error_is_cataloged_and_later_revision_can_recover(
    tmp_path: Path,
) -> None:
    attempts = 0

    def load() -> Agent:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ImportError("syntax error")
        return _agent()

    controller = ReloadController(
        root=tmp_path,
        agent_module="agent",
        runtime=FakeRuntime([True]),
        load_agent=load,
    )
    await controller.apply({tmp_path / "agent.py"})
    assert controller.snapshot()["state"] == "error"
    assert "VK-WEB-005" in str(controller.snapshot()["message"])

    await controller.apply({tmp_path / "agent.py"})
    assert controller.snapshot()["state"] == "ready"
    assert controller.snapshot()["revision"] == 1


@pytest.mark.asyncio
async def test_reload_stops_waiting_when_supervisor_stops(tmp_path: Path) -> None:
    stop = asyncio.Event()
    stop.set()
    controller = ReloadController(
        root=tmp_path,
        agent_module="agent",
        runtime=FakeRuntime([False]),
        load_agent=_agent,
        retry_s=0,
    )

    await controller.apply({tmp_path / "flow.py"}, stop_event=stop)

    assert controller.snapshot()["state"] == "restart_pending"
    assert controller.snapshot()["revision"] == 0


def test_reload_classifies_only_runtime_relevant_files(tmp_path: Path) -> None:
    assert _relevant({tmp_path / "prompts" / "system.md"}, tmp_path)
    assert _relevant({tmp_path / "voicekit.jsonc"}, tmp_path)
    assert not _relevant({tmp_path / "README.md"}, tmp_path)
    assert not _requires_worker_restart({tmp_path / "agent.py"}, tmp_path)
    assert not _requires_worker_restart({tmp_path / "prompts" / "system.md"}, tmp_path)
    assert _requires_worker_restart({tmp_path / "tools.py"}, tmp_path)
