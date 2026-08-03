"""Managed-Postgres repository with locked, checksum-verified migrations."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from types import TracebackType
from typing import Any, LiteralString, Self, cast

import psycopg
from psycopg import AsyncConnection
from psycopg import sql as pg_sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue

from voicey.errors import VoiceyError
from voicey.obs.latency import LatencySample
from voicey.obs.logging import scrub_secrets
from voicey.obs.records import (
    CallRecord,
    NewCall,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
)
from voicey.results.events import build_event_body
from voicey.storage.models import (
    CallLease,
    DeliveryClaim,
    DeliveryRecord,
    PersistedEvent,
    ProviderCallState,
    PurgeItem,
    RecordingReady,
    RecordingSnapshot,
    ResultDeliveryConfig,
    ResultSnapshot,
    TerminalRequest,
)
from voicey.storage.sqlite import MAX_DELIVERY_ATTEMPTS, RETRY_DELAYS_SECONDS

_MIGRATION_LOCK = 0x564F4943454B4954


class PostgresMigrator:
    """Apply append-only SQL migrations under a session advisory lock."""

    def __init__(
        self,
        pool: AsyncConnectionPool[AsyncConnection[DictRow]],
    ) -> None:
        self._pool = pool

    async def migrate(self) -> tuple[int, ...]:
        migrations = _migration_sources()
        async with self._pool.connection() as connection:
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (_MIGRATION_LOCK,),
                    )
                    await connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS voicey_schema_migrations (
                            version INTEGER PRIMARY KEY,
                            name TEXT NOT NULL UNIQUE,
                            checksum TEXT NOT NULL,
                            applied_at TIMESTAMPTZ NOT NULL
                        )
                        """
                    )
                    cursor = await connection.execute(
                        "SELECT version, name, checksum FROM voicey_schema_migrations"
                    )
                    rows = list(await cursor.fetchall())
                    applied = {
                        int(row["version"]): (str(row["name"]), str(row["checksum"]))
                        for row in rows
                    }
                    known_versions = {version for version, _, _, _ in migrations}
                    if not set(applied) <= known_versions:
                        raise VoiceyError(
                            "VY-OBS-004",
                            detail="Postgres schema contains an unknown newer migration.",
                        )
                    for version, name, checksum, statement in migrations:
                        existing = applied.get(version)
                        if existing is not None:
                            if existing != (name, checksum):
                                raise VoiceyError(
                                    "VY-OBS-004",
                                    detail=f"Postgres migration {version} checksum changed.",
                                )
                            continue
                        await connection.execute(_query(statement), prepare=False)
                        await connection.execute(
                            """
                                INSERT INTO voicey_schema_migrations(
                                    version, name, checksum, applied_at
                                ) VALUES (%s, %s, %s, %s)
                                """,
                            (version, name, checksum, datetime.now(UTC)),
                        )
                return tuple(version for version, _, _, _ in migrations)
            except VoiceyError:
                raise
            except psycopg.Error as exc:
                raise VoiceyError(
                    "VY-RES-008",
                    detail="Postgres migration or schema validation failed.",
                ) from exc

    async def validate(self) -> tuple[int, ...]:
        migrations = _migration_sources()
        async with self._pool.connection() as connection:
            try:
                cursor = await connection.execute(
                    """
                    SELECT version, name, checksum
                    FROM voicey_schema_migrations ORDER BY version
                    """
                )
                rows = list(await cursor.fetchall())
            except psycopg.Error as exc:
                raise VoiceyError(
                    "VY-OBS-004",
                    detail="Postgres schema migration table is unavailable.",
                ) from exc
        expected = [(version, name, checksum) for version, name, checksum, _ in migrations]
        actual = [(int(row["version"]), str(row["name"]), str(row["checksum"])) for row in rows]
        if actual != expected:
            raise VoiceyError(
                "VY-OBS-004",
                detail="Postgres schema is missing, newer, or checksum-incompatible.",
            )
        return tuple(version for version, _, _ in actual)


class PostgresRepository:
    """Pooled multi-replica repository matching the SQLite lifecycle contract."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout_s: float = 10,
    ) -> None:
        if not dsn or min_size < 0 or max_size < 1 or min_size > max_size or timeout_s <= 0:
            raise VoiceyError("VY-RES-008", detail="Postgres pool settings are invalid.")
        self._pool = AsyncConnectionPool(
            dsn,
            connection_class=AsyncConnection[DictRow],
            kwargs={"row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_s,
            open=False,
            name="voicey-storage",
        )
        self._migrator = PostgresMigrator(self._pool)
        self._open = False

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
        if self._open:
            return self
        try:
            await self._pool.open(wait=True)
            await self._migrator.migrate()
            await self._migrator.validate()
        except VoiceyError:
            await self._pool.close()
            raise
        except (psycopg.Error, TimeoutError) as exc:
            await self._pool.close()
            raise VoiceyError(
                "VY-RES-008",
                detail="Postgres pool could not become ready.",
            ) from exc
        self._open = True
        return self

    async def close(self) -> None:
        if not self._open:
            return
        self._open = False
        await self._pool.close()

    async def ready(self) -> bool:
        await self._migrator.validate()
        async with self._pool.connection() as connection:
            cursor = await connection.execute("SELECT 1 AS ready")
            row = await cursor.fetchone()
        return row is not None and int(row["ready"]) == 1

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        recording_id = f"rec_{uuid.uuid4().hex}" if delivery.recording_enabled else None
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(
                    """
                        INSERT INTO calls (
                            call_id, agent_name, runtime, channel, direction, provider,
                            provider_call_id, from_number, to_number, config_hash,
                            status, webhook_status, started_at, updated_at,
                            owner_id, generation, lease_expires_at, delivery_endpoint,
                            include_json, redact_json, purge_after_days, recording_id
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'active', 'not_ready', %s, %s, %s, 1, %s, %s,
                            %s, %s, %s, %s
                        )
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
                        call.started_at,
                        current,
                        owner_id,
                        expires_at,
                        delivery.endpoint,
                        Jsonb(list(delivery.include)),
                        Jsonb(list(delivery.redact)),
                        delivery.purge_after_days,
                        recording_id,
                    ),
                )
                if recording_id is not None:
                    await connection.execute(
                        """
                            INSERT INTO recordings(
                                recording_id, call_id, status, created_at
                            ) VALUES (%s, %s, 'pending', %s)
                            """,
                        (recording_id, call.call_id, current),
                    )
        except psycopg.Error as exc:
            raise VoiceyError("VY-RES-008", detail="Postgres begin_call failed.") from exc
        return CallLease(
            call_id=call.call_id,
            owner_id=owner_id,
            generation=1,
            expires_at=expires_at,
        )

    async def renew_lease(
        self,
        lease: CallLease,
        *,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        changed = await self._execute_count(
            """
            UPDATE calls SET lease_expires_at = %s, updated_at = %s
            WHERE call_id = %s AND status = 'active'
              AND owner_id = %s AND generation = %s
            """,
            (expires_at, current, lease.call_id, lease.owner_id, lease.generation),
        )
        _require_fenced(changed, lease.call_id)
        return lease.model_copy(update={"expires_at": expires_at})

    async def handoff_call(
        self,
        call_id: str,
        *,
        expected_owner_id: str,
        owner_id: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        row = await self._fetch_one(
            """
            UPDATE calls
            SET owner_id = %s, generation = generation + 1,
                lease_expires_at = %s, updated_at = %s
            WHERE call_id = %s AND status = 'active' AND owner_id = %s
            RETURNING generation
            """,
            (owner_id, expires_at, current, call_id, expected_owner_id),
        )
        if row is None:
            raise VoiceyError("VY-RES-006", detail=call_id)
        return CallLease(
            call_id=call_id,
            owner_id=owner_id,
            generation=int(row["generation"]),
            expires_at=expires_at,
        )

    async def takeover_expired_call(
        self,
        call_id: str,
        *,
        owner_id: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        row = await self._fetch_one(
            """
            UPDATE calls
            SET owner_id = %s, generation = generation + 1,
                lease_expires_at = %s, updated_at = %s
            WHERE call_id = %s AND status = 'active'
              AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
            RETURNING generation
            """,
            (owner_id, expires_at, current, call_id, current),
        )
        if row is None:
            raise VoiceyError("VY-RES-006", detail=call_id)
        return CallLease(
            call_id=call_id,
            owner_id=owner_id,
            generation=int(row["generation"]),
            expires_at=expires_at,
        )

    async def flush_results(self, lease: CallLease, snapshot: ResultSnapshot) -> None:
        safe_data = cast("dict[str, JsonValue]", scrub_secrets(snapshot.data))
        changed = await self._execute_count(
            """
            UPDATE calls
            SET outcome = %s, results_json = %s, interruptions = %s, updated_at = %s
            WHERE call_id = %s AND status = 'active'
              AND owner_id = %s AND generation = %s
            """,
            (
                snapshot.outcome,
                Jsonb(safe_data),
                snapshot.interruptions,
                datetime.now(UTC),
                lease.call_id,
                lease.owner_id,
                lease.generation,
            ),
        )
        _require_fenced(changed, lease.call_id)

    async def update_provider_state(self, lease: CallLease, state: str) -> None:
        changed = await self._execute_count(
            """
            UPDATE calls SET last_provider_state = %s, updated_at = %s
            WHERE call_id = %s AND status = 'active'
              AND owner_id = %s AND generation = %s
            """,
            (
                str(scrub_secrets(state)),
                datetime.now(UTC),
                lease.call_id,
                lease.owner_id,
                lease.generation,
            ),
        )
        _require_fenced(changed, lease.call_id)

    async def terminalize(
        self,
        lease: CallLease,
        request: TerminalRequest,
    ) -> PersistedEvent:
        try:
            async with self._pool.connection() as connection, connection.transaction():
                row = await _connection_fetch_one(
                    connection,
                    "SELECT * FROM calls WHERE call_id = %s FOR UPDATE",
                    (lease.call_id,),
                )
                if row is None:
                    raise VoiceyError("VY-OBS-003", detail=lease.call_id)
                _assert_owner(row, lease)
                if row["status"] != "active":
                    existing = await _connection_fetch_one(
                        connection,
                        """
                            SELECT * FROM call_events
                            WHERE call_id = %s AND is_terminal
                            """,
                        (lease.call_id,),
                    )
                    if existing is None:
                        raise VoiceyError("VY-RES-007", detail=lease.call_id)
                    return _event_from_row(existing)
                call = await self._materialize(connection, row)
                delivery = _delivery_config(row)
                event_id = f"evt_{uuid.uuid4().hex}"
                recording_id = _optional_text(row["recording_id"])
                body = build_event_body(
                    event_id=event_id,
                    event_type=request.event_type,
                    call=call,
                    ended_at=request.ended_at,
                    ended_reason=request.ended_reason,
                    outcome=_optional_text(row["outcome"]),
                    data=cast("dict[str, JsonValue]", row["results_json"]),
                    interruptions=int(row["interruptions"]),
                    delivery=delivery,
                    recording=None,
                    recording_id=recording_id,
                )
                status = "completed" if request.event_type == "call.completed" else "failed"
                cursor = await connection.execute(
                    """
                        UPDATE calls
                        SET status = %s, webhook_status = 'pending', ended_at = %s,
                            terminal_reason = %s, last_provider_state = %s,
                            updated_at = %s, lease_expires_at = NULL
                        WHERE call_id = %s AND status = 'active'
                          AND owner_id = %s AND generation = %s
                        """,
                    (
                        status,
                        request.ended_at,
                        request.ended_reason,
                        request.provider_state,
                        request.ended_at,
                        lease.call_id,
                        lease.owner_id,
                        lease.generation,
                    ),
                )
                _require_fenced(cursor.rowcount, lease.call_id)
                await connection.execute(
                    """
                        INSERT INTO call_events(
                            event_id, call_id, event_type, is_terminal, body, created_at
                        ) VALUES (%s, %s, %s, TRUE, %s, %s)
                        """,
                    (
                        event_id,
                        lease.call_id,
                        request.event_type,
                        body,
                        request.ended_at,
                    ),
                )
                await connection.execute(
                    """
                        INSERT INTO deliveries(
                            event_id, endpoint, status, next_attempt_at
                        ) VALUES (%s, %s, 'pending', %s)
                        """,
                    (event_id, delivery.endpoint, request.ended_at),
                )
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError(
                "VY-RES-008",
                detail="Postgres terminal transaction failed.",
            ) from exc
        return PersistedEvent(
            event_id=event_id,
            call_id=lease.call_id,
            event_type=request.event_type,
            body=body,
            created_at=request.ended_at,
        )

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None:
        await self._observation_write(
            """
            INSERT INTO call_timeline(call_id, event_type, occurred_at, details_json)
            VALUES (%s, %s, %s, %s)
            """,
            (call_id, event.event_type, event.occurred_at, Jsonb(event.details)),
            call_id=call_id,
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
        await self._relay_observation_write(
            """
            INSERT INTO call_timeline(
                call_id, event_type, occurred_at, details_json, relay_operation_id
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                call_id,
                event.event_type,
                event.occurred_at,
                Jsonb(event.details),
                operation_id,
            ),
            table="call_timeline",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None:
        await self._observation_write(
            """
            INSERT INTO call_transcript(call_id, turn_id, role, text, t_ms)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                call_id,
                turn.turn_id,
                turn.role,
                str(scrub_secrets(turn.text)),
                turn.t_ms,
            ),
            call_id=call_id,
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
        await self._relay_observation_write(
            """
            INSERT INTO call_transcript(
                call_id, turn_id, role, text, t_ms, relay_operation_id
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
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
        await self._observation_write(
            """
            INSERT INTO call_tools(
                call_id, invocation_id, tool_name, arguments_json, result_json,
                duration_ms, status, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                call_id,
                observation.invocation_id,
                observation.tool_name,
                Jsonb(observation.arguments),
                None if observation.result is None else Jsonb(observation.result),
                observation.duration_ms,
                observation.status,
                observation.occurred_at,
            ),
            call_id=call_id,
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
        await self._relay_observation_write(
            """
            INSERT INTO call_tools(
                call_id, invocation_id, tool_name, arguments_json, result_json,
                duration_ms, status, occurred_at, relay_operation_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                call_id,
                observation.invocation_id,
                observation.tool_name,
                Jsonb(observation.arguments),
                None if observation.result is None else Jsonb(observation.result),
                observation.duration_ms,
                observation.status,
                observation.occurred_at,
                operation_id,
            ),
            table="call_tools",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def record_latency(self, call_id: str, sample: LatencySample) -> None:
        await self._observation_write(
            """
            INSERT INTO call_latency(
                call_id, turn_id, turn_index, metric, duration_ms, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                call_id,
                sample.turn_id,
                sample.turn_index,
                sample.metric,
                sample.duration_ms,
                sample.observed_at,
            ),
            call_id=call_id,
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
        await self._relay_observation_write(
            """
            INSERT INTO call_latency(
                call_id, turn_id, turn_index, metric, duration_ms, observed_at,
                relay_operation_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                call_id,
                sample.turn_id,
                sample.turn_index,
                sample.metric,
                sample.duration_ms,
                sample.observed_at,
                operation_id,
            ),
            table="call_latency",
            call_id=call_id,
            operation_id=operation_id,
            owner_id=owner_id,
            generation=generation,
        )

    async def get_call(self, call_id: str) -> CallRecord:
        async with self._pool.connection() as connection:
            row = await _connection_fetch_one(
                connection,
                "SELECT * FROM calls WHERE call_id = %s",
                (call_id,),
            )
            if row is None:
                raise VoiceyError("VY-OBS-003", detail=call_id)
            return await self._materialize(connection, row)

    async def list_calls(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[CallRecord, ...]:
        if not 1 <= limit <= 1000:
            raise VoiceyError("VY-OBS-005", detail=f"limit={limit}")
        async with self._pool.connection() as connection:
            if status is None:
                cursor = await connection.execute(
                    "SELECT * FROM calls ORDER BY started_at DESC LIMIT %s",
                    (limit,),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT * FROM calls
                    WHERE status = %s ORDER BY started_at DESC LIMIT %s
                    """,
                    (status, limit),
                )
            rows = list(await cursor.fetchall())
            return tuple([await self._materialize(connection, row) for row in rows])

    async def mark_recording_ready(
        self,
        update: RecordingReady,
        *,
        relay_lease: CallLease | None = None,
    ) -> PersistedEvent:
        try:
            async with self._pool.connection() as connection, connection.transaction():
                row = await _connection_fetch_one(
                    connection,
                    """
                        SELECT calls.* FROM calls JOIN recordings USING(call_id)
                        WHERE recordings.recording_id = %s FOR UPDATE OF calls
                        """,
                    (update.recording_id,),
                )
                if row is None:
                    raise VoiceyError("VY-RES-010", detail=update.recording_id)
                if relay_lease is not None:
                    _assert_owner(row, relay_lease, code="VY-REL-004")
                if row["status"] == "active":
                    raise VoiceyError(
                        "VY-RES-010",
                        detail="recording-ready arrived before terminal persistence.",
                    )
                existing = await _connection_fetch_one(
                    connection,
                    """
                        SELECT * FROM call_events
                        WHERE call_id = %s AND event_type = 'call.recording.ready'
                        """,
                    (str(row["call_id"]),),
                )
                if existing is not None:
                    return _event_from_row(existing)
                call = await self._materialize(connection, row)
                delivery = _delivery_config(row)
                event_id = f"evt_{uuid.uuid4().hex}"
                ended_at = _datetime(row["ended_at"])
                body = build_event_body(
                    event_id=event_id,
                    event_type="call.recording.ready",
                    call=call,
                    ended_at=ended_at,
                    ended_reason=str(row["terminal_reason"]),
                    outcome=_optional_text(row["outcome"]),
                    data=cast("dict[str, JsonValue]", row["results_json"]),
                    interruptions=int(row["interruptions"]),
                    delivery=delivery,
                    recording=update,
                    recording_id=update.recording_id,
                )
                await connection.execute(
                    """
                        UPDATE recordings
                        SET status = 'ready', access_url = %s, storage_key = %s,
                            ready_at = %s
                        WHERE recording_id = %s
                        """,
                    (
                        update.access_url,
                        update.storage_key,
                        update.ready_at,
                        update.recording_id,
                    ),
                )
                await connection.execute(
                    """
                        INSERT INTO call_events(
                            event_id, call_id, event_type, is_terminal, body, created_at
                        ) VALUES (%s, %s, 'call.recording.ready', FALSE, %s, %s)
                        """,
                    (event_id, str(row["call_id"]), body, update.ready_at),
                )
                await connection.execute(
                    """
                        INSERT INTO deliveries(
                            event_id, endpoint, status, next_attempt_at
                        ) VALUES (%s, %s, 'pending', %s)
                        """,
                    (event_id, delivery.endpoint, update.ready_at),
                )
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError(
                "VY-RES-008",
                detail="Postgres recording-ready transaction failed.",
            ) from exc
        return PersistedEvent(
            event_id=event_id,
            call_id=str(row["call_id"]),
            event_type="call.recording.ready",
            body=body,
            created_at=update.ready_at,
        )

    async def mark_recording_failed(self, call_id: str) -> None:
        row = await self._fetch_one(
            """
            UPDATE recordings SET status = 'failed'
            WHERE call_id = %s AND status = 'pending'
            RETURNING status
            """,
            (call_id,),
        )
        if row is None:
            existing = await self._fetch_one(
                "SELECT status FROM recordings WHERE call_id = %s",
                (call_id,),
            )
            if existing is None:
                raise VoiceyError("VY-RES-010", detail=call_id)

    async def mark_recording_failed_fenced(self, lease: CallLease) -> None:
        row = await self._fetch_one(
            """
            UPDATE recordings SET status = 'failed'
            FROM calls
            WHERE recordings.call_id = %s AND recordings.status = 'pending'
              AND calls.call_id = recordings.call_id
              AND calls.owner_id = %s AND calls.generation = %s
            RETURNING recordings.status
            """,
            (lease.call_id, lease.owner_id, lease.generation),
        )
        if row is None:
            await self.assert_relay_fence(lease)
            existing = await self.get_recording_for_call(lease.call_id)
            if existing is None:
                raise VoiceyError("VY-RES-010", detail=lease.call_id)

    async def get_event(self, event_id: str) -> PersistedEvent:
        row = await self._fetch_one(
            "SELECT * FROM call_events WHERE event_id = %s",
            (event_id,),
        )
        if row is None:
            raise VoiceyError("VY-RES-009", detail=event_id)
        return _event_from_row(row)

    async def get_terminal_event_for_call(self, call_id: str) -> PersistedEvent:
        row = await self._fetch_one(
            "SELECT * FROM call_events WHERE call_id = %s AND is_terminal",
            (call_id,),
        )
        if row is None:
            raise VoiceyError("VY-RES-009", detail=call_id)
        return _event_from_row(row)

    async def get_result_snapshot(self, call_id: str) -> ResultSnapshot:
        row = await self._fetch_one(
            "SELECT outcome, results_json, interruptions FROM calls WHERE call_id = %s",
            (call_id,),
        )
        if row is None:
            raise VoiceyError("VY-OBS-003", detail=call_id)
        return ResultSnapshot.model_validate(
            {
                "outcome": row["outcome"],
                "data": row["results_json"],
                "interruptions": row["interruptions"],
            }
        )

    async def get_recording_for_call(self, call_id: str) -> RecordingSnapshot | None:
        row = await self._fetch_one(
            "SELECT * FROM recordings WHERE call_id = %s",
            (call_id,),
        )
        return None if row is None else _recording_snapshot(row)

    async def get_recording(self, recording_id: str) -> RecordingSnapshot:
        row = await self._fetch_one(
            "SELECT * FROM recordings WHERE recording_id = %s",
            (recording_id,),
        )
        if row is None:
            raise VoiceyError("VY-RES-010", detail=recording_id)
        return _recording_snapshot(row)

    async def claim_deliveries(
        self,
        *,
        owner_id: str,
        limit: int,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[DeliveryClaim, ...]:
        if not 1 <= limit <= 100:
            raise VoiceyError("VY-OBS-005", detail=f"delivery limit={limit}")
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        try:
            async with self._pool.connection() as connection, connection.transaction():
                cursor = await connection.execute(
                    """
                        WITH due AS (
                            SELECT event_id, endpoint FROM deliveries
                            WHERE (
                                status = 'pending' AND next_attempt_at <= %s
                            ) OR (
                                status = 'delivering' AND lease_expires_at <= %s
                            )
                            ORDER BY next_attempt_at, event_id
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        )
                        UPDATE deliveries
                        SET status = 'delivering',
                            attempt_count = deliveries.attempt_count + 1,
                            lease_owner = %s,
                            lease_expires_at = %s
                        FROM due
                        WHERE deliveries.event_id = due.event_id
                          AND deliveries.endpoint = due.endpoint
                        RETURNING deliveries.event_id, deliveries.endpoint,
                                  deliveries.attempt_count
                        """,
                    (current, current, limit, owner_id, expires_at),
                )
                rows = list(await cursor.fetchall())
                claims: list[DeliveryClaim] = []
                for row in rows:
                    event_row = await _connection_fetch_one(
                        connection,
                        "SELECT * FROM call_events WHERE event_id = %s",
                        (str(row["event_id"]),),
                    )
                    if event_row is None:
                        raise VoiceyError(
                            "VY-RES-007",
                            detail=str(row["event_id"]),
                        )
                    event = _event_from_row(event_row)
                    claims.append(
                        DeliveryClaim(
                            event_id=event.event_id,
                            call_id=event.call_id,
                            endpoint=str(row["endpoint"]),
                            body=event.body,
                            attempt_count=int(row["attempt_count"]),
                            lease_owner=owner_id,
                            lease_expires_at=expires_at,
                        )
                    )
                return tuple(claims)
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError("VY-RES-008", detail="Postgres delivery claim failed.") from exc

    async def acknowledge_delivery(
        self,
        claim: DeliveryClaim,
        *,
        now: datetime | None = None,
    ) -> None:
        current = _utc(now)
        try:
            async with self._pool.connection() as connection, connection.transaction():
                cursor = await connection.execute(
                    """
                        UPDATE deliveries
                        SET status = 'delivered', delivered_at = %s,
                            lease_owner = NULL, lease_expires_at = NULL,
                            last_error = NULL
                        WHERE event_id = %s AND endpoint = %s
                          AND status = 'delivering' AND lease_owner = %s
                          AND attempt_count = %s
                        """,
                    (
                        current,
                        claim.event_id,
                        claim.endpoint,
                        claim.lease_owner,
                        claim.attempt_count,
                    ),
                )
                if cursor.rowcount != 1:
                    raise VoiceyError("VY-RES-008", detail="delivery claim was lost.")
                await connection.execute(
                    """
                        UPDATE calls SET webhook_status = 'delivered'
                        WHERE call_id = %s AND NOT EXISTS (
                            SELECT 1 FROM deliveries
                            JOIN call_events USING(event_id)
                            WHERE call_events.call_id = %s
                              AND deliveries.status != 'delivered'
                        )
                        """,
                    (claim.call_id, claim.call_id),
                )
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError(
                "VY-RES-008",
                detail="Postgres delivery acknowledgement failed.",
            ) from exc

    async def fail_delivery(
        self,
        claim: DeliveryClaim,
        *,
        error: str,
        jitter: Callable[[float], float],
        now: datetime | None = None,
    ) -> DeliveryRecord:
        current = _utc(now)
        dead_lettered = claim.attempt_count >= MAX_DELIVERY_ATTEMPTS
        if dead_lettered:
            status = "dead_lettered"
            next_attempt_at = current
        else:
            base_delay = RETRY_DELAYS_SECONDS[claim.attempt_count]
            delay = max(0.0, jitter(float(base_delay)))
            if not 0.8 * base_delay <= delay <= 1.2 * base_delay:
                raise VoiceyError(
                    "VY-RES-008",
                    detail="retry jitter must remain within ±20%.",
                )
            status = "pending"
            next_attempt_at = current + timedelta(seconds=delay)
        try:
            async with self._pool.connection() as connection, connection.transaction():
                cursor = await connection.execute(
                    """
                        UPDATE deliveries
                        SET status = %s, next_attempt_at = %s, last_error = %s,
                            lease_owner = NULL, lease_expires_at = NULL
                        WHERE event_id = %s AND endpoint = %s
                          AND status = 'delivering' AND lease_owner = %s
                          AND attempt_count = %s
                        """,
                    (
                        status,
                        next_attempt_at,
                        str(scrub_secrets(error))[:1000],
                        claim.event_id,
                        claim.endpoint,
                        claim.lease_owner,
                        claim.attempt_count,
                    ),
                )
                if cursor.rowcount != 1:
                    raise VoiceyError("VY-RES-008", detail="delivery claim was lost.")
                if dead_lettered:
                    await connection.execute(
                        """
                            UPDATE calls SET webhook_status = 'dead_lettered'
                            WHERE call_id = %s
                            """,
                        (claim.call_id,),
                    )
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError("VY-RES-008", detail="Postgres delivery failure failed.") from exc
        records = await self._delivery_records(
            """
            WHERE deliveries.event_id = %s AND deliveries.endpoint = %s
            """,
            (claim.event_id, claim.endpoint),
        )
        return records[0]

    async def redeliver(
        self,
        event_id: str,
        *,
        now: datetime | None = None,
    ) -> DeliveryRecord:
        current = _utc(now)
        row = await self._fetch_one(
            """
            UPDATE deliveries
            SET status = 'pending', attempt_count = 0, next_attempt_at = %s,
                lease_owner = NULL, lease_expires_at = NULL,
                last_error = NULL, delivered_at = NULL
            WHERE event_id = %s
            RETURNING event_id
            """,
            (current, event_id),
        )
        if row is None:
            raise VoiceyError("VY-RES-009", detail=event_id)
        return (
            await self._delivery_records(
                "WHERE deliveries.event_id = %s",
                (event_id,),
            )
        )[0]

    async def list_deliveries(
        self,
        *,
        undelivered_only: bool = False,
    ) -> tuple[DeliveryRecord, ...]:
        where = "WHERE deliveries.status != 'delivered'" if undelivered_only else ""
        return await self._delivery_records(where, ())

    async def dlq_depth(self) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS depth FROM deliveries WHERE status = 'dead_lettered'",
            (),
        )
        return 0 if row is None else int(row["depth"])

    async def list_stale_calls(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        current = _utc(now)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT call_id FROM calls
                WHERE status = 'active'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
                ORDER BY started_at
                """,
                (current,),
            )
            rows = list(await cursor.fetchall())
        return tuple(str(row["call_id"]) for row in rows)

    async def get_provider_state(self, call_id: str) -> str | None:
        """Return the latest fenced or authenticated provider observation."""
        row = await self._fetch_one(
            "SELECT last_provider_state FROM calls WHERE call_id = %s",
            (call_id,),
        )
        if row is None:
            raise VoiceyError("VY-OBS-003", detail=call_id)
        value = row["last_provider_state"]
        return None if value is None else str(value)

    async def record_provider_observation(
        self,
        provider_call_id: str,
        state: ProviderCallState,
    ) -> None:
        """Persist carrier-authenticated truth without granting lifecycle ownership."""
        if not provider_call_id or state not in {"active", "completed", "failed", "unknown"}:
            raise VoiceyError("VY-RES-008", detail="provider observation is invalid.")
        try:
            async with self._pool.connection() as connection, connection.transaction():
                cursor = await connection.execute(
                    """
                    UPDATE calls
                    SET last_provider_state = %s, updated_at = %s
                    WHERE status = 'active'
                      AND (call_id = %s OR provider_call_id = %s)
                    RETURNING status
                    """,
                    (
                        state,
                        datetime.now(UTC),
                        provider_call_id,
                        provider_call_id,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    existing = await _connection_fetch_one(
                        connection,
                        """
                        SELECT status FROM calls
                        WHERE call_id = %s OR provider_call_id = %s
                        """,
                        (provider_call_id, provider_call_id),
                    )
                    if existing is None:
                        raise VoiceyError("VY-OBS-003", detail=provider_call_id)
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError(
                "VY-RES-008",
                detail="provider observation could not be persisted.",
            ) from exc

    async def current_relay_lease(self, call_id: str) -> CallLease:
        row = await self._fetch_one(
            """
            SELECT call_id, owner_id, generation, lease_expires_at, updated_at
            FROM calls WHERE call_id = %s
            """,
            (call_id,),
        )
        if row is None:
            raise VoiceyError("VY-OBS-003", detail=call_id)
        owner_id = row["owner_id"]
        if owner_id is None or int(row["generation"]) < 1:
            raise VoiceyError("VY-REL-004", detail=f"{call_id} has no current owner.")
        return CallLease(
            call_id=call_id,
            owner_id=str(owner_id),
            generation=int(row["generation"]),
            expires_at=_datetime(row["lease_expires_at"] or row["updated_at"]),
        )

    async def assert_relay_fence(self, lease: CallLease) -> None:
        current = await self.current_relay_lease(lease.call_id)
        if current.owner_id != lease.owner_id or current.generation != lease.generation:
            raise VoiceyError("VY-REL-004", detail=f"{lease.call_id} generation is stale.")

    async def register_backup(
        self,
        *,
        backup_id: str,
        storage_key: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> None:
        await self._execute_count(
            """
            INSERT INTO backups(backup_id, storage_key, created_at, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (backup_id, storage_key, _utc(now), _utc(expires_at)),
        )

    async def queue_retention(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[PurgeItem, ...]:
        current = _utc(now)
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(
                    """
                        INSERT INTO purge_queue(storage_key, artifact_kind, queued_at)
                        SELECT recordings.storage_key, 'recording', %s
                        FROM recordings JOIN calls USING(call_id)
                        WHERE recordings.storage_key IS NOT NULL
                          AND calls.status != 'active'
                          AND calls.ended_at
                              + make_interval(days => calls.purge_after_days) <= %s
                        ON CONFLICT(storage_key) DO NOTHING
                        """,
                    (current, current),
                )
                await connection.execute(
                    """
                        INSERT INTO purge_queue(storage_key, artifact_kind, queued_at)
                        SELECT storage_key, 'backup', %s
                        FROM backups WHERE expires_at <= %s
                        ON CONFLICT(storage_key) DO NOTHING
                        """,
                    (current, current),
                )
                await connection.execute(
                    """
                        DELETE FROM calls
                        WHERE status != 'active'
                          AND ended_at + make_interval(days => purge_after_days) <= %s
                        """,
                    (current,),
                )
                await connection.execute(
                    "DELETE FROM backups WHERE expires_at <= %s",
                    (current,),
                )
                cursor = await connection.execute(
                    """
                        SELECT storage_key, artifact_kind
                        FROM purge_queue ORDER BY storage_key
                        """
                )
                rows = list(await cursor.fetchall())
        except psycopg.Error as exc:
            raise VoiceyError("VY-RES-008", detail="Postgres retention failed.") from exc
        return tuple(
            PurgeItem.model_validate(
                {
                    "storage_key": row["storage_key"],
                    "artifact_kind": row["artifact_kind"],
                }
            )
            for row in rows
        )

    async def acknowledge_purge(self, storage_key: str) -> None:
        await self._execute_count(
            "DELETE FROM purge_queue WHERE storage_key = %s",
            (storage_key,),
        )

    async def _fetch_one(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> DictRow | None:
        try:
            async with self._pool.connection() as connection:
                return await _connection_fetch_one(connection, statement, parameters)
        except psycopg.Error as exc:
            raise VoiceyError("VY-RES-008", detail="Postgres query failed.") from exc

    async def _execute_count(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> int:
        try:
            async with self._pool.connection() as connection:
                cursor = await connection.execute(_query(statement), parameters)
                return cursor.rowcount
        except psycopg.Error as exc:
            raise VoiceyError("VY-RES-008", detail="Postgres mutation failed.") from exc

    async def _observation_write(
        self,
        statement: str,
        parameters: tuple[object, ...],
        *,
        call_id: str,
    ) -> None:
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(_query(statement), parameters)
                await connection.execute(
                    "UPDATE calls SET updated_at = %s WHERE call_id = %s",
                    (datetime.now(UTC), call_id),
                )
        except psycopg.Error as exc:
            raise VoiceyError("VY-OBS-002", detail="Postgres observation failed.") from exc

    async def _relay_observation_write(
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
        try:
            async with self._pool.connection() as connection, connection.transaction():
                row = await _connection_fetch_one(
                    connection,
                    """
                    SELECT owner_id, generation FROM calls
                    WHERE call_id = %s FOR UPDATE
                    """,
                    (call_id,),
                )
                if (
                    row is None
                    or row["owner_id"] != owner_id
                    or int(row["generation"]) != generation
                ):
                    raise VoiceyError(
                        "VY-REL-004",
                        detail=f"{call_id} generation is stale.",
                    )
                await connection.execute(_query(statement), parameters)
                existing = await _connection_fetch_one(
                    connection,
                    f"SELECT call_id FROM {table} WHERE relay_operation_id = %s",
                    (operation_id,),
                )
                if existing is None or str(existing["call_id"]) != call_id:
                    raise VoiceyError(
                        "VY-REL-005",
                        detail="relay operation id collides with another observation.",
                    )
                await connection.execute(
                    "UPDATE calls SET updated_at = %s WHERE call_id = %s",
                    (datetime.now(UTC), call_id),
                )
        except VoiceyError:
            raise
        except psycopg.Error as exc:
            raise VoiceyError(
                "VY-REL-006",
                detail="Postgres relay observation failed.",
            ) from exc

    async def _materialize(
        self,
        connection: AsyncConnection[DictRow],
        row: Mapping[str, Any],
    ) -> CallRecord:
        call_id = str(row["call_id"])
        timeline = await _connection_fetch_all(
            connection,
            "SELECT * FROM call_timeline WHERE call_id = %s ORDER BY sequence",
            (call_id,),
        )
        transcript = await _connection_fetch_all(
            connection,
            "SELECT * FROM call_transcript WHERE call_id = %s ORDER BY sequence",
            (call_id,),
        )
        tools = await _connection_fetch_all(
            connection,
            "SELECT * FROM call_tools WHERE call_id = %s ORDER BY sequence",
            (call_id,),
        )
        latency = await _connection_fetch_all(
            connection,
            "SELECT * FROM call_latency WHERE call_id = %s ORDER BY sequence",
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
                "started_at": row["started_at"],
                "updated_at": row["updated_at"],
                "ended_at": row["ended_at"],
                "terminal_reason": row["terminal_reason"],
                "timeline": [
                    {
                        "event_type": item["event_type"],
                        "occurred_at": item["occurred_at"],
                        "details": item["details_json"],
                    }
                    for item in timeline
                ],
                "transcript": [
                    {
                        "turn_id": item["turn_id"],
                        "role": item["role"],
                        "text": item["text"],
                        "t_ms": item["t_ms"],
                    }
                    for item in transcript
                ],
                "tool_calls": [
                    {
                        "invocation_id": item["invocation_id"],
                        "tool_name": item["tool_name"],
                        "arguments": item["arguments_json"],
                        "result": item["result_json"],
                        "duration_ms": item["duration_ms"],
                        "status": item["status"],
                        "occurred_at": item["occurred_at"],
                    }
                    for item in tools
                ],
                "latency": [
                    {
                        "turn_id": item["turn_id"],
                        "turn_index": item["turn_index"],
                        "metric": item["metric"],
                        "duration_ms": item["duration_ms"],
                        "observed_at": item["observed_at"],
                    }
                    for item in latency
                ],
            }
        )

    async def _delivery_records(
        self,
        where: str,
        parameters: tuple[object, ...],
    ) -> tuple[DeliveryRecord, ...]:
        async with self._pool.connection() as connection:
            statement = pg_sql.SQL(
                """
                SELECT deliveries.*, call_events.call_id
                FROM deliveries JOIN call_events USING(event_id)
                {}
                ORDER BY deliveries.next_attempt_at, deliveries.event_id
                """,
            ).format(_query(where))
            cursor = await connection.execute(
                statement,
                parameters,
            )
            rows = list(await cursor.fetchall())
        return tuple(
            DeliveryRecord.model_validate(
                {
                    "event_id": row["event_id"],
                    "call_id": row["call_id"],
                    "endpoint": row["endpoint"],
                    "status": row["status"],
                    "attempt_count": row["attempt_count"],
                    "next_attempt_at": row["next_attempt_at"],
                    "last_error": row["last_error"],
                    "delivered_at": row["delivered_at"],
                }
            )
            for row in rows
        )


async def _connection_fetch_one(
    connection: AsyncConnection[DictRow],
    statement: str,
    parameters: tuple[object, ...],
) -> DictRow | None:
    cursor = await connection.execute(_query(statement), parameters)
    return await cursor.fetchone()


async def _connection_fetch_all(
    connection: AsyncConnection[DictRow],
    statement: str,
    parameters: tuple[object, ...],
) -> list[DictRow]:
    cursor = await connection.execute(_query(statement), parameters)
    return list(await cursor.fetchall())


def _migration_sources() -> tuple[tuple[int, str, str, str], ...]:
    root = files("voicey.storage").joinpath("migrations", "postgres")
    migrations: list[tuple[int, str, str, str]] = []
    for item in sorted(root.iterdir(), key=lambda value: value.name):
        if not item.name.endswith(".sql"):
            continue
        try:
            version = int(item.name.split("_", 1)[0])
            statement = item.read_text(encoding="utf-8")
        except (ValueError, OSError) as exc:
            raise VoiceyError(
                "VY-OBS-004",
                detail="Postgres migration resource is malformed.",
            ) from exc
        checksum = hashlib.sha256(statement.encode()).hexdigest()
        migrations.append((version, item.name, checksum, statement))
    if not migrations or len({item[0] for item in migrations}) != len(migrations):
        raise VoiceyError(
            "VY-OBS-004",
            detail="Postgres migration sequence is empty or duplicated.",
        )
    return tuple(migrations)


def _query(statement: str) -> pg_sql.SQL:
    """Mark only engine-owned SQL fragments as composable Psycopg queries."""
    return pg_sql.SQL(cast("LiteralString", statement))


def _delivery_config(row: Mapping[str, Any]) -> ResultDeliveryConfig:
    return ResultDeliveryConfig.model_validate(
        {
            "endpoint": row["delivery_endpoint"],
            "include": row["include_json"],
            "redact": row["redact_json"],
            "purge_after_days": row["purge_after_days"],
            "recording_enabled": row["recording_id"] is not None,
        }
    )


def _event_from_row(row: Mapping[str, Any]) -> PersistedEvent:
    return PersistedEvent.model_validate(
        {
            "event_id": row["event_id"],
            "call_id": row["call_id"],
            "event_type": row["event_type"],
            "body": bytes(row["body"]),
            "created_at": row["created_at"],
        }
    )


def _recording_snapshot(row: Mapping[str, Any]) -> RecordingSnapshot:
    return RecordingSnapshot.model_validate(
        {
            "recording_id": row["recording_id"],
            "call_id": row["call_id"],
            "status": row["status"],
            "access_url": row["access_url"],
            "storage_key": row["storage_key"],
            "created_at": row["created_at"],
            "ready_at": row["ready_at"],
        }
    )


def _assert_owner(
    row: Mapping[str, Any],
    lease: CallLease,
    *,
    code: str = "VY-RES-006",
) -> None:
    if row["owner_id"] != lease.owner_id or int(row["generation"]) != lease.generation:
        raise VoiceyError(code, detail=lease.call_id)


def _require_fenced(changed: int, call_id: str) -> None:
    if changed != 1:
        raise VoiceyError("VY-RES-006", detail=call_id)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise VoiceyError("VY-RES-008", detail="storage timestamp is timezone-naive.")
    return current.astimezone(UTC)


def _expires(current: datetime, lease_ttl: timedelta) -> datetime:
    if lease_ttl <= timedelta(0):
        raise VoiceyError("VY-RES-008", detail="lease TTL must be positive.")
    return current + lease_ttl


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise VoiceyError("VY-RES-008", detail="Postgres timestamp is invalid.")
    return _utc(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
