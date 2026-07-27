"""Pipecat's native eval transport wired through the production session builder."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from pipecat.evals.transport import EvalTransportParams
from pipecat.runner.types import EvalRunnerArguments
from pipecat.runner.utils import create_transport  # pyright: ignore[reportUnknownVariableType]
from pipecat.workers.runner import WorkerRunner

from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.obs.records import TimelineEvent
from voicekit.runtimes.pipecat.admission import AdmissionController
from voicekit.runtimes.pipecat.flows import TransferHandler
from voicekit.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatCallLifecycle,
    PipecatLifecycleManager,
    PipecatRepository,
)
from voicekit.runtimes.pipecat.session import PipecatSession, PipecatSessionBuilder
from voicekit.storage.sqlite import SQLiteRepository


class EvalTransferHandler(TransferHandler):
    """Exercise the production transfer tool without contacting a carrier."""

    def __init__(self, repository: PipecatRepository) -> None:
        self._repository = repository

    async def __call__(self, call_id: str, number: str) -> None:
        await self._repository.append_timeline(
            call_id,
            TimelineEvent(
                event_type="eval.transfer_requested",
                details={"destination_configured": bool(number)},
            ),
        )


async def run_eval_agent(
    agent: Agent,
    runner_args: EvalRunnerArguments,
    *,
    repository: PipecatRepository | None = None,
    repository_path: Path = Path(".voicekit/evals.db"),
) -> None:
    """Run one agent through Pipecat's installed ``-t eval`` transport."""
    if agent.runtime != "pipecat":
        raise VoicekitError("VK-RUN-001", detail="Pipecat Evals require runtime='pipecat'.")
    owned_repository: SQLiteRepository | None = None
    active_repository: PipecatRepository
    if repository is None:
        repository_path.parent.mkdir(parents=True, exist_ok=True)
        owned_repository = await SQLiteRepository(repository_path).open()
        active_repository = owned_repository
    else:
        active_repository = repository

    lifecycle: PipecatCallLifecycle | None = None
    session: PipecatSession | None = None
    wait_task: asyncio.Task[object] | None = None
    try:
        call = PipecatCall(
            call_id=f"call_eval_{runner_args.session_id or uuid.uuid4().hex}",
            channel="web",
            direction="inbound",
            provider="eval",
        )
        admission = AdmissionController(1)
        admission_lease = await admission.acquire(call.call_id)
        lifecycle = await PipecatLifecycleManager(
            active_repository,
            admission,
            owner_id=f"pipecat_eval_{uuid.uuid4().hex}",
        ).begin(agent, call, admission_lease)
        transport = await create_transport(
            runner_args,
            {
                "eval": lambda: EvalTransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                )
            },
        )
        session = PipecatSessionBuilder(
            active_repository,
            transfer_handler=EvalTransferHandler(active_repository),
        ).build(
            agent=agent,
            call=call,
            lifecycle=lifecycle,
            transport=transport,
            sample_rate=16000,
        )
        runner = WorkerRunner(
            handle_sigint=runner_args.handle_sigint,
            handle_sigterm=runner_args.handle_sigterm,
        )
        await session.start(runner)
        wait_task = asyncio.create_task(session.wait(), name=f"voicekit-eval-{call.call_id}")
        await runner.run()
        await wait_task
    finally:
        try:
            if wait_task is not None and not wait_task.done() and session is not None:
                await session.end("caller_hangup")
                await wait_task
            elif lifecycle is not None and lifecycle.terminal_event is None:
                await lifecycle.finish("setup_error", provider_state="failed")
        finally:
            if owned_repository is not None:
                await owned_repository.close()
