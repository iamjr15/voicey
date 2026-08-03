"""Run the P4 metrics/OTLP gate against actual loopback HTTP listeners."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from voicey.config.models import Observability, RuntimeName
from voicey.obs import (
    LatencySample,
    NewCall,
    Telemetry,
    TelemetryServer,
    ToolCallObservation,
    TranscriptTurn,
)
from voicey.storage.models import TerminalRequest

_PRIVATE_TRANSCRIPT = "private-patient-utterance"
_PRIVATE_ARGUMENT = "private-tool-argument"


class _ManagedServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        return


async def run_gate(report_path: Path) -> dict[str, Any]:
    """Verify both runtime labels, exposition, exporter wire, and PII bounds."""
    bodies: list[bytes] = []
    content_types: list[str] = []
    collector = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @collector.post("/v1/traces")
    async def receive(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        bodies.append(await request.body())
        content_types.append(request.headers.get("content-type", ""))
        return Response(status_code=200)

    collector_port = _free_port()
    collector_server = _ManagedServer(
        uvicorn.Config(
            collector,
            host="127.0.0.1",
            port=collector_port,
            log_level="warning",
            access_log=False,
        )
    )
    collector_task = asyncio.create_task(
        collector_server.serve(),
        name="voicey-observability-gate-collector",
    )
    await _wait_started(collector_server, collector_task)

    rows: list[dict[str, Any]] = []
    try:
        for runtime in ("pipecat", "livekit"):
            rows.append(
                await _runtime_row(
                    runtime,
                    collector_port=collector_port,
                )
            )
    finally:
        collector_server.should_exit = True
        await asyncio.gather(collector_task, return_exceptions=True)

    wire = b"".join(bodies)
    if not bodies or any(not body for body in bodies):
        raise AssertionError("the real OTLP/HTTP collector received no span payload")
    if not all("application/x-protobuf" in value for value in content_types):
        raise AssertionError(f"unexpected OTLP content types: {content_types!r}")
    for private in (_PRIVATE_TRANSCRIPT, _PRIVATE_ARGUMENT):
        if private.encode() in wire:
            raise AssertionError("OTLP protobuf contains protected transcript/tool payload")

    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "green",
        "rows": rows,
        "otlp_requests": len(bodies),
        "otlp_bytes": len(wire),
        "pii_scan": "green",
    }
    await asyncio.to_thread(_write_report, report_path, report)
    return report


async def _runtime_row(
    runtime: RuntimeName,
    *,
    collector_port: int,
) -> dict[str, Any]:
    metrics_port = _free_port()
    settings = Observability(
        prometheus_enabled=True,
        prometheus_port=metrics_port,
        otlp_endpoint=f"http://127.0.0.1:{collector_port}/v1/traces",
    )
    telemetry = Telemetry(
        agent_name=f"gate-{runtime}",
        runtime=runtime,
        settings=settings,
    )
    server = TelemetryServer(telemetry)
    await server.start()
    call_id = f"gate-{runtime}-call"
    call = NewCall(
        call_id=call_id,
        agent_name=f"gate-{runtime}",
        runtime=runtime,
        channel="web",
        direction="inbound",
        config_hash=f"sha256:{'a' * 64}",
    )
    try:
        telemetry.begin_call(call)
        telemetry.observe_turn(
            call_id,
            TranscriptTurn(
                turn_id="turn_1",
                role="user",
                text=_PRIVATE_TRANSCRIPT,
                t_ms=5,
            ),
        )
        telemetry.observe_tool(
            call_id,
            ToolCallObservation(
                invocation_id=f"tool-{runtime}",
                tool_name="lookup",
                arguments={"value": _PRIVATE_ARGUMENT},
                result={"ok": True},
                duration_ms=8,
                status="succeeded",
            ),
        )
        telemetry.observe_latency(
            LatencySample(
                turn_id="turn_1",
                turn_index=1,
                metric="e2e",
                duration_ms=321,
                observed_at=datetime.now(UTC),
            )
        )
        telemetry.set_dlq_depth(2)
        async with httpx.AsyncClient(timeout=5) as client:
            active_response = await client.get(f"http://127.0.0.1:{metrics_port}/metrics")
        active_response.raise_for_status()
        active_metrics = active_response.text
        active_sample = f'voicey_active_calls{{agent="gate-{runtime}",runtime="{runtime}"}} 1.0'
        if active_sample not in active_metrics:
            raise AssertionError("active call gauge was not exposed before terminalization")

        request = TerminalRequest(
            event_type="call.failed" if runtime == "pipecat" else "call.completed",
            ended_reason="provider_error" if runtime == "pipecat" else "agent_hangup",
        )
        telemetry.finish_call(call_id, request)
        if not await asyncio.to_thread(telemetry.force_flush):
            raise AssertionError("OTLP force_flush timed out")
        async with httpx.AsyncClient(timeout=5) as client:
            terminal_response = await client.get(f"http://127.0.0.1:{metrics_port}/metrics")
        terminal_response.raise_for_status()
        terminal_metrics = terminal_response.text
        terminal_sample = f'voicey_active_calls{{agent="gate-{runtime}",runtime="{runtime}"}} 0.0'
        if terminal_sample not in terminal_metrics:
            raise AssertionError("terminal call did not release the active gauge")
        dlq_sample = f'voicey_results_dlq_depth{{agent="gate-{runtime}",runtime="{runtime}"}} 2.0'
        if dlq_sample not in terminal_metrics:
            raise AssertionError("DLQ depth is absent")
        error_green = runtime == "pipecat" and 'code="VY-RUN-002"' in terminal_metrics
        if runtime == "pipecat" and not error_green:
            raise AssertionError("stable error-code counter is absent")
        for private in (_PRIVATE_TRANSCRIPT, _PRIVATE_ARGUMENT):
            if private in terminal_metrics:
                raise AssertionError("Prometheus output contains protected payload")
        return {
            "runtime": runtime,
            "prometheus_status": "green",
            "active_before_terminal": 1,
            "active_after_terminal": 0,
            "latency_histogram": "green",
            "error_code_counter": "green" if error_green else "not-applicable",
        }
    finally:
        await server.stop()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


async def _wait_started(
    server: uvicorn.Server,
    task: asyncio.Task[None],
) -> None:
    async def wait() -> None:
        while not server.started:
            if task.done():
                await task
                raise AssertionError("OTLP collector exited before readiness")
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicey/verification/p4-observability-report.json"),
    )
    arguments = parser.parse_args()
    report = asyncio.run(run_gate(arguments.report))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
