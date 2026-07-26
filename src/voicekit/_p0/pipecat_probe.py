"""Pipecat 1.6 walking skeleton against installed current APIs."""

from __future__ import annotations

import asyncio

from aiortc import RTCPeerConnection, RTCSessionDescription
from pipecat.flows import FlowManager, NodeConfig
from pipecat.frames.frames import EndFrame, Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.workers.runner import WorkerRunner

from voicekit import results, tool
from voicekit._p0.common import (
    BrowserEvidence,
    MockPhoneProvider,
    RuntimeProbe,
    finalize_probe,
)
from voicekit.tools import get_tool_metadata


class _ProbeLLM(LLMService):
    """Provider-free LLM processor that preserves flow-control frames."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


async def run_pipecat_probe() -> RuntimeProbe:
    """Run native FlowManager, PipelineWorker, SmallWebRTC, and result paths."""
    call_id = "call_p0_pipecat"
    buffer = results.CallResultBuffer(call_id=call_id)
    phone = MockPhoneProvider()
    tool_result = ""

    @tool(say_while_running="I am checking the P0 slot.")
    async def record_slot(slot: str) -> str:
        """Record the selected P0 appointment slot."""
        results.set("slot", slot)
        return f"recorded:{slot}"

    async def flow_tool(
        flow_manager: FlowManager,
        slot: str,
    ) -> tuple[str, NodeConfig | None]:
        del flow_manager
        return await record_slot(slot), None

    context = LLMContext(messages=[])
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_idle_timeout=0,
            vad_analyzer=None,
        ),
    )
    llm = _ProbeLLM()
    pipeline = Pipeline([aggregators.user(), llm, aggregators.assistant()])
    worker = PipelineWorker(
        pipeline,
        enable_rtvi=True,
        enable_turn_tracking=True,
        idle_timeout_secs=None,
    )
    flow_manager = FlowManager(
        llm=llm,
        context_aggregator=aggregators,
        worker=worker,
    )

    @worker.event_handler("on_pipeline_started")
    async def on_pipeline_started(  # pyright: ignore[reportUnusedFunction]
        active_worker: PipelineWorker,
        _frame: Frame,
    ) -> None:
        nonlocal tool_result
        with results.result_context(buffer):
            await flow_manager.initialize(
                NodeConfig(
                    name="p0-entry",
                    task_messages=[
                        {
                            "role": "developer",
                            "content": "Exercise the provider-free P0 tool path.",
                        }
                    ],
                    functions=[flow_tool],
                    respond_immediately=False,
                )
            )
            tool_result, _ = await flow_tool(flow_manager, "2030-01-02T10:00:00Z")
            results.set_outcome("p0_proven")
            await phone.terminate(call_id, "provider_mock_completed")
        await active_worker.queue_frame(EndFrame())

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    await asyncio.wait_for(runner.run(), timeout=10)
    browser = await _exercise_small_webrtc()

    return finalize_probe(
        runtime="pipecat",
        native_bootstrap=f"{type(worker).__name__}+{type(flow_manager).__name__}",
        native_tool_name=get_tool_metadata(record_slot).name,
        tool_result=tool_result,
        buffer=buffer,
        browser=browser,
        phone=phone,
    )


async def _exercise_small_webrtc() -> BrowserEvidence:
    browser = RTCPeerConnection()
    server = SmallWebRTCConnection(connection_timeout_secs=5)
    try:
        browser.createDataChannel("rtvi")
        browser.addTransceiver("audio", direction="sendrecv")
        offer = await browser.createOffer()
        await browser.setLocalDescription(offer)
        local = browser.localDescription

        await server.initialize(local.sdp, local.type)
        answer = server.get_answer()
        if answer is None:
            msg = "SmallWebRTC did not produce an answer"
            raise AssertionError(msg)
        await browser.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        for _ in range(100):
            if browser.connectionState in {"connected", "failed"}:
                break
            await asyncio.sleep(0.05)
        return BrowserEvidence(
            session_id=str(answer["pc_id"]),
            connected=browser.connectionState == "connected",
        )
    finally:
        await server.disconnect()
        await browser.close()
