from __future__ import annotations

import asyncio
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from voicey.obs import CallRecord
from voicey.results import (
    ProviderReconciliation,
    RecoveryCoordinator,
)
from voicey.storage import SQLiteRepository

pytestmark = [pytest.mark.integration]


class _CrashedProviderReconciler:
    async def reconcile(self, call: CallRecord) -> ProviderReconciliation:
        assert call.provider_call_id == "CA_sigkill"
        return ProviderReconciliation(
            state="failed",
            ended_reason="worker_crash",
        )


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX-only")
@pytest.mark.asyncio
async def test_sigkill_recovery_persists_partial_transcript_and_one_terminal(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "calls.sqlite3"
    child = r"""
import asyncio
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from voicey.obs import NewCall, TranscriptTurn
from voicey.storage import ResultDeliveryConfig, SQLiteRepository

async def main():
    repository = SQLiteRepository(Path(sys.argv[1]))
    await repository.open()
    now = datetime.now(UTC)
    await repository.begin_call(
        NewCall(
            call_id="call_sigkill",
            agent_name="crash-test",
            runtime="pipecat",
            channel="phone",
            direction="inbound",
            provider="twilio",
            provider_call_id="CA_sigkill",
            from_number="+14155550123",
            to_number="+14155550124",
            config_hash="sha256:" + "c" * 64,
            started_at=now,
        ),
        owner_id="doomed_worker",
        delivery=ResultDeliveryConfig(
            endpoint="https://receiver.example.test/results",
        ),
        lease_ttl=timedelta(milliseconds=1),
        now=now,
    )
    await repository.append_transcript(
        "call_sigkill",
        TranscriptTurn(
            turn_id="partial",
            role="user",
            text="persisted before process death",
            t_ms=10,
        ),
    )
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
            _CrashedProviderReconciler(),
            owner_id="recovery_worker",
            clock=lambda: sweep_time,
        )

        run = await coordinator.run_once()
        event = await repository.get_terminal_event_for_call("call_sigkill")
        deliveries = await repository.list_deliveries()

    assert run.terminalized == 1
    assert len(deliveries) == 1
    assert event.event_type == "call.failed"
    assert b"persisted before process death" in event.body
