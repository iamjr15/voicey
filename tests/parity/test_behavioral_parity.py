from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from livekit.agents import RunContext

from voicekit import Agent, Models, Phone, Results, Web, results, tool
from voicekit.config.models import RuntimeName
from voicekit.obs.records import ToolCallObservation, TranscriptTurn
from voicekit.runtimes.livekit.lifecycle import LiveKitLifecycleManager
from voicekit.runtimes.livekit.tools import shared_livekit_tools
from voicekit.runtimes.pipecat.admission import AdmissionController
from voicekit.runtimes.pipecat.flows import shared_flow_tools
from voicekit.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatLifecycleManager,
)
from voicekit.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).parents[2]
RECIPE = ROOT / "recipes" / "appointment-booking"
RUNTIMES: tuple[RuntimeName, ...] = ("pipecat", "livekit")


class MemoryToolSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, ToolCallObservation]] = []

    async def record(self, call_id: str, observation: ToolCallObservation) -> None:
        self.items.append((call_id, observation))


class FakeRunContext:
    def __init__(self) -> None:
        self.interruptions_disabled = False
        self.fillers: list[str] = []

    def disallow_interruptions(self) -> None:
        self.interruptions_disabled = True

    @asynccontextmanager
    async def with_filler(self, text: str) -> AsyncGenerator[None]:
        self.fillers.append(text)
        yield


@tool
def lookup_customer(name: str) -> dict[str, str]:
    """Look up a customer before making a reservation."""
    return {"name": name, "status": "found"}


@tool(say_while_running="I am reserving that now.", mutating=True)
def reserve_slot(slot: str) -> dict[str, str]:
    """Reserve the caller-confirmed slot."""
    return {"slot": slot, "status": "reserved"}


@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_recipe_greeting_is_identical(
    runtime: RuntimeName,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (RECIPE / "prompts" / "greeting.md").read_text(encoding="utf-8").strip()

    if runtime == "pipecat":
        module = _load_module(
            RECIPE / "pipecat" / "flow.py",
            "voicekit_parity_pipecat_recipe",
        )
        node = cast("dict[str, Any]", module.entry(cast(Any, None)))
        observed = str(node["task_messages"][0]["content"])
    else:
        module = _load_module(
            RECIPE / "livekit" / "flow.py",
            "voicekit_parity_livekit_recipe",
        )
        replies: list[str] = []

        def generate_reply(*, instructions: str) -> None:
            replies.append(instructions)

        fake_session = SimpleNamespace(generate_reply=generate_reply)
        monkeypatch.setattr(
            module.AppointmentIntakeAgent,
            "session",
            property(lambda _agent: fake_session),
        )
        native = module.entrypoint([])
        await native.on_enter()
        observed = replies[0]

    assert observed == expected


@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_shared_tool_sequence_is_identical(runtime: RuntimeName) -> None:
    sink = MemoryToolSink()
    call_id = f"call-tools-{runtime}"
    arguments = ({"name": "Ada"}, {"slot": "10:00"})
    values: list[object] = []

    if runtime == "pipecat":
        worker = SimpleNamespace(queue_frame=_no_op)
        manager = SimpleNamespace(worker=worker)
        native = shared_flow_tools(
            [lookup_customer, reserve_slot],
            call_id=call_id,
            sink=sink,
        )
        names = [item.name for item in native]
        for item, item_arguments in zip(native, arguments, strict=True):
            value, next_node = await cast(Any, item.handler)(item_arguments, manager)
            assert next_node is None
            values.append(value["value"])
    else:
        buffer = results.CallResultBuffer(call_id=call_id)
        context = FakeRunContext()
        native = shared_livekit_tools(
            [lookup_customer, reserve_slot],
            call_id=call_id,
            buffer=buffer,
            sink=sink,
        )
        names = [item.info.name for item in native]
        for item, item_arguments in zip(native, arguments, strict=True):
            values.append(
                await item(
                    cast("RunContext[Any]", context),
                    item_arguments,
                )
            )
        assert context.interruptions_disabled
        assert context.fillers == ["I am reserving that now."]

    assert names == ["lookup_customer", "reserve_slot"]
    assert [item[1].tool_name for item in sink.items] == names
    assert values == [
        {"name": "Ada", "status": "found"},
        {"slot": "10:00", "status": "reserved"},
    ]


@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_terminal_webhook_payload_is_identical(
    runtime: RuntimeName,
    tmp_path: Path,
) -> None:
    agent = _agent(runtime)
    repository = await SQLiteRepository(tmp_path / f"{runtime}.sqlite3").open()
    admission = AdmissionController(1)
    admission_lease = await admission.acquire(f"call-{runtime}")
    call = PipecatCall(
        call_id=f"call-{runtime}",
        channel="phone",
        direction="inbound",
        provider="twilio",
        provider_call_id="CA" + ("7" * 32),
        from_number="+14155550100",
        to_number="+14155550123",
    )
    manager = (
        PipecatLifecycleManager(repository, admission)
        if runtime == "pipecat"
        else LiveKitLifecycleManager(repository, admission)
    )
    lifecycle = await manager.begin(agent, call, admission_lease)
    await repository.append_transcript(
        call.call_id,
        TranscriptTurn(
            turn_id="turn_0001",
            role="user",
            text="Book ten o'clock.",
            t_ms=10,
        ),
    )
    with results.result_context(lifecycle.buffer):
        results.set("slot", "10:00")
        results.set_outcome("booked")
    event = await lifecycle.finish(
        "agent_hangup",
        interruptions=1,
        provider_state="completed",
    )
    payload = cast("dict[str, Any]", json.loads(event.body))
    await repository.close()

    payload.pop("id")
    call_payload = cast("dict[str, Any]", payload["call"])
    for field in ("id", "started_at", "ended_at", "duration_s"):
        call_payload.pop(field)
    agent_payload = cast("dict[str, Any]", payload["agent"])
    agent_payload.pop("runtime")
    agent_payload.pop("config_hash")

    assert payload == {
        "event": "call.completed",
        "call": {
            "direction": "inbound",
            "from": "+14155550100",
            "to": "+14155550123",
            "ended_reason": "agent_hangup",
        },
        "agent": {"name": "parity-agent"},
        "outcome": "booked",
        "data": {"slot": "10:00"},
        "transcript": [
            {
                "role": "user",
                "text": "Book ten o'clock.",
                "t_ms": 10,
            }
        ],
        "recording": None,
        "metrics": {
            "turns": 1,
            "interruptions": 1,
            "latency_ms": None,
        },
    }


def _agent(runtime: RuntimeName) -> Agent:
    return Agent(
        name="parity-agent",
        runtime=runtime,
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Help callers book appointments.",
        flow="flow:entry",
        tools=[lookup_customer, reserve_slot],
        phone=Phone(provider="twilio", number="+14155550123"),
        web=Web(enabled=True, allowed_origins=["https://app.example.test"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def _no_op(_frame: object) -> None:
    return None
