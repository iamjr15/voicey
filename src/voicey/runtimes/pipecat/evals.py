"""Pipecat's native eval transport wired through the production session builder."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, cast

from pipecat.evals.transport import EvalTransportParams
from pipecat.runner.types import EvalRunnerArguments
from pipecat.runner.utils import create_transport  # pyright: ignore[reportUnknownVariableType]
from pipecat.workers.runner import WorkerRunner

from voicey.config.models import Agent
from voicey.errors import VoiceyError
from voicey.obs.records import TimelineEvent
from voicey.runtimes.pipecat.admission import AdmissionController
from voicey.runtimes.pipecat.flows import TransferHandler, WarmTransferHandler
from voicey.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatCallLifecycle,
    PipecatLifecycleManager,
    PipecatRepository,
)
from voicey.runtimes.pipecat.session import PipecatSession, PipecatSessionBuilder
from voicey.storage.sqlite import SQLiteRepository

_EVAL_TRANSFER_NUMBER = "+15555550199"


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


class EvalWarmTransferHandler(WarmTransferHandler):
    """Exercise consent and private-briefing arguments without a carrier call."""

    def __init__(self, repository: PipecatRepository) -> None:
        self._repository = repository

    async def __call__(
        self,
        call_id: str,
        number: str,
        briefing: str,
        set_reason: Any,
    ) -> None:
        await self._repository.append_timeline(
            call_id,
            TimelineEvent(
                event_type="eval.warm_transfer_requested",
                details={
                    "destination_configured": bool(number),
                    "briefing_present": bool(briefing),
                },
            ),
        )
        set_reason("transferred")


async def run_eval_agent(
    agent: Agent,
    runner_args: EvalRunnerArguments,
    *,
    repository: PipecatRepository | None = None,
    repository_path: Path = Path(".voicey/evals.db"),
    call_id: str | None = None,
) -> None:
    """Run one agent through Pipecat's installed ``-t eval`` transport."""
    if agent.runtime != "pipecat":
        raise VoiceyError("VY-RUN-001", detail="Pipecat Evals require runtime='pipecat'.")
    if agent.behavior.transfer_number is None:
        agent = agent.model_copy(
            update={
                "behavior": agent.behavior.model_copy(
                    update={"transfer_number": _EVAL_TRANSFER_NUMBER}
                )
            }
        )
    body = cast(dict[str, Any], runner_args.body) if isinstance(runner_args.body, dict) else {}
    configured_repository = body.get("voicey_repository")
    if repository is None and isinstance(configured_repository, str):
        repository_path = Path(configured_repository)
    configured_call_id = body.get("voicey_call_id")
    if call_id is None and isinstance(configured_call_id, str):
        call_id = configured_call_id
    transfer_mode = body.get("voicey_transfer_mode")

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
            call_id=call_id or f"call_eval_{runner_args.session_id or uuid.uuid4().hex}",
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
            transfer_handler=(
                EvalTransferHandler(active_repository) if transfer_mode != "warm" else None
            ),
            warm_transfer_handler=(
                EvalWarmTransferHandler(active_repository) if transfer_mode == "warm" else None
            ),
        ).build(
            agent=agent,
            call=call,
            lifecycle=lifecycle,
            transport=transport,
            sample_rate=16000,
        )

        @transport.event_handler("on_client_disconnected")
        async def terminalize_eval_disconnect(  # pyright: ignore[reportUnusedFunction]
            _transport: object,
            _client: object,
        ) -> None:
            # EvalSuite may stop the bot process immediately after it records a
            # passing transcript. Persist the buffered business result first;
            # PipecatSession.wait() remains idempotent if the process stays up.
            session.set_reason("caller_hangup")
            await lifecycle.finish(
                "caller_hangup",
                interruptions=session.observations.interruptions,
                provider_state="completed",
            )

        # The native eval transport ends a successful scenario by cancelling
        # the worker after its WebSocket client disconnects. That cancellation
        # is the evaluator's normal caller hangup, not a production worker crash.
        session.unattributed_cancel_reason = "caller_hangup"
        runner = WorkerRunner(
            handle_sigint=runner_args.handle_sigint,
            handle_sigterm=runner_args.handle_sigterm,
        )
        await session.start(runner)
        wait_task = asyncio.create_task(session.wait(), name=f"voicey-eval-{call.call_id}")
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
