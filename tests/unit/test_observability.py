from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from voicey.config.models import Observability
from voicey.errors import VoiceyError
from voicey.obs import (
    InstrumentedRepository,
    LatencySample,
    LatencySeries,
    NewCall,
    SQLiteCallRecordStore,
    Telemetry,
    TelemetryServer,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
    call_context,
    configure_logging,
    get_logger,
)
from voicey.obs.logging import REDACTED
from voicey.storage.models import (
    CallLease,
    DeliveryClaim,
    DeliveryRecord,
    PersistedEvent,
    ResultDeliveryConfig,
    TerminalRequest,
)

CONFIG_HASH = f"sha256:{'a' * 64}"


def _new_call(
    call_id: str = "call_01",
    *,
    started_at: datetime | None = None,
) -> NewCall:
    values: dict[str, object] = {
        "call_id": call_id,
        "agent_name": "clinic-front-desk",
        "runtime": "pipecat",
        "channel": "phone",
        "direction": "inbound",
        "provider": "twilio",
        "provider_call_id": f"CA_{call_id}",
        "config_hash": CONFIG_HASH,
    }
    if started_at is not None:
        values["started_at"] = started_at
    return NewCall.model_validate(values)


def test_json_logs_correlate_calls_and_remove_info_level_pii_and_secrets() -> None:
    output = StringIO()
    configure_logging(format="json", stream=output)

    with call_context("call_01", config_hash=CONFIG_HASH, runtime="pipecat"):
        get_logger(component="runtime").info(
            "caller +14155550123 wrote person@example.test",
            phone_number="+14155550123",
            transcript="My appointment is Tuesday",
            api_key="sk-super-secret-value",  # pragma: allowlist secret
            nested={
                "customer_name": "Ada Lovelace",
                "safe_count": 3,
                "authorization": "Bearer secret-token",  # pragma: allowlist secret
            },
        )

    line = output.getvalue()
    event = cast("dict[str, object]", json.loads(line))
    assert event["call_id"] == "call_01"
    assert event["config_hash"] == CONFIG_HASH
    assert event["runtime"] == "pipecat"
    assert event["component"] == "runtime"
    assert event["phone_number"] == REDACTED
    assert event["transcript"] == REDACTED
    assert "+14155550123" not in line
    assert "person@example.test" not in line
    assert "Ada Lovelace" not in line
    assert "super-secret-value" not in line
    assert event["level"] == "info"
    assert isinstance(event["timestamp"], str)


def test_debug_logs_may_hold_pii_but_never_secrets() -> None:
    output = StringIO()
    configure_logging(format="json", level="debug", stream=output)

    get_logger().debug(
        "diagnostic",
        phone_number="+14155550123",
        password="do-not-log",  # pragma: allowlist secret
    )

    event = cast("dict[str, object]", json.loads(output.getvalue()))
    assert event["phone_number"] == "+14155550123"
    assert event["password"] == REDACTED


def test_exception_tracebacks_are_not_emitted_at_error_level() -> None:
    output = StringIO()
    configure_logging(format="json", stream=output)

    try:
        raise RuntimeError("caller +14155550123")
    except RuntimeError:
        get_logger().exception("provider failed for person@example.test")

    line = output.getvalue()
    event = cast("dict[str, object]", json.loads(line))
    assert event["exception"] == REDACTED
    assert "+14155550123" not in line
    assert "person@example.test" not in line


def test_pretty_renderer_is_human_readable_and_context_is_restored() -> None:
    output = StringIO()
    configure_logging(format="pretty", stream=output)
    logger = get_logger()

    with call_context("call_pretty"):
        logger.info("call.started", safe_count=1)
    logger.info("engine.idle")

    lines = output.getvalue().splitlines()
    assert "call.started" in lines[0]
    assert "call_id=call_pretty" in lines[0]
    assert "engine.idle" in lines[1]
    assert "call_id" not in lines[1]


@pytest.mark.asyncio
async def test_call_log_context_is_isolated_between_parallel_tasks() -> None:
    output = StringIO()
    configure_logging(format="json", stream=output)

    async def emit(call_id: str) -> None:
        with call_context(call_id):
            await asyncio.sleep(0)
            get_logger().info("turn.completed")

    await asyncio.gather(*(emit(f"call_{index}") for index in range(12)))

    logged_ids = {
        cast("dict[str, str]", json.loads(line))["call_id"]
        for line in output.getvalue().splitlines()
    }
    assert logged_ids == {f"call_{index}" for index in range(12)}


def test_latency_series_records_turns_and_nearest_rank_summaries() -> None:
    series = LatencySeries()
    for turn_index, duration_ms in enumerate([100.0, 200.0, 300.0, 400.0], start=1):
        series.record(
            turn_id=f"turn_{turn_index}",
            turn_index=turn_index,
            metric="e2e",
            duration_ms=duration_ms,
        )
    series.record(
        turn_id="turn_1",
        turn_index=1,
        metric="llm_ttft",
        duration_ms=75,
    )

    summary = series.summaries()
    assert summary["e2e"].count == 4
    assert summary["e2e"].p50_ms == 200
    assert summary["e2e"].p95_ms == 400
    assert summary["e2e"].max_ms == 400
    assert len(series.snapshot()) == 5
    assert len(series.for_turn("turn_1")) == 2
    assert series.for_turn("missing") == ()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {
                "turn_id": "",
                "turn_index": 1,
                "metric": "e2e",
                "duration_ms": 1,
                "observed_at": datetime.now(UTC),
            },
            "Fix: use the runtime",
        ),
        (
            {
                "turn_id": "turn_1",
                "turn_index": 0,
                "metric": "e2e",
                "duration_ms": 1,
                "observed_at": datetime.now(UTC),
            },
            "Fix: number conversation",
        ),
        (
            {
                "turn_id": "turn_1",
                "turn_index": 1,
                "metric": "e2e",
                "duration_ms": float("inf"),
                "observed_at": datetime.now(UTC),
            },
            "Fix: record a finite",
        ),
        (
            {
                "turn_id": "turn_1",
                "turn_index": 1,
                "metric": "e2e",
                "duration_ms": 1,
                "observed_at": datetime.now(),
            },
            "Fix: record a UTC-aware",
        ),
    ],
)
def test_latency_samples_reject_invalid_data_with_fixes(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        LatencySample.model_validate(values)


@pytest.mark.asyncio
async def test_sqlite_store_is_protected_durable_and_materializes_observations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "private" / "calls.sqlite3"
    started_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    async with SQLiteCallRecordStore(database_path) as store:
        await store.create_call(_new_call(started_at=started_at))
        await store.append_timeline(
            "call_01",
            TimelineEvent(
                event_type="call.connected",
                occurred_at=started_at,
                details={"transport": "websocket", "attempt": 1},
            ),
        )
        await store.append_transcript(
            "call_01",
            TranscriptTurn(
                turn_id="turn_1",
                role="user",
                text="I need an appointment.",
                t_ms=420,
            ),
        )
        await store.record_tool_call(
            "call_01",
            ToolCallObservation(
                invocation_id="tool_01",
                tool_name="find_slots",
                arguments={"day": "Tuesday"},
                result={"slots": ["10:00"]},
                duration_ms=12.5,
                status="succeeded",
                occurred_at=started_at,
            ),
        )
        await store.record_latency(
            "call_01",
            LatencySample(
                turn_id="turn_1",
                turn_index=1,
                metric="llm_ttft",
                duration_ms=250.5,
                observed_at=started_at,
            ),
        )

        pragmas = await store.pragmas()
        record = await store.get_call("call_01")
        listed = await store.list_calls(status="active")

    assert pragmas == {
        "journal_mode": "wal",
        "synchronous": 2,
        "foreign_keys": 1,
        "busy_timeout": 5000,
    }
    assert record.started_at == started_at
    assert record.updated_at >= record.started_at
    assert record.status == "active"
    assert record.webhook_status == "not_ready"
    assert record.timeline[0].details["transport"] == "websocket"
    assert record.transcript[0].text == "I need an appointment."
    assert record.tool_calls[0].result == {"slots": ["10:00"]}
    assert record.latency[0].duration_ms == 250.5
    assert listed == (record,)
    if os.name == "posix":
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(database_path.parent.stat().st_mode) == 0o700

    async with SQLiteCallRecordStore(database_path) as reopened:
        assert (await reopened.get_call("call_01")).transcript == record.transcript


@pytest.mark.asyncio
async def test_sqlite_store_serializes_parallel_observation_writes(tmp_path: Path) -> None:
    async with SQLiteCallRecordStore(tmp_path / "calls.sqlite3") as store:
        await store.create_call(_new_call())
        await asyncio.gather(
            *(
                store.append_timeline(
                    "call_01",
                    TimelineEvent(event_type=f"turn.{index}", details={"index": index}),
                )
                for index in range(30)
            )
        )

        record = await store.get_call("call_01")

    assert len(record.timeline) == 30
    assert {cast("int", event.details["index"]) for event in record.timeline} == set(range(30))


@pytest.mark.asyncio
async def test_call_records_retain_pii_but_scrub_secret_shaped_values(
    tmp_path: Path,
) -> None:
    async with SQLiteCallRecordStore(tmp_path / "calls.sqlite3") as store:
        await store.create_call(_new_call())
        await store.append_transcript(
            "call_01",
            TranscriptTurn(
                turn_id="turn",
                role="user",
                text="accidentally said whsec_c2VjcmV0",  # pragma: allowlist secret
                t_ms=1,
            ),
        )
        await store.record_tool_call(
            "call_01",
            ToolCallObservation(
                invocation_id="tool_secret",
                tool_name="lookup",
                arguments={
                    "phone_number": "+14155550123",
                    "api_key": "secret-value",  # pragma: allowlist secret
                },
                result={
                    "authorization": "Bearer secret-token",  # pragma: allowlist secret
                },
                duration_ms=1,
                status="succeeded",
            ),
        )

        record = await store.get_call("call_01")

    assert "+14155550123" in json.dumps(record.tool_calls[0].arguments, sort_keys=True)
    serialized = record.model_dump_json()
    assert "c2VjcmV0" not in serialized
    assert "secret-value" not in serialized
    assert "secret-token" not in serialized
    assert serialized.count(REDACTED) >= 3


@pytest.mark.asyncio
async def test_sqlite_store_maps_missing_duplicate_and_invalid_queries(
    tmp_path: Path,
) -> None:
    store = SQLiteCallRecordStore(tmp_path / "calls.sqlite3")
    with pytest.raises(VoiceyError) as unopened:
        await store.get_call("missing")
    assert unopened.value.code == "VY-OBS-001"

    await store.open()
    assert await store.open() is store
    await store.create_call(_new_call())

    with pytest.raises(VoiceyError) as missing:
        await store.get_call("missing")
    assert missing.value.code == "VY-OBS-003"

    with pytest.raises(VoiceyError) as duplicate:
        await store.create_call(_new_call())
    assert duplicate.value.code == "VY-OBS-002"

    with pytest.raises(VoiceyError) as bad_limit:
        await store.list_calls(limit=0)
    assert bad_limit.value.code == "VY-OBS-005"

    with pytest.raises(VoiceyError) as missing_parent:
        await store.append_transcript(
            "missing",
            TranscriptTurn(turn_id="turn", role="user", text="hello", t_ms=0),
        )
    assert missing_parent.value.code == "VY-OBS-002"
    await store.close()
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_store_rejects_unknown_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "calls.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(VoiceyError) as caught:
        await SQLiteCallRecordStore(database_path).open()

    assert caught.value.code == "VY-OBS-004"
    assert "supported schema is 3" in str(caught.value)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: NewCall(
                call_id="call",
                agent_name="agent",
                runtime="pipecat",
                channel="web",
                direction="inbound",
                config_hash="wrong",
            ),
            "Fix: pass Agent.config_hash",
        ),
        (
            lambda: TimelineEvent(event_type="", details={}),
            "Fix: provide a stable dotted",
        ),
        (
            lambda: TranscriptTurn(
                turn_id="turn",
                role="user",
                text="hello",
                t_ms=-1,
            ),
            "Fix: record an offset",
        ),
        (
            lambda: ToolCallObservation(
                invocation_id="tool",
                tool_name="find",
                arguments={},
                duration_ms=-1,
                status="failed",
            ),
            "Fix: record a non-negative",
        ),
    ],
)
def test_call_observation_models_reject_invalid_values(
    factory: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        factory()


def test_prometheus_registry_has_bounded_runtime_metrics_without_pii() -> None:
    telemetry = Telemetry(
        agent_name="clinic-front-desk",
        runtime="pipecat",
        settings=Observability(prometheus_enabled=True),
    )
    call = _new_call()
    telemetry.begin_call(call)
    telemetry.observe_latency(
        LatencySample(
            turn_id="turn_1",
            turn_index=1,
            metric="e2e",
            duration_ms=420,
            observed_at=datetime.now(UTC),
        )
    )
    telemetry.observe_turn(
        call.call_id,
        TranscriptTurn(
            turn_id="turn_1",
            role="user",
            text="person@example.test needs +14155550123",
            t_ms=12,
        ),
    )
    telemetry.observe_tool(
        call.call_id,
        ToolCallObservation(
            invocation_id="tool_1",
            tool_name="find_slots",
            arguments={"email": "person@example.test"},
            result={"phone": "+14155550123"},
            duration_ms=15,
            status="timed_out",
        ),
    )
    telemetry.finish_call(
        call.call_id,
        TerminalRequest(
            event_type="call.failed",
            ended_reason="provider_error",
        ),
    )
    rendered = telemetry.render_prometheus().decode()

    assert 'voicey_calls_total{agent="clinic-front-desk",runtime="pipecat"} 1.0' in rendered
    assert 'voicey_active_calls{agent="clinic-front-desk",runtime="pipecat"} 0.0' in rendered
    assert (
        'voicey_errors_total{agent="clinic-front-desk",code="VY-RUN-002",runtime="pipecat"} 1.0'
    ) in rendered
    assert 'metric="e2e"' in rendered
    assert "person@example.test" not in rendered
    assert "+14155550123" not in rendered


@pytest.mark.asyncio
async def test_prometheus_app_uses_configured_path_and_no_store() -> None:
    telemetry = Telemetry(
        agent_name="agent",
        runtime="livekit",
        settings=Observability(
            prometheus_enabled=True,
            prometheus_path="/internal/metrics",
        ),
    )
    server = TelemetryServer(telemetry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="http://metrics.test",
    ) as client:
        response = await client.get("/internal/metrics")
        missing = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "text/plain" in response.headers["content-type"]
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_telemetry_server_starts_real_listener_and_disabled_lifecycle() -> None:
    disabled = Telemetry(
        agent_name="agent",
        runtime="pipecat",
        settings=Observability(),
    )
    assert not disabled.enabled
    disabled_server = TelemetryServer(disabled)
    await disabled_server.start()
    await disabled_server.stop()

    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    port = cast("tuple[str, int]", reservation.getsockname())[1]
    reservation.close()
    enabled = Telemetry(
        agent_name="agent",
        runtime="livekit",
        settings=Observability(prometheus_enabled=True, prometheus_port=port),
    )
    assert enabled.enabled
    server = TelemetryServer(enabled)
    await server.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/metrics")
        assert response.status_code == 200
    finally:
        await server.stop()


def test_otlp_emits_call_turn_and_tool_spans_without_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voicey.obs.telemetry as telemetry_module

    exporter = InMemorySpanExporter()

    def exporter_factory(**_kwargs: object) -> InMemorySpanExporter:
        return exporter

    monkeypatch.setattr(
        telemetry_module,
        "OTLPSpanExporter",
        exporter_factory,
    )
    telemetry = Telemetry(
        agent_name="agent",
        runtime="pipecat",
        settings=Observability(
            otlp_endpoint="http://127.0.0.1:4318/v1/traces",
            otlp_headers_env="VOICEY_TEST_OTLP_HEADERS",
        ),
        environment={
            "VOICEY_TEST_OTLP_HEADERS": "authorization=test-only",
        },
    )
    call = _new_call("call_trace")
    telemetry.begin_call(call)
    telemetry.begin_call(call)
    telemetry.observe_turn(
        call.call_id,
        TranscriptTurn(
            turn_id="turn_1",
            role="assistant",
            text="Private transcript content",
            t_ms=20,
        ),
    )
    telemetry.observe_tool(
        call.call_id,
        ToolCallObservation(
            invocation_id="tool_trace",
            tool_name="lookup",
            arguments={"secret": "private"},  # pragma: allowlist secret
            result={"private": True},
            duration_ms=4,
            status="timed_out",
        ),
    )
    telemetry.finish_call(
        call.call_id,
        TerminalRequest(
            event_type="call.failed",
            ended_reason="provider_error",
        ),
    )
    assert telemetry.force_flush()

    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {
        "voicey.call",
        "voicey.turn",
        "voicey.tool",
    }
    serialized = repr([dict(span.attributes or {}) for span in spans])
    assert "Private transcript content" not in serialized
    assert "'secret': 'private'" not in serialized  # pragma: allowlist secret
    telemetry.shutdown()


def test_otlp_missing_or_malformed_header_env_fails_with_catalog_error() -> None:
    settings = Observability(
        otlp_endpoint="http://127.0.0.1:4318/v1/traces",
        otlp_headers_env="VOICEY_TEST_OTLP_HEADERS",
    )
    for environment in ({}, {"VOICEY_TEST_OTLP_HEADERS": "not-a-header"}):
        telemetry = Telemetry(
            agent_name="agent",
            runtime="pipecat",
            settings=settings,
            environment=environment,
        )
        with pytest.raises(VoiceyError) as caught:
            telemetry.begin_call(_new_call("call_bad_header"))
        assert caught.value.code == "VY-OBS-006"


@pytest.mark.asyncio
async def test_repository_metrics_follow_only_successful_durable_writes() -> None:
    class Repository:
        fail = True

        async def begin_call(self, call: NewCall, **_kwargs: object) -> object:
            if self.fail:
                raise RuntimeError("durable write failed")
            return object()

    telemetry = Telemetry(
        agent_name="agent",
        runtime="pipecat",
        settings=Observability(),
    )
    raw = Repository()
    repository = InstrumentedRepository(raw, telemetry)
    call = _new_call("call_transactional_metrics")
    with pytest.raises(RuntimeError, match="durable write failed"):
        await repository.begin_call(
            call,
            owner_id="owner",
            delivery=object(),  # type: ignore[arg-type]
            lease_ttl=object(),  # type: ignore[arg-type]
        )
    sample = 'voicey_calls_total{agent="agent",runtime="pipecat"}'
    assert f"{sample} 0.0" in telemetry.render_prometheus().decode()

    raw.fail = False
    await repository.begin_call(
        call,
        owner_id="owner",
        delivery=object(),  # type: ignore[arg-type]
        lease_ttl=object(),  # type: ignore[arg-type]
    )
    assert f"{sample} 1.0" in telemetry.render_prometheus().decode()


@pytest.mark.asyncio
async def test_instrumented_repository_observes_terminal_dlq_and_close() -> None:
    now = datetime.now(UTC)
    lease = CallLease(
        call_id="call_instrumented",
        owner_id="owner",
        generation=1,
        expires_at=now + timedelta(seconds=30),
    )
    event = PersistedEvent(
        event_id="evt_instrumented",
        call_id=lease.call_id,
        event_type="call.failed",
        body=b"{}",
        created_at=now,
    )
    delivery = DeliveryRecord(
        event_id=event.event_id,
        call_id=lease.call_id,
        endpoint="https://receiver.example.test/results",
        status="dead_lettered",
        attempt_count=8,
        next_attempt_at=now,
        last_error="HTTP 500",
        delivered_at=None,
    )

    class Repository:
        closed = False

        async def begin_call(self, _call: NewCall, **_kwargs: object) -> CallLease:
            return lease

        async def append_transcript(
            self,
            _call_id: str,
            _turn: TranscriptTurn,
        ) -> None:
            return

        async def record_tool_call(
            self,
            _call_id: str,
            _observation: ToolCallObservation,
        ) -> None:
            return

        async def record_latency(
            self,
            _call_id: str,
            _sample: LatencySample,
        ) -> None:
            return

        async def terminalize(
            self,
            _lease: CallLease,
            _request: TerminalRequest,
        ) -> PersistedEvent:
            return event

        async def fail_delivery(
            self,
            _claim: DeliveryClaim,
            **_kwargs: object,
        ) -> DeliveryRecord:
            return delivery

        async def redeliver(
            self,
            _event_id: str,
            **_kwargs: object,
        ) -> DeliveryRecord:
            return delivery.model_copy(update={"status": "pending"})

        async def dlq_depth(self) -> int:
            return 3

        async def passthrough(self) -> str:
            return "ok"

        async def close(self) -> None:
            self.closed = True

    telemetry = Telemetry(
        agent_name="agent",
        runtime="pipecat",
        settings=Observability(),
    )
    raw = Repository()
    repository = InstrumentedRepository(raw, telemetry, shutdown_telemetry=True)
    call = _new_call(lease.call_id)
    returned_lease = await repository.begin_call(
        call,
        owner_id="owner",
        delivery=ResultDeliveryConfig(endpoint="https://receiver.example.test/results"),
        lease_ttl=timedelta(seconds=30),
    )
    await repository.append_transcript(
        call.call_id,
        TranscriptTurn(turn_id="turn_1", role="user", text="private", t_ms=1),
    )
    await repository.record_tool_call(
        call.call_id,
        ToolCallObservation(
            invocation_id="tool",
            tool_name="lookup",
            arguments={"private": True},
            duration_ms=2,
            status="succeeded",
        ),
    )
    await repository.record_latency(
        call.call_id,
        LatencySample(
            turn_id="turn_1",
            turn_index=1,
            metric="llm_ttft",
            duration_ms=100,
            observed_at=now,
        ),
    )
    request = TerminalRequest(event_type="call.failed", ended_reason="worker_crash")
    assert await repository.terminalize(returned_lease, request) == event
    claim = DeliveryClaim(
        event_id=event.event_id,
        call_id=call.call_id,
        endpoint=delivery.endpoint,
        body=b"{}",
        attempt_count=8,
        lease_owner="delivery-owner",
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert await repository.fail_delivery(claim, error="HTTP 500") == delivery
    assert (await repository.redeliver(event.event_id)).status == "pending"
    assert await repository.refresh_dlq_depth() == 3
    assert await repository.passthrough() == "ok"
    await repository.close()

    rendered = telemetry.render_prometheus().decode()
    assert 'code="VY-RUN-006"' in rendered
    assert 'metric="llm_ttft"' in rendered
    assert "} 3.0" in rendered
    assert raw.closed


def test_telemetry_admission_and_error_cardinality_are_idempotent() -> None:
    telemetry = Telemetry(
        agent_name="agent",
        runtime="livekit",
        settings=Observability(),
    )
    telemetry.admit_call("call_1")
    telemetry.admit_call("call_1")
    assert telemetry.release_call("call_1")
    assert not telemetry.release_call("call_1")
    telemetry.record_error("not-a-catalog-code")
    with pytest.raises(VoiceyError) as negative:
        telemetry.set_dlq_depth(-1)

    rendered = telemetry.render_prometheus().decode()
    assert 'voicey_calls_total{agent="agent",runtime="livekit"} 1.0' in rendered
    assert 'code="VY-CLI-009"' in rendered
    assert negative.value.code == "VY-OBS-006"


def test_telemetry_resets_process_local_state_after_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voicey.obs.telemetry as telemetry_module

    telemetry = Telemetry(
        agent_name="agent",
        runtime="livekit",
        settings=Observability(),
    )
    telemetry.admit_call("parent")
    parent_pid = os.getpid()
    monkeypatch.setattr(telemetry_module.os, "getpid", lambda: parent_pid + 1)

    telemetry.admit_call("child", count=False)

    assert telemetry.release_call("child")
    assert not telemetry.release_call("parent")
