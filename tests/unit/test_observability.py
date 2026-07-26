from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from voicekit.errors import VoicekitError
from voicekit.obs import (
    LatencySample,
    LatencySeries,
    NewCall,
    SQLiteCallRecordStore,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
    call_context,
    configure_logging,
    get_logger,
)
from voicekit.obs.logging import REDACTED

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
    with pytest.raises(VoicekitError) as unopened:
        await store.get_call("missing")
    assert unopened.value.code == "VK-OBS-001"

    await store.open()
    assert await store.open() is store
    await store.create_call(_new_call())

    with pytest.raises(VoicekitError) as missing:
        await store.get_call("missing")
    assert missing.value.code == "VK-OBS-003"

    with pytest.raises(VoicekitError) as duplicate:
        await store.create_call(_new_call())
    assert duplicate.value.code == "VK-OBS-002"

    with pytest.raises(VoicekitError) as bad_limit:
        await store.list_calls(limit=0)
    assert bad_limit.value.code == "VK-OBS-005"

    with pytest.raises(VoicekitError) as missing_parent:
        await store.append_transcript(
            "missing",
            TranscriptTurn(turn_id="turn", role="user", text="hello", t_ms=0),
        )
    assert missing_parent.value.code == "VK-OBS-002"
    await store.close()
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_store_rejects_unknown_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "calls.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(VoicekitError) as caught:
        await SQLiteCallRecordStore(database_path).open()

    assert caught.value.code == "VK-OBS-004"
    assert "supported schema is 2" in str(caught.value)


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
