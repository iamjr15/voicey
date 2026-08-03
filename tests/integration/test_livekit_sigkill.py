from __future__ import annotations

import asyncio
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from voicey.obs import CallRecord
from voicey.results import ProviderReconciliation, RecoveryCoordinator
from voicey.storage import SQLiteRepository

pytestmark = [pytest.mark.integration]


class _LiveKitCrashedJobReconciler:
    async def reconcile(self, call: CallRecord) -> ProviderReconciliation:
        assert call.runtime == "livekit"
        assert call.provider_call_id == "sip_sigkill"
        return ProviderReconciliation(state="failed", ended_reason="worker_crash")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX-only")
@pytest.mark.asyncio
async def test_livekit_sigkill_recovers_incremental_native_event_once(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "calls.sqlite3"
    child = r"""
import asyncio
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from livekit.agents import ConversationItemAddedEvent
from livekit.agents.llm import ChatMessage

from voicey import Agent, Models, Results, Web
from voicey.runtimes.livekit import LiveKitCall, LiveKitLifecycleManager
from voicey.runtimes.livekit.observability import LiveKitObservationBridge
from voicey.runtimes.pipecat.admission import AdmissionController
from voicey.storage import SQLiteRepository

async def noop():
    return None

async def main():
    repository = await SQLiteRepository(Path(sys.argv[1])).open()
    agent = Agent(
        name="livekit-crash-test",
        runtime="livekit",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Persist before crashing.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEY_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )
    admission = AdmissionController(1)
    call = LiveKitCall(
        call_id="call_livekit_sigkill",
        channel="phone",
        direction="inbound",
        provider="twilio",
        provider_call_id="sip_sigkill",
    )
    lease = await admission.acquire(call.call_id)
    await LiveKitLifecycleManager(
        repository,
        admission,
        owner_id="doomed_livekit_job",
        lease_ttl=timedelta(milliseconds=1),
    ).begin(agent, call, lease)
    bridge = LiveKitObservationBridge(
        call_id=call.call_id,
        store=cast(Any, repository),
        end_call_phrases=(),
        on_user_idle=noop,
        on_end_phrase=noop,
    )
    await bridge.on_conversation_item(
        ConversationItemAddedEvent(
            item=ChatMessage(
                role="user",
                content=["native LiveKit event persisted before process death"],
                metrics={"transcription_delay": 0.123},
            )
        )
    )
    await bridge.drain()
    os.kill(os.getpid(), signal.SIGKILL)

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        str(database_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == -signal.SIGKILL, stderr.decode()

    sweep_time = datetime.now(UTC) + timedelta(seconds=1)
    async with SQLiteRepository(database_path) as repository:
        coordinator = RecoveryCoordinator(
            repository,
            _LiveKitCrashedJobReconciler(),
            owner_id="livekit_recovery_worker",
            clock=lambda: sweep_time,
        )
        first = await coordinator.run_once()
        second = await coordinator.run_once()
        event = await repository.get_terminal_event_for_call("call_livekit_sigkill")
        deliveries = await repository.list_deliveries()
        record = await repository.get_call("call_livekit_sigkill")

    assert first.terminalized == 1
    assert second.terminalized == 0
    assert len(deliveries) == 1
    assert record.runtime == "livekit"
    assert event.event_type == "call.failed"
    assert b"native LiveKit event persisted before process death" in event.body
