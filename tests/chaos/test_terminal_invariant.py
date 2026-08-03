from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import pytest

from voicey import Agent, Models, Results, Web, results, tool
from voicey.obs import TranscriptTurn
from voicey.results import DeliveryWorker
from voicey.results.signing import encode_secret
from voicey.runtimes.pipecat.admission import AdmissionController
from voicey.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatLifecycleManager,
)
from voicey.storage import SQLiteRepository
from voicey.storage.models import EndedReason
from voicey.tools import RepositoryToolObservationSink, ToolExecutor
from voicey.tools.execution import tool_execution_context

Fault = Literal[
    "provider_connection_killed",
    "carrier_websocket_dropped",
    "tool_timed_out",
]


def _agent(runtime: Literal["pipecat", "livekit"]) -> Agent:
    return Agent(
        name=f"chaos-{runtime}",
        runtime=runtime,
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Persist every adversarial result.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEY_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


@pytest.mark.parametrize("runtime", ["pipecat", "livekit"])
@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("provider_connection_killed", "provider_error"),
        ("carrier_websocket_dropped", "provider_hangup"),
        ("tool_timed_out", "provider_error"),
    ],
)
@pytest.mark.asyncio
async def test_every_injected_fault_has_one_terminal_and_attempted_delivery(
    tmp_path: Path,
    runtime: Literal["pipecat", "livekit"],
    fault: Fault,
    reason: EndedReason,
) -> None:
    call_id = f"call_chaos_{runtime}_{fault}"
    requests: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    async with SQLiteRepository(tmp_path / f"{runtime}-{fault}.sqlite3") as repository:
        admission = AdmissionController(1)
        manager = PipecatLifecycleManager(
            repository,
            admission,
            runtime=runtime,
            owner_id=f"chaos-{runtime}",
        )
        reservation = await admission.acquire(call_id)
        lifecycle = await manager.begin(
            _agent(runtime),
            PipecatCall(
                call_id=call_id,
                channel="phone",
                direction="inbound",
                provider="twilio",
                provider_call_id=f"provider-{call_id}",
            ),
            reservation,
        )
        await repository.append_transcript(
            call_id,
            TranscriptTurn(
                turn_id="partial-user",
                role="user",
                text="This partial turn must survive the injected fault.",
                t_ms=12,
            ),
        )

        if fault == "tool_timed_out":

            @tool
            async def slow_mutation() -> dict[str, bool]:
                """Exercise the bounded mutating-tool timeout path."""
                await asyncio.sleep(0.05)
                return {"ok": True}

            with (
                results.result_context(lifecycle.buffer),
                tool_execution_context(
                    call_id,
                    RepositoryToolObservationSink(repository),
                ),
            ):
                execution = await ToolExecutor(timeout_s=0.001).execute(
                    slow_mutation,
                    {},
                )
            assert not execution.ok
            assert execution.error is not None
            assert execution.error.code == "tool_timeout"

        first, duplicate = await asyncio.gather(
            lifecycle.finish(reason, provider_state="failed"),
            lifecycle.finish(reason, provider_state="failed"),
        )
        pulled = await repository.get_terminal_event_for_call(call_id)
        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
            worker = DeliveryWorker(
                repository,
                owner_id=f"delivery-{runtime}",
                current_secret=encode_secret(b"chaos-delivery-secret"),
                client=client,
                clock=lambda: datetime.now(UTC),
                jitter=lambda delay: delay,
            )
            delivery_run = await worker.run_once()
        deliveries = [
            item for item in await repository.list_deliveries() if item.call_id == call_id
        ]

    assert first == duplicate == pulled
    assert admission.active_count == 0
    assert len(deliveries) == 1
    assert deliveries[0].attempt_count == 1
    assert deliveries[0].status == "pending"
    assert delivery_run.claimed == delivery_run.failed == 1
    assert len(requests) == 1
    assert b"This partial turn must survive" in pulled.body
