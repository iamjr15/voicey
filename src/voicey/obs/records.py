"""Protected SQLite foundation for durable per-call observation records."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, TypeAlias

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from voicey.config.models import RuntimeName
from voicey.errors import VoiceyError
from voicey.obs.latency import LatencySample
from voicey.obs.logging import scrub_secrets
from voicey.security.files import ensure_private_file

Channel: TypeAlias = Literal["phone", "web"]
Direction: TypeAlias = Literal["inbound", "outbound"]
CallStatus: TypeAlias = Literal["active", "completed", "failed"]
WebhookStatus: TypeAlias = Literal[
    "not_ready",
    "pending",
    "delivered",
    "dead_lettered",
]
TranscriptRole: TypeAlias = Literal["user", "assistant", "system", "tool"]
ToolStatus: TypeAlias = Literal["succeeded", "failed", "timed_out"]

SCHEMA_VERSION = 3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    runtime TEXT NOT NULL,
    channel TEXT NOT NULL,
    direction TEXT NOT NULL,
    provider TEXT,
    provider_call_id TEXT,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    webhook_status TEXT NOT NULL DEFAULT 'not_ready',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    terminal_reason TEXT,
    CHECK (runtime IN ('pipecat', 'livekit')),
    CHECK (channel IN ('phone', 'web')),
    CHECK (direction IN ('inbound', 'outbound')),
    CHECK (status IN ('active', 'completed', 'failed')),
    CHECK (webhook_status IN ('not_ready', 'pending', 'delivered', 'dead_lettered'))
);

CREATE INDEX IF NOT EXISTS calls_started_at_idx
    ON calls(started_at DESC);
CREATE INDEX IF NOT EXISTS calls_status_updated_idx
    ON calls(status, updated_at);

CREATE TABLE IF NOT EXISTS call_timeline (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_timeline_call_idx
    ON call_timeline(call_id, sequence);

CREATE TABLE IF NOT EXISTS call_transcript (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    t_ms INTEGER NOT NULL,
    CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    CHECK (t_ms >= 0)
);
CREATE INDEX IF NOT EXISTS call_transcript_call_idx
    ON call_transcript(call_id, sequence);

CREATE TABLE IF NOT EXISTS call_tools (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    invocation_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    CHECK (duration_ms >= 0),
    CHECK (status IN ('succeeded', 'failed', 'timed_out'))
);
CREATE INDEX IF NOT EXISTS call_tools_call_idx
    ON call_tools(call_id, sequence);

CREATE TABLE IF NOT EXISTS call_latency (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    metric TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    observed_at TEXT NOT NULL,
    CHECK (turn_index >= 1),
    CHECK (duration_ms >= 0),
    CHECK (metric IN ('stt_partial', 'stt_final', 'llm_ttft', 'tts_ttfb', 'e2e'))
);
CREATE INDEX IF NOT EXISTS call_latency_call_idx
    ON call_latency(call_id, sequence);
"""

_MIGRATION_V2 = """
ALTER TABLE calls ADD COLUMN from_number TEXT;
ALTER TABLE calls ADD COLUMN to_number TEXT;
ALTER TABLE calls ADD COLUMN owner_id TEXT;
ALTER TABLE calls ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE calls ADD COLUMN lease_expires_at TEXT;
ALTER TABLE calls ADD COLUMN delivery_endpoint TEXT;
ALTER TABLE calls ADD COLUMN include_json TEXT NOT NULL
    DEFAULT '["transcript","data","recording","metrics"]';
ALTER TABLE calls ADD COLUMN redact_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE calls ADD COLUMN purge_after_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE calls ADD COLUMN recording_id TEXT;
ALTER TABLE calls ADD COLUMN outcome TEXT;
ALTER TABLE calls ADD COLUMN results_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE calls ADD COLUMN interruptions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE calls ADD COLUMN last_provider_state TEXT;

CREATE TABLE call_events (
    event_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    is_terminal INTEGER NOT NULL,
    body BLOB NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (event_type IN (
        'call.started',
        'call.completed',
        'call.failed',
        'call.recording.ready'
    )),
    CHECK (is_terminal IN (0, 1))
);
CREATE UNIQUE INDEX one_terminal_event_per_call
    ON call_events(call_id) WHERE is_terminal = 1;
CREATE UNIQUE INDEX one_recording_ready_event_per_call
    ON call_events(call_id) WHERE event_type = 'call.recording.ready';
CREATE INDEX call_events_call_idx ON call_events(call_id, created_at);

CREATE TABLE deliveries (
    event_id TEXT NOT NULL REFERENCES call_events(event_id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    delivered_at TEXT,
    PRIMARY KEY(event_id, endpoint),
    CHECK (status IN ('pending', 'delivering', 'delivered', 'dead_lettered')),
    CHECK (attempt_count >= 0)
);
CREATE INDEX deliveries_claim_idx
    ON deliveries(status, next_attempt_at, lease_expires_at);

CREATE TABLE recordings (
    recording_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    access_url TEXT,
    storage_key TEXT,
    created_at TEXT NOT NULL,
    ready_at TEXT,
    CHECK (status IN ('pending', 'ready', 'failed'))
);

CREATE TABLE backups (
    backup_id TEXT PRIMARY KEY,
    storage_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE purge_queue (
    storage_key TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    CHECK (artifact_kind IN ('recording', 'backup'))
);
"""

_MIGRATION_V3 = """
BEGIN IMMEDIATE;

ALTER TABLE call_timeline ADD COLUMN relay_operation_id TEXT;
ALTER TABLE call_transcript ADD COLUMN relay_operation_id TEXT;
ALTER TABLE call_tools ADD COLUMN relay_operation_id TEXT;
ALTER TABLE call_latency ADD COLUMN relay_operation_id TEXT;

CREATE UNIQUE INDEX call_timeline_relay_operation_idx
    ON call_timeline(relay_operation_id)
    WHERE relay_operation_id IS NOT NULL;
CREATE UNIQUE INDEX call_transcript_relay_operation_idx
    ON call_transcript(relay_operation_id)
    WHERE relay_operation_id IS NOT NULL;
CREATE UNIQUE INDEX call_tools_relay_operation_idx
    ON call_tools(relay_operation_id)
    WHERE relay_operation_id IS NOT NULL;
CREATE UNIQUE INDEX call_latency_relay_operation_idx
    ON call_latency(relay_operation_id)
    WHERE relay_operation_id IS NOT NULL;

PRAGMA user_version = 3;
COMMIT;
"""


class ObservationModel(BaseModel):
    """Strict base for data persisted in the protected call record."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NewCall(ObservationModel):
    """Fields durably written before a call becomes externally visible."""

    call_id: str
    agent_name: str
    runtime: RuntimeName
    channel: Channel
    direction: Direction
    provider: str | None = None
    provider_call_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    config_hash: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("call_id", "agent_name", "config_hash")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value:
            msg = "required call metadata is empty. Fix: provide a stable non-empty value."
            raise ValueError(msg)
        return value

    @field_validator("config_hash")
    @classmethod
    def valid_config_hash(cls, value: str) -> str:
        digest = value.removeprefix("sha256:")
        if not value.startswith("sha256:") or len(digest) != 64:
            msg = (
                "config_hash is not a canonical SHA-256. "
                "Fix: pass Agent.config_hash when creating the call record."
            )
            raise ValueError(msg)
        try:
            int(digest, 16)
        except ValueError as exc:
            msg = (
                "config_hash is not hexadecimal. "
                "Fix: pass Agent.config_hash when creating the call record."
            )
            raise ValueError(msg) from exc
        return value

    @field_validator("started_at")
    @classmethod
    def aware_started_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="started_at")


class TimelineEvent(ObservationModel):
    """One non-PII lifecycle marker for an operator-visible timeline."""

    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def valid_event_type(cls, value: str) -> str:
        if not value:
            msg = "event_type is empty. Fix: provide a stable dotted event name."
            raise ValueError(msg)
        return value

    @field_validator("occurred_at")
    @classmethod
    def aware_occurred_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="occurred_at")


class TranscriptTurn(ObservationModel):
    """One incrementally persisted transcript turn."""

    turn_id: str
    role: TranscriptRole
    text: str
    t_ms: int

    @field_validator("turn_id")
    @classmethod
    def valid_turn_id(cls, value: str) -> str:
        if not value:
            msg = "turn_id is empty. Fix: use the runtime's stable turn identifier."
            raise ValueError(msg)
        return value

    @field_validator("t_ms")
    @classmethod
    def valid_offset(cls, value: int) -> int:
        if value < 0:
            msg = "transcript t_ms is negative. Fix: record an offset from call start."
            raise ValueError(msg)
        return value


class ToolCallObservation(ObservationModel):
    """A final structured tool invocation observation."""

    invocation_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    result: JsonValue | None = None
    duration_ms: float
    status: ToolStatus
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("invocation_id", "tool_name")
    @classmethod
    def required_identifier(cls, value: str) -> str:
        if not value:
            msg = "tool observation identifier is empty. Fix: provide stable invocation data."
            raise ValueError(msg)
        return value

    @field_validator("duration_ms")
    @classmethod
    def valid_duration(cls, value: float) -> float:
        if value < 0:
            msg = "tool duration_ms is negative. Fix: record a non-negative duration."
            raise ValueError(msg)
        return value

    @field_validator("occurred_at")
    @classmethod
    def aware_occurred_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="occurred_at")


class CallRecord(ObservationModel):
    """Materialized protected call record used by CLI and playground reads."""

    call_id: str
    agent_name: str
    runtime: RuntimeName
    channel: Channel
    direction: Direction
    provider: str | None
    provider_call_id: str | None
    from_number: str | None
    to_number: str | None
    config_hash: str
    status: CallStatus
    webhook_status: WebhookStatus
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    terminal_reason: str | None
    timeline: tuple[TimelineEvent, ...]
    transcript: tuple[TranscriptTurn, ...]
    tool_calls: tuple[ToolCallObservation, ...]
    latency: tuple[LatencySample, ...]


class SQLiteCallRecordStore:
    """One serialized writer connection with WAL and FULL durability."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> Self:
        """Open and validate the protected local schema."""
        if self._db is not None:
            return self
        try:
            ensure_private_file(self.path)
            database = await aiosqlite.connect(self.path)
            database.row_factory = aiosqlite.Row
            await database.execute("PRAGMA foreign_keys = ON")
            await database.execute("PRAGMA busy_timeout = 5000")
            await database.execute("PRAGMA journal_mode = WAL")
            await database.execute("PRAGMA synchronous = FULL")
            await database.execute("PRAGMA secure_delete = ON")
            cursor = await database.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            version = int(row[0]) if row is not None else 0
            if version not in {0, 1, 2, SCHEMA_VERSION}:
                await database.close()
                raise VoiceyError(
                    "VY-OBS-004",
                    detail=f"found schema {version}; supported schema is {SCHEMA_VERSION}.",
                )
            if version == 0:
                await database.executescript(_SCHEMA_V1)
                version = 1
            if version == 1:
                await database.executescript(_MIGRATION_V2)
                version = 2
            if version == 2:
                await database.executescript(_MIGRATION_V3)
                await database.commit()
            self._db = database
            return self
        except VoiceyError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise VoiceyError("VY-OBS-001", detail=f"{self.path}: {exc}") from exc

    async def close(self) -> None:
        """Flush and close the writer connection."""
        if self._db is None:
            return
        database, self._db = self._db, None
        try:
            await database.commit()
            await database.close()
        except sqlite3.Error as exc:
            raise VoiceyError("VY-OBS-001", detail=f"{self.path}: {exc}") from exc

    async def pragmas(self) -> dict[str, str | int]:
        """Expose durability settings for preflight and tests."""
        database = self._connection()
        values: dict[str, str | int] = {}
        for pragma in ("journal_mode", "synchronous", "foreign_keys", "busy_timeout"):
            cursor = await database.execute(f"PRAGMA {pragma}")
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                values[pragma] = row[0]
        return values

    async def ready(self) -> bool:
        """Validate local durability settings for relay readiness."""
        values = await self.pragmas()
        return (
            str(values.get("journal_mode", "")).casefold() == "wal"
            and int(values.get("synchronous", 0)) == 2
            and int(values.get("foreign_keys", 0)) == 1
        )

    async def create_call(self, call: NewCall) -> None:
        """Durably create the lifecycle row before external call activity."""
        started_at = _to_iso(call.started_at)
        await self._write(
            """
            INSERT INTO calls (
                call_id, agent_name, runtime, channel, direction, provider,
                provider_call_id, from_number, to_number, config_hash,
                started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call.call_id,
                call.agent_name,
                call.runtime,
                call.channel,
                call.direction,
                call.provider,
                call.provider_call_id,
                call.from_number,
                call.to_number,
                call.config_hash,
                started_at,
                started_at,
            ),
        )

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None:
        """Append one ordered lifecycle marker."""
        await self._write(
            """
            INSERT INTO call_timeline(call_id, event_type, occurred_at, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                call_id,
                event.event_type,
                _to_iso(event.occurred_at),
                _json(event.details),
            ),
            touch_call_id=call_id,
        )

    async def append_timeline_once(
        self,
        call_id: str,
        event: TimelineEvent,
        *,
        operation_id: str,
        owner_id: str,
        generation: int,
    ) -> None:
        """Append a relayed timeline marker at most once."""
        await self._write_relay_observation(
            """
            INSERT OR IGNORE INTO call_timeline(
                call_id, event_type, occurred_at, details_json, relay_operation_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                call_id,
                event.event_type,
                _to_iso(event.occurred_at),
                _json(event.details),
                operation_id,
            ),
            table="call_timeline",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None:
        """Incrementally persist one transcript turn."""
        await self._write(
            """
            INSERT INTO call_transcript(call_id, turn_id, role, text, t_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                call_id,
                turn.turn_id,
                turn.role,
                str(scrub_secrets(turn.text)),
                turn.t_ms,
            ),
            touch_call_id=call_id,
        )

    async def append_transcript_once(
        self,
        call_id: str,
        turn: TranscriptTurn,
        *,
        operation_id: str,
        owner_id: str,
        generation: int,
    ) -> None:
        """Append a relayed transcript turn at most once."""
        await self._write_relay_observation(
            """
            INSERT OR IGNORE INTO call_transcript(
                call_id, turn_id, role, text, t_ms, relay_operation_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                turn.turn_id,
                turn.role,
                str(scrub_secrets(turn.text)),
                turn.t_ms,
                operation_id,
            ),
            table="call_transcript",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        """Persist one final tool observation without stack traces."""
        await self._write(
            """
            INSERT INTO call_tools(
                call_id, invocation_id, tool_name, arguments_json, result_json,
                duration_ms, status, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                observation.invocation_id,
                observation.tool_name,
                _json(observation.arguments),
                None if observation.result is None else _json(observation.result),
                observation.duration_ms,
                observation.status,
                _to_iso(observation.occurred_at),
            ),
            touch_call_id=call_id,
        )

    async def record_tool_call_once(
        self,
        call_id: str,
        observation: ToolCallObservation,
        *,
        operation_id: str,
        owner_id: str,
        generation: int,
    ) -> None:
        """Persist a relayed final tool observation at most once."""
        await self._write_relay_observation(
            """
            INSERT OR IGNORE INTO call_tools(
                call_id, invocation_id, tool_name, arguments_json, result_json,
                duration_ms, status, occurred_at, relay_operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                observation.invocation_id,
                observation.tool_name,
                _json(observation.arguments),
                None if observation.result is None else _json(observation.result),
                observation.duration_ms,
                observation.status,
                _to_iso(observation.occurred_at),
                operation_id,
            ),
            table="call_tools",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def record_latency(self, call_id: str, sample: LatencySample) -> None:
        """Persist one validated latency sample."""
        await self._write(
            """
            INSERT INTO call_latency(
                call_id, turn_id, turn_index, metric, duration_ms, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                sample.turn_id,
                sample.turn_index,
                sample.metric,
                sample.duration_ms,
                _to_iso(sample.observed_at),
            ),
            touch_call_id=call_id,
        )

    async def record_latency_once(
        self,
        call_id: str,
        sample: LatencySample,
        *,
        operation_id: str,
        owner_id: str,
        generation: int,
    ) -> None:
        """Persist a relayed latency sample at most once."""
        await self._write_relay_observation(
            """
            INSERT OR IGNORE INTO call_latency(
                call_id, turn_id, turn_index, metric, duration_ms, observed_at,
                relay_operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                sample.turn_id,
                sample.turn_index,
                sample.metric,
                sample.duration_ms,
                _to_iso(sample.observed_at),
                operation_id,
            ),
            table="call_latency",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def get_call(self, call_id: str) -> CallRecord:
        """Materialize one protected call and all ordered observations."""
        database = self._connection()
        cursor = await database.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise VoiceyError("VY-OBS-003", detail=call_id)
        return await self._materialize(row)

    async def list_calls(
        self,
        *,
        status: CallStatus | None = None,
        limit: int = 100,
    ) -> tuple[CallRecord, ...]:
        """List newest calls with an optional lifecycle-state filter."""
        if not 1 <= limit <= 1000:
            raise VoiceyError("VY-OBS-005", detail=f"limit={limit}")
        database = self._connection()
        if status is None:
            cursor = await database.execute(
                "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        else:
            cursor = await database.execute(
                "SELECT * FROM calls WHERE status = ? ORDER BY started_at DESC LIMIT ?",
                (status, limit),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple([await self._materialize(row) for row in rows])

    async def _write(
        self,
        statement: str,
        parameters: tuple[object, ...],
        *,
        touch_call_id: str | None = None,
    ) -> None:
        database = self._connection()
        async with self._write_lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                await database.execute(statement, parameters)
                if touch_call_id is not None:
                    await database.execute(
                        "UPDATE calls SET updated_at = ? WHERE call_id = ?",
                        (_to_iso(datetime.now(UTC)), touch_call_id),
                    )
                await database.commit()
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError("VY-OBS-002", detail=str(exc)) from exc

    async def _write_relay_observation(
        self,
        statement: str,
        parameters: tuple[object, ...],
        *,
        table: str,
        call_id: str,
        operation_id: str,
        owner_id: str,
        generation: int,
    ) -> None:
        database = self._connection()
        async with self._write_lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                cursor = await database.execute(
                    """
                    SELECT 1 FROM calls
                    WHERE call_id = ? AND owner_id = ? AND generation = ?
                    """,
                    (call_id, owner_id, generation),
                )
                fenced = await cursor.fetchone()
                await cursor.close()
                if fenced is None:
                    raise VoiceyError("VY-REL-004", detail=f"{call_id} generation is stale.")
                await database.execute(statement, parameters)
                cursor = await database.execute(
                    f"SELECT call_id FROM {table} WHERE relay_operation_id = ?",
                    (operation_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None or str(row["call_id"]) != call_id:
                    raise VoiceyError(
                        "VY-REL-005",
                        detail="relay operation id collides with another observation.",
                    )
                await database.execute(
                    "UPDATE calls SET updated_at = ? WHERE call_id = ?",
                    (_to_iso(datetime.now(UTC)), call_id),
                )
                await database.commit()
            except VoiceyError:
                await database.rollback()
                raise
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError("VY-REL-006", detail=str(exc)) from exc

    async def _materialize(self, row: aiosqlite.Row) -> CallRecord:
        database = self._connection()
        call_id = str(row["call_id"])
        timeline_rows = await _fetch_all(
            database,
            "SELECT * FROM call_timeline WHERE call_id = ? ORDER BY sequence",
            (call_id,),
        )
        transcript_rows = await _fetch_all(
            database,
            "SELECT * FROM call_transcript WHERE call_id = ? ORDER BY sequence",
            (call_id,),
        )
        tool_rows = await _fetch_all(
            database,
            "SELECT * FROM call_tools WHERE call_id = ? ORDER BY sequence",
            (call_id,),
        )
        latency_rows = await _fetch_all(
            database,
            "SELECT * FROM call_latency WHERE call_id = ? ORDER BY sequence",
            (call_id,),
        )
        return CallRecord.model_validate(
            {
                "call_id": call_id,
                "agent_name": row["agent_name"],
                "runtime": row["runtime"],
                "channel": row["channel"],
                "direction": row["direction"],
                "provider": row["provider"],
                "provider_call_id": row["provider_call_id"],
                "from_number": row["from_number"],
                "to_number": row["to_number"],
                "config_hash": row["config_hash"],
                "status": row["status"],
                "webhook_status": row["webhook_status"],
                "started_at": _from_iso(str(row["started_at"])),
                "updated_at": _from_iso(str(row["updated_at"])),
                "ended_at": (None if row["ended_at"] is None else _from_iso(str(row["ended_at"]))),
                "terminal_reason": row["terminal_reason"],
                "timeline": [
                    {
                        "event_type": item["event_type"],
                        "occurred_at": _from_iso(str(item["occurred_at"])),
                        "details": json.loads(str(item["details_json"])),
                    }
                    for item in timeline_rows
                ],
                "transcript": [
                    {
                        "turn_id": item["turn_id"],
                        "role": item["role"],
                        "text": item["text"],
                        "t_ms": item["t_ms"],
                    }
                    for item in transcript_rows
                ],
                "tool_calls": [
                    {
                        "invocation_id": item["invocation_id"],
                        "tool_name": item["tool_name"],
                        "arguments": json.loads(str(item["arguments_json"])),
                        "result": (
                            None
                            if item["result_json"] is None
                            else json.loads(str(item["result_json"]))
                        ),
                        "duration_ms": item["duration_ms"],
                        "status": item["status"],
                        "occurred_at": _from_iso(str(item["occurred_at"])),
                    }
                    for item in tool_rows
                ],
                "latency": [
                    {
                        "turn_id": item["turn_id"],
                        "turn_index": item["turn_index"],
                        "metric": item["metric"],
                        "duration_ms": item["duration_ms"],
                        "observed_at": _from_iso(str(item["observed_at"])),
                    }
                    for item in latency_rows
                ],
            }
        )

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise VoiceyError("VY-OBS-001", detail="call-record store is not open.")
        return self._db


async def _fetch_all(
    database: aiosqlite.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> list[aiosqlite.Row]:
    cursor = await database.execute(statement, parameters)
    rows = await cursor.fetchall()
    await cursor.close()
    return list(rows)


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field_name} lacks a timezone. Fix: record a UTC-aware datetime."
        raise ValueError(msg)
    return value.astimezone(UTC)


def _to_iso(value: datetime) -> str:
    return _aware_utc(value, field_name="timestamp").isoformat().replace("+00:00", "Z")


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _json(value: JsonValue | dict[str, JsonValue]) -> str:
    return json.dumps(
        scrub_secrets(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
