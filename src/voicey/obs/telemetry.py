"""Bounded Prometheus metrics and optional PII-safe OTLP call tracing."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import timedelta
from threading import Lock
from typing import Any

import uvicorn
from fastapi import FastAPI, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest

from voicey.config.models import Agent, Observability
from voicey.errors import ERROR_CATALOG, VoiceyError
from voicey.obs.latency import LatencySample
from voicey.obs.records import NewCall, ToolCallObservation, TranscriptTurn
from voicey.storage.models import (
    CallLease,
    DeliveryClaim,
    DeliveryRecord,
    EndedReason,
    PersistedEvent,
    ResultDeliveryConfig,
    TerminalRequest,
)

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FAILURE_CODE: dict[EndedReason, str] = {
    "stt_unavailable": "VY-RUN-002",
    "llm_unavailable": "VY-RUN-002",
    "tts_unavailable": "VY-RUN-002",
    "carrier_error": "VY-TEL-011",
    "provider_error": "VY-RUN-002",
    "worker_crash": "VY-RUN-006",
    "setup_error": "VY-RUN-003",
    "recovery_unknown": "VY-RUN-006",
    "unknown": "VY-RUN-006",
}
_LATENCY_BUCKETS_MS = (
    25,
    50,
    100,
    200,
    300,
    500,
    750,
    1000,
    1500,
    2500,
    5000,
    10000,
)


class Telemetry:
    """One process-local metric registry and lazily fork-safe OTLP provider."""

    def __init__(
        self,
        *,
        agent_name: str,
        runtime: str,
        settings: Observability,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.runtime = runtime
        self.settings = settings
        self._environment = os.environ if environment is None else environment
        self.registry = CollectorRegistry(auto_describe=True)
        labels = ("runtime", "agent")
        self._calls = Counter(
            "voicey_calls",
            "Durably admitted calls; use rate(voicey_calls_total[5m]) for call rate.",
            labels,
            registry=self.registry,
        )
        self._active = Gauge(
            "voicey_active_calls",
            "Calls currently owned by this voicey process.",
            labels,
            registry=self.registry,
        )
        self._errors = Counter(
            "voicey_errors",
            "Errors grouped by stable voicey catalog code.",
            (*labels, "code"),
            registry=self.registry,
        )
        self._dlq = Gauge(
            "voicey_results_dlq_depth",
            "Current durable result-delivery dead-letter depth.",
            labels,
            registry=self.registry,
        )
        self._latency = Histogram(
            "voicey_turn_latency_ms",
            "Per-turn subsystem latency in milliseconds.",
            (*labels, "metric"),
            buckets=_LATENCY_BUCKETS_MS,
            registry=self.registry,
        )
        self._label_values = (runtime, agent_name)
        self._calls.labels(*self._label_values)
        self._active.labels(*self._label_values).set(0)
        self._dlq.labels(*self._label_values).set(0)
        for metric in ("stt_partial", "stt_final", "llm_ttft", "tts_ttfb", "e2e"):
            self._latency.labels(*self._label_values, metric)
        self._active_ids: set[str] = set()
        self._call_spans: dict[str, trace.Span] = {}
        self._lock = Lock()
        self._provider: TracerProvider | None = None
        self._tracer: trace.Tracer | None = None
        self._provider_pid: int | None = None
        self._state_pid = os.getpid()

    @classmethod
    def from_agent(
        cls,
        agent: Agent,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> Telemetry:
        return cls(
            agent_name=agent.name,
            runtime=agent.runtime,
            settings=agent.observability,
            environment=environment,
        )

    @property
    def enabled(self) -> bool:
        return self.settings.prometheus_enabled or self.settings.otlp_endpoint is not None

    def start(self) -> None:
        """Validate and initialize configured exporters before admission."""
        self._ensure_tracer()

    def begin_call(self, call: NewCall, *, count: bool = True) -> None:
        """Idempotently open the active-call metric and its root span."""
        self._reset_after_fork()
        self.admit_call(call.call_id, count=count)
        with self._lock:
            if call.call_id in self._call_spans:
                return
            tracer = self._ensure_tracer()
            if tracer is None:
                return
            self._call_spans[call.call_id] = tracer.start_span(
                "voicey.call",
                kind=trace.SpanKind.SERVER,
                attributes={
                    "voicey.call.id": call.call_id,
                    "voicey.agent.name": call.agent_name,
                    "voicey.runtime": call.runtime,
                    "voicey.channel": call.channel,
                    "voicey.direction": call.direction,
                    **(
                        {}
                        if call.provider is None
                        else {"voicey.telephony.provider": call.provider}
                    ),
                },
            )

    def admit_call(self, call_id: str, *, count: bool = True) -> None:
        """Track parent-process admission without creating an OTLP span."""
        self._reset_after_fork()
        with self._lock:
            if call_id in self._active_ids:
                return
            self._active_ids.add(call_id)
            if count:
                self._calls.labels(*self._label_values).inc()
            self._active.labels(*self._label_values).set(len(self._active_ids))

    def release_call(self, call_id: str) -> bool:
        """Release one parent-process admission and return whether it existed."""
        self._reset_after_fork()
        with self._lock:
            if call_id not in self._active_ids:
                return False
            self._active_ids.remove(call_id)
            self._active.labels(*self._label_values).set(len(self._active_ids))
            return True

    def finish_call(self, call_id: str, request: TerminalRequest) -> None:
        """Close metrics and trace exactly once after durable terminalization."""
        self._reset_after_fork()
        with self._lock:
            span = self._call_spans.pop(call_id, None)
        was_active = self.release_call(call_id)
        code = _FAILURE_CODE.get(request.ended_reason)
        if code is not None and was_active:
            self.record_error(code)
        if span is not None:
            span.set_attribute("voicey.call.event_type", request.event_type)
            span.set_attribute("voicey.call.ended_reason", request.ended_reason)
            if code is not None:
                span.set_attribute("error.type", code)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
            else:
                span.set_status(trace.Status(trace.StatusCode.OK))
            span.end()

    def record_error(self, code: str) -> None:
        """Count only bounded stable catalog codes."""
        if code not in ERROR_CATALOG:
            code = "VY-CLI-009"
        self._errors.labels(*self._label_values, code).inc()

    def observe_latency(self, sample: LatencySample) -> None:
        self._latency.labels(*self._label_values, sample.metric).observe(sample.duration_ms)

    def observe_turn(self, call_id: str, turn: TranscriptTurn) -> None:
        self._child_span(
            call_id,
            "voicey.turn",
            {
                "voicey.turn.id": turn.turn_id,
                "voicey.turn.role": turn.role,
                "voicey.turn.offset_ms": turn.t_ms,
            },
        )

    def observe_tool(self, call_id: str, observation: ToolCallObservation) -> None:
        self._child_span(
            call_id,
            "voicey.tool",
            {
                "voicey.tool.name": observation.tool_name,
                "voicey.tool.status": observation.status,
                "voicey.tool.duration_ms": observation.duration_ms,
            },
            failed=observation.status != "succeeded",
        )

    def set_dlq_depth(self, depth: int) -> None:
        if depth < 0:
            raise VoiceyError("VY-OBS-006", detail="DLQ depth cannot be negative.")
        self._dlq.labels(*self._label_values).set(depth)

    def render_prometheus(self) -> bytes:
        return generate_latest(self.registry)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        provider = self._provider
        return True if provider is None else provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        provider = self._provider
        self._provider = None
        self._tracer = None
        self._provider_pid = None
        if provider is not None:
            provider.shutdown()

    def _child_span(
        self,
        call_id: str,
        name: str,
        attributes: dict[str, str | int | float],
        *,
        failed: bool = False,
    ) -> None:
        tracer = self._ensure_tracer()
        if tracer is None:
            return
        with self._lock:
            parent = self._call_spans.get(call_id)
        context = None if parent is None else trace.set_span_in_context(parent)
        span = tracer.start_span(name, context=context, attributes=attributes)
        if failed:
            span.set_status(trace.Status(trace.StatusCode.ERROR))
        span.end()

    def _ensure_tracer(self) -> trace.Tracer | None:
        endpoint = self.settings.otlp_endpoint
        if endpoint is None:
            return None
        pid = os.getpid()
        if self._tracer is not None and self._provider_pid == pid:
            return self._tracer
        try:
            headers = _otlp_headers(self.settings, self._environment)
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": "voicey",
                        "service.instance.id": f"{self.agent_name}-{pid}",
                        "voicey.agent.name": self.agent_name,
                        "voicey.runtime": self.runtime,
                    }
                ),
                shutdown_on_exit=False,
            )
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=endpoint,
                        headers=headers,
                    )
                )
            )
        except Exception as exc:
            raise VoiceyError(
                "VY-OBS-006",
                detail="the OTLP/HTTP trace exporter could not be configured.",
            ) from exc
        self._provider = provider
        self._provider_pid = pid
        self._tracer = provider.get_tracer("voicey", "1")
        return self._tracer

    def _reset_after_fork(self) -> None:
        pid = os.getpid()
        if self._state_pid == pid:
            return
        self._lock = Lock()
        self._active_ids = set()
        self._call_spans = {}
        self._provider = None
        self._tracer = None
        self._provider_pid = None
        self._state_pid = pid


class InstrumentedRepository:
    """Repository decorator that observes only successful durable mutations."""

    def __init__(
        self,
        repository: Any,
        telemetry: Telemetry,
        *,
        shutdown_telemetry: bool = False,
    ) -> None:
        self.repository = repository
        self.telemetry = telemetry
        self._shutdown_telemetry = shutdown_telemetry

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
        now: Any = None,
    ) -> CallLease:
        lease = await self.repository.begin_call(
            call,
            owner_id=owner_id,
            delivery=delivery,
            lease_ttl=lease_ttl,
            now=now,
        )
        self.telemetry.begin_call(call)
        return lease

    async def handoff_call(self, call_id: str, **kwargs: Any) -> CallLease:
        return await self.repository.handoff_call(call_id, **kwargs)

    async def takeover_expired_call(self, call_id: str, **kwargs: Any) -> CallLease:
        return await self.repository.takeover_expired_call(call_id, **kwargs)

    async def terminalize(
        self,
        lease: CallLease,
        request: TerminalRequest,
    ) -> PersistedEvent:
        event = await self.repository.terminalize(lease, request)
        self.telemetry.finish_call(lease.call_id, request)
        return event

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None:
        await self.repository.append_transcript(call_id, turn)
        self.telemetry.observe_turn(call_id, turn)

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        await self.repository.record_tool_call(call_id, observation)
        self.telemetry.observe_tool(call_id, observation)

    async def record_latency(self, call_id: str, sample: LatencySample) -> None:
        await self.repository.record_latency(call_id, sample)
        self.telemetry.observe_latency(sample)

    async def fail_delivery(
        self,
        claim: DeliveryClaim,
        **kwargs: Any,
    ) -> DeliveryRecord:
        record = await self.repository.fail_delivery(claim, **kwargs)
        await self.refresh_dlq_depth()
        return record

    async def redeliver(self, event_id: str, **kwargs: Any) -> DeliveryRecord:
        record = await self.repository.redeliver(event_id, **kwargs)
        await self.refresh_dlq_depth()
        return record

    async def refresh_dlq_depth(self) -> int:
        depth = int(await self.repository.dlq_depth())
        self.telemetry.set_dlq_depth(depth)
        return depth

    async def close(self) -> None:
        close = getattr(self.repository, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        if self._shutdown_telemetry:
            await asyncio.to_thread(self.telemetry.force_flush)
            await asyncio.to_thread(self.telemetry.shutdown)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.repository, name)


class TelemetryServer:
    """Loopback-by-default Prometheus listener with explicit lifecycle."""

    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry
        self.app = FastAPI(
            title="voicey metrics",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        path = telemetry.settings.prometheus_path

        @self.app.get(path, include_in_schema=False)
        async def metrics() -> Response:  # pyright: ignore[reportUnusedFunction]
            return Response(
                content=self.telemetry.render_prometheus(),
                media_type=CONTENT_TYPE_LATEST,
                headers={"cache-control": "no-store"},
            )

        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.telemetry.start()
        if not self.telemetry.settings.prometheus_enabled or self._task is not None:
            return
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=self.telemetry.settings.prometheus_bind,
                port=self.telemetry.settings.prometheus_port,
                log_level="warning",
                access_log=False,
                proxy_headers=False,
            )
        )
        self._task = asyncio.create_task(self._server.serve(), name="voicey-metrics")
        try:
            await asyncio.wait_for(self._wait_started(), timeout=10)
        except Exception as exc:
            await self.stop()
            if isinstance(exc, VoiceyError):
                raise
            raise VoiceyError(
                "VY-OBS-006",
                detail="the Prometheus listener could not bind or start.",
            ) from exc

    async def stop(self) -> None:
        if self._task is not None:
            assert self._server is not None
            self._server.should_exit = True
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._server = None
        await asyncio.to_thread(self.telemetry.force_flush)
        await asyncio.to_thread(self.telemetry.shutdown)

    async def _wait_started(self) -> None:
        assert self._server is not None
        assert self._task is not None
        while not self._server.started:
            if self._task.done():
                with suppress(asyncio.CancelledError):
                    await self._task
                raise VoiceyError(
                    "VY-OBS-006",
                    detail="the Prometheus listener exited before readiness.",
                )
            await asyncio.sleep(0.01)


def _otlp_headers(
    settings: Observability,
    environment: Mapping[str, str],
) -> dict[str, str] | None:
    env_name = settings.otlp_headers_env
    if env_name is None:
        return None
    raw = environment.get(env_name)
    if raw is None:
        raise VoiceyError(
            "VY-OBS-006",
            detail=f"the OTLP headers environment variable {env_name} is missing.",
        )
    headers: dict[str, str] = {}
    for item in raw.split(","):
        name, separator, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not _HEADER_NAME.fullmatch(name) or not value:
            raise VoiceyError(
                "VY-OBS-006",
                detail=f"the OTLP headers environment variable {env_name} is malformed.",
            )
        headers[name] = value
    return headers
