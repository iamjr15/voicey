"""SQLite implementation of the fenced lifecycle and durable outbox contract."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import aiosqlite
from pydantic import JsonValue

from voicekit.errors import VoicekitError
from voicekit.obs.logging import scrub_secrets
from voicekit.obs.records import NewCall, SQLiteCallRecordStore
from voicekit.results.events import build_event_body
from voicekit.storage.models import (
    CallLease,
    DeliveryClaim,
    DeliveryRecord,
    PersistedEvent,
    PurgeItem,
    RecordingReady,
    RecordingSnapshot,
    ResultDeliveryConfig,
    ResultSnapshot,
    TerminalRequest,
)

RETRY_DELAYS_SECONDS: tuple[int, ...] = (
    0,
    5,
    5 * 60,
    30 * 60,
    2 * 60 * 60,
    5 * 60 * 60,
    10 * 60 * 60,
    10 * 60 * 60,
)
MAX_DELIVERY_ATTEMPTS = len(RETRY_DELAYS_SECONDS)


class SQLiteRepository(SQLiteCallRecordStore):
    """One-writer SQLite repository with generation fencing and an outbox."""

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        """Atomically create an active row, lease, and pending recording reference."""
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        recording_id = f"rec_{uuid.uuid4().hex}" if delivery.recording_enabled else None
        database = self._connection()
        async with self._write_lock:
            try:
                cursor = await database.execute("BEGIN IMMEDIATE")
                await cursor.close()
                cursor = await database.execute(
                    """
                    INSERT INTO calls (
                        call_id, agent_name, runtime, channel, direction, provider,
                        provider_call_id, from_number, to_number, config_hash,
                        status, webhook_status, started_at, updated_at,
                        owner_id, generation, lease_expires_at, delivery_endpoint,
                        include_json, redact_json, purge_after_days, recording_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'not_ready', ?, ?,
                        ?, 1, ?, ?, ?, ?, ?, ?
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
                        _iso(call.started_at),
                        _iso(current),
                        owner_id,
                        _iso(expires_at),
                        delivery.endpoint,
                        _json(list(delivery.include)),
                        _json(list(delivery.redact)),
                        delivery.purge_after_days,
                        recording_id,
                    ),
                )
                await cursor.close()
                if recording_id is not None:
                    cursor = await database.execute(
                        """
                        INSERT INTO recordings(
                            recording_id, call_id, status, created_at
                        ) VALUES (?, ?, 'pending', ?)
                        """,
                        (recording_id, call.call_id, _iso(current)),
                    )
                    await cursor.close()
                await database.commit()
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoicekitError("VK-RES-008", detail=str(exc)) from exc
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
        """Extend a lease only when owner and generation still match."""
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        database = self._connection()
        async with self._write_lock:
            cursor = await database.execute(
                """
                UPDATE calls
                SET lease_expires_at = ?, updated_at = ?
                WHERE call_id = ? AND status = 'active'
                  AND owner_id = ? AND generation = ?
                """,
                (
                    _iso(expires_at),
                    _iso(current),
                    lease.call_id,
                    lease.owner_id,
                    lease.generation,
                ),
            )
            changed = cursor.rowcount
            await cursor.close()
            await database.commit()
        if changed != 1:
            raise VoicekitError("VK-RES-006", detail=lease.call_id)
        return lease.model_copy(update={"expires_at": expires_at})

    async def takeover_expired_call(
        self,
        call_id: str,
        *,
        owner_id: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        """Atomically increment the generation after the prior lease expires."""
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        database = self._connection()
        async with self._write_lock:
            try:
                cursor = await database.execute(
                    """
                    UPDATE calls
                    SET owner_id = ?, generation = generation + 1,
                        lease_expires_at = ?, updated_at = ?
                    WHERE call_id = ? AND status = 'active'
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    RETURNING generation
                    """,
                    (
                        owner_id,
                        _iso(expires_at),
                        _iso(current),
                        call_id,
                        _iso(current),
                    ),
                )
                row = await cursor.fetchone()
                await cursor.close()
                await database.commit()
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoicekitError("VK-RES-008", detail=str(exc)) from exc
        if row is None:
            raise VoicekitError("VK-RES-006", detail=call_id)
        return CallLease(
            call_id=call_id,
            owner_id=owner_id,
            generation=int(row["generation"]),
            expires_at=expires_at,
        )

    async def flush_results(self, lease: CallLease, snapshot: ResultSnapshot) -> None:
        """Incrementally persist result data under the active fencing token."""
        database = self._connection()
        current = datetime.now(UTC)
        safe_data = cast("dict[str, JsonValue]", scrub_secrets(snapshot.data))
        async with self._write_lock:
            cursor = await database.execute(
                """
                UPDATE calls
                SET outcome = ?, results_json = ?, interruptions = ?, updated_at = ?
                WHERE call_id = ? AND status = 'active'
                  AND owner_id = ? AND generation = ?
                """,
                (
                    snapshot.outcome,
                    _json(safe_data),
                    snapshot.interruptions,
                    _iso(current),
                    lease.call_id,
                    lease.owner_id,
                    lease.generation,
                ),
            )
            changed = cursor.rowcount
            await cursor.close()
            await database.commit()
        if changed != 1:
            raise VoicekitError("VK-RES-006", detail=lease.call_id)

    async def update_provider_state(self, lease: CallLease, state: str) -> None:
        """Persist the last reconciled carrier/runtime state under fencing."""
        database = self._connection()
        async with self._write_lock:
            cursor = await database.execute(
                """
                UPDATE calls
                SET last_provider_state = ?, updated_at = ?
                WHERE call_id = ? AND status = 'active'
                  AND owner_id = ? AND generation = ?
                """,
                (
                    str(scrub_secrets(state)),
                    _iso(datetime.now(UTC)),
                    lease.call_id,
                    lease.owner_id,
                    lease.generation,
                ),
            )
            changed = cursor.rowcount
            await cursor.close()
            await database.commit()
        if changed != 1:
            raise VoicekitError("VK-RES-006", detail=lease.call_id)

    async def terminalize(
        self,
        lease: CallLease,
        request: TerminalRequest,
    ) -> PersistedEvent:
        """CAS active to terminal and insert immutable event+delivery in one txn."""
        database = self._connection()
        async with self._write_lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                row = await _fetch_one(
                    database,
                    "SELECT * FROM calls WHERE call_id = ?",
                    (lease.call_id,),
                )
                if row is None:
                    raise VoicekitError("VK-OBS-003", detail=lease.call_id)
                if row["owner_id"] != lease.owner_id or int(row["generation"]) != lease.generation:
                    raise VoicekitError("VK-RES-006", detail=lease.call_id)
                if row["status"] != "active":
                    existing = await self._terminal_event_in_transaction(lease.call_id)
                    await database.rollback()
                    return existing

                call = await self._materialize(row)
                delivery = _delivery_config(row)
                event_id = f"evt_{uuid.uuid4().hex}"
                recording_id = None if row["recording_id"] is None else str(row["recording_id"])
                body = build_event_body(
                    event_id=event_id,
                    event_type=request.event_type,
                    call=call,
                    ended_at=request.ended_at,
                    ended_reason=request.ended_reason,
                    outcome=row["outcome"],
                    data=cast(
                        "dict[str, JsonValue]",
                        json.loads(str(row["results_json"])),
                    ),
                    interruptions=int(row["interruptions"]),
                    delivery=delivery,
                    recording=None,
                    recording_id=recording_id,
                )
                status = "completed" if request.event_type == "call.completed" else "failed"
                cursor = await database.execute(
                    """
                    UPDATE calls
                    SET status = ?, webhook_status = 'pending', ended_at = ?,
                        terminal_reason = ?, last_provider_state = ?,
                        updated_at = ?, lease_expires_at = NULL
                    WHERE call_id = ? AND status = 'active'
                      AND owner_id = ? AND generation = ?
                    """,
                    (
                        status,
                        _iso(request.ended_at),
                        request.ended_reason,
                        request.provider_state,
                        _iso(request.ended_at),
                        lease.call_id,
                        lease.owner_id,
                        lease.generation,
                    ),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    raise VoicekitError("VK-RES-006", detail=lease.call_id)
                await cursor.close()
                await database.execute(
                    """
                    INSERT INTO call_events(
                        event_id, call_id, event_type, is_terminal, body, created_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (
                        event_id,
                        lease.call_id,
                        request.event_type,
                        body,
                        _iso(request.ended_at),
                    ),
                )
                await database.execute(
                    """
                    INSERT INTO deliveries(
                        event_id, endpoint, status, next_attempt_at
                    ) VALUES (?, ?, 'pending', ?)
                    """,
                    (event_id, delivery.endpoint, _iso(request.ended_at)),
                )
                await database.commit()
            except VoicekitError:
                if database.in_transaction:
                    await database.rollback()
                raise
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoicekitError("VK-RES-008", detail=str(exc)) from exc
        return PersistedEvent(
            event_id=event_id,
            call_id=lease.call_id,
            event_type=request.event_type,
            body=body,
            created_at=request.ended_at,
        )

    async def mark_recording_ready(self, update: RecordingReady) -> PersistedEvent:
        """Persist an engine artifact and emit one non-terminal update event."""
        database = self._connection()
        async with self._write_lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                row = await _fetch_one(
                    database,
                    """
                    SELECT calls.*
                    FROM calls JOIN recordings USING(call_id)
                    WHERE recordings.recording_id = ?
                    """,
                    (update.recording_id,),
                )
                if row is None:
                    raise VoicekitError("VK-RES-010", detail=update.recording_id)
                if row["status"] == "active":
                    raise VoicekitError(
                        "VK-RES-010",
                        detail="recording-ready arrived before terminal persistence.",
                    )
                existing = await _fetch_one(
                    database,
                    """
                    SELECT * FROM call_events
                    WHERE call_id = ? AND event_type = 'call.recording.ready'
                    """,
                    (str(row["call_id"]),),
                )
                if existing is not None:
                    await database.rollback()
                    return _event_from_row(existing)

                call = await self._materialize(row)
                delivery = _delivery_config(row)
                event_id = f"evt_{uuid.uuid4().hex}"
                ended_at = _parse(str(row["ended_at"]))
                body = build_event_body(
                    event_id=event_id,
                    event_type="call.recording.ready",
                    call=call,
                    ended_at=ended_at,
                    ended_reason=str(row["terminal_reason"]),
                    outcome=row["outcome"],
                    data=cast(
                        "dict[str, JsonValue]",
                        json.loads(str(row["results_json"])),
                    ),
                    interruptions=int(row["interruptions"]),
                    delivery=delivery,
                    recording=update,
                    recording_id=update.recording_id,
                )
                await database.execute(
                    """
                    UPDATE recordings
                    SET status = 'ready', access_url = ?, storage_key = ?, ready_at = ?
                    WHERE recording_id = ?
                    """,
                    (
                        update.access_url,
                        update.storage_key,
                        _iso(update.ready_at),
                        update.recording_id,
                    ),
                )
                await database.execute(
                    """
                    INSERT INTO call_events(
                        event_id, call_id, event_type, is_terminal, body, created_at
                    ) VALUES (?, ?, 'call.recording.ready', 0, ?, ?)
                    """,
                    (
                        event_id,
                        str(row["call_id"]),
                        body,
                        _iso(update.ready_at),
                    ),
                )
                await database.execute(
                    """
                    INSERT INTO deliveries(
                        event_id, endpoint, status, next_attempt_at
                    ) VALUES (?, ?, 'pending', ?)
                    """,
                    (event_id, delivery.endpoint, _iso(update.ready_at)),
                )
                await database.commit()
            except VoicekitError:
                if database.in_transaction:
                    await database.rollback()
                raise
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoicekitError("VK-RES-008", detail=str(exc)) from exc
        return PersistedEvent(
            event_id=event_id,
            call_id=str(row["call_id"]),
            event_type="call.recording.ready",
            body=body,
            created_at=update.ready_at,
        )

    async def get_event(self, event_id: str) -> PersistedEvent:
        row = await _fetch_one(
            self._connection(),
            "SELECT * FROM call_events WHERE event_id = ?",
            (event_id,),
        )
        if row is None:
            raise VoicekitError("VK-RES-009", detail=event_id)
        return _event_from_row(row)

    async def get_terminal_event_for_call(self, call_id: str) -> PersistedEvent:
        row = await _fetch_one(
            self._connection(),
            "SELECT * FROM call_events WHERE call_id = ? AND is_terminal = 1",
            (call_id,),
        )
        if row is None:
            raise VoicekitError("VK-RES-009", detail=call_id)
        return _event_from_row(row)

    async def get_result_snapshot(self, call_id: str) -> ResultSnapshot:
        """Return incrementally flushed data for the local/admin playground."""
        row = await _fetch_one(
            self._connection(),
            "SELECT outcome, results_json, interruptions FROM calls WHERE call_id = ?",
            (call_id,),
        )
        if row is None:
            raise VoicekitError("VK-OBS-003", detail=call_id)
        return ResultSnapshot.model_validate(
            {
                "outcome": row["outcome"],
                "data": json.loads(str(row["results_json"])),
                "interruptions": row["interruptions"],
            }
        )

    async def get_recording_for_call(self, call_id: str) -> RecordingSnapshot | None:
        """Return engine-owned metadata without exposing a carrier recording URL."""
        row = await _fetch_one(
            self._connection(),
            "SELECT * FROM recordings WHERE call_id = ?",
            (call_id,),
        )
        if row is None:
            return None
        return RecordingSnapshot.model_validate(
            {
                "recording_id": row["recording_id"],
                "call_id": row["call_id"],
                "status": row["status"],
                "access_url": row["access_url"],
                "storage_key": row["storage_key"],
                "created_at": _parse(str(row["created_at"])),
                "ready_at": (None if row["ready_at"] is None else _parse(str(row["ready_at"]))),
            }
        )

    async def claim_deliveries(
        self,
        *,
        owner_id: str,
        limit: int,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[DeliveryClaim, ...]:
        """Lease due outbox rows with one UPDATE...RETURNING statement."""
        if not 1 <= limit <= 100:
            raise VoicekitError("VK-OBS-005", detail=f"delivery limit={limit}")
        current = _utc(now)
        expires_at = _expires(current, lease_ttl)
        database = self._connection()
        async with self._write_lock:
            try:
                cursor = await database.execute(
                    """
                    WITH due AS (
                        SELECT event_id, endpoint
                        FROM deliveries
                        WHERE (
                            status = 'pending' AND next_attempt_at <= ?
                        ) OR (
                            status = 'delivering' AND lease_expires_at <= ?
                        )
                        ORDER BY next_attempt_at, event_id
                        LIMIT ?
                    )
                    UPDATE deliveries
                    SET status = 'delivering',
                        attempt_count = attempt_count + 1,
                        lease_owner = ?,
                        lease_expires_at = ?
                    WHERE (event_id, endpoint) IN (
                        SELECT event_id, endpoint FROM due
                    )
                    RETURNING event_id, endpoint, attempt_count
                    """,
                    (
                        _iso(current),
                        _iso(current),
                        limit,
                        owner_id,
                        _iso(expires_at),
                    ),
                )
                claimed_rows = list(await cursor.fetchall())
                await cursor.close()
                await database.commit()
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoicekitError("VK-RES-008", detail=str(exc)) from exc

        claims: list[DeliveryClaim] = []
        for delivery_row in claimed_rows:
            event = await self.get_event(str(delivery_row["event_id"]))
            claims.append(
                DeliveryClaim(
                    event_id=event.event_id,
                    call_id=event.call_id,
                    endpoint=str(delivery_row["endpoint"]),
                    body=event.body,
                    attempt_count=int(delivery_row["attempt_count"]),
                    lease_owner=owner_id,
                    lease_expires_at=expires_at,
                )
            )
        return tuple(claims)

    async def acknowledge_delivery(
        self,
        claim: DeliveryClaim,
        *,
        now: datetime | None = None,
    ) -> None:
        """Mark one exclusively claimed attempt delivered."""
        current = _utc(now)
        database = self._connection()
        async with self._write_lock:
            cursor = await database.execute(
                """
                UPDATE deliveries
                SET status = 'delivered', delivered_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
                WHERE event_id = ? AND endpoint = ? AND status = 'delivering'
                  AND lease_owner = ? AND attempt_count = ?
                """,
                (
                    _iso(current),
                    claim.event_id,
                    claim.endpoint,
                    claim.lease_owner,
                    claim.attempt_count,
                ),
            )
            changed = cursor.rowcount
            await cursor.close()
            if changed == 1:
                await database.execute(
                    """
                    UPDATE calls SET webhook_status = 'delivered'
                    WHERE call_id = ? AND NOT EXISTS (
                        SELECT 1 FROM deliveries
                        JOIN call_events USING(event_id)
                        WHERE call_events.call_id = ?
                          AND deliveries.status != 'delivered'
                    )
                    """,
                    (claim.call_id, claim.call_id),
                )
            await database.commit()
        if changed != 1:
            raise VoicekitError("VK-RES-008", detail="delivery claim was lost.")

    async def fail_delivery(
        self,
        claim: DeliveryClaim,
        *,
        error: str,
        jitter: Callable[[float], float],
        now: datetime | None = None,
    ) -> DeliveryRecord:
        """Schedule the canonical next attempt or visibly dead-letter at eight."""
        current = _utc(now)
        dead_lettered = claim.attempt_count >= MAX_DELIVERY_ATTEMPTS
        if dead_lettered:
            status = "dead_lettered"
            next_attempt_at = current
        else:
            base_delay = RETRY_DELAYS_SECONDS[claim.attempt_count]
            delay = max(0.0, jitter(float(base_delay)))
            if not 0.8 * base_delay <= delay <= 1.2 * base_delay:
                raise VoicekitError(
                    "VK-RES-008",
                    detail="retry jitter must remain within ±20%.",
                )
            status = "pending"
            next_attempt_at = current + timedelta(seconds=delay)
        database = self._connection()
        async with self._write_lock:
            cursor = await database.execute(
                """
                UPDATE deliveries
                SET status = ?, next_attempt_at = ?, last_error = ?,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE event_id = ? AND endpoint = ? AND status = 'delivering'
                  AND lease_owner = ? AND attempt_count = ?
                """,
                (
                    status,
                    _iso(next_attempt_at),
                    str(scrub_secrets(error))[:1000],
                    claim.event_id,
                    claim.endpoint,
                    claim.lease_owner,
                    claim.attempt_count,
                ),
            )
            changed = cursor.rowcount
            await cursor.close()
            if dead_lettered and changed == 1:
                await database.execute(
                    "UPDATE calls SET webhook_status = 'dead_lettered' WHERE call_id = ?",
                    (claim.call_id,),
                )
            await database.commit()
        if changed != 1:
            raise VoicekitError("VK-RES-008", detail="delivery claim was lost.")
        return (
            await self._delivery_records(
                "WHERE deliveries.event_id = ? AND deliveries.endpoint = ?",
                (claim.event_id, claim.endpoint),
            )
        )[0]

    async def redeliver(
        self,
        event_id: str,
        *,
        now: datetime | None = None,
    ) -> DeliveryRecord:
        """Reset delivery state while preserving event id and immutable bytes."""
        current = _utc(now)
        database = self._connection()
        async with self._write_lock:
            cursor = await database.execute(
                """
                UPDATE deliveries
                SET status = 'pending', attempt_count = 0, next_attempt_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = NULL, delivered_at = NULL
                WHERE event_id = ?
                """,
                (_iso(current), event_id),
            )
            changed = cursor.rowcount
            await cursor.close()
            await database.commit()
        if changed != 1:
            raise VoicekitError("VK-RES-009", detail=event_id)
        return (await self._delivery_records("WHERE deliveries.event_id = ?", (event_id,)))[0]

    async def list_deliveries(
        self,
        *,
        undelivered_only: bool = False,
    ) -> tuple[DeliveryRecord, ...]:
        where = "WHERE deliveries.status != 'delivered'" if undelivered_only else ""
        return await self._delivery_records(where, ())

    async def dlq_depth(self) -> int:
        row = await _fetch_one(
            self._connection(),
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
        cursor = await self._connection().execute(
            """
            SELECT call_id FROM calls
            WHERE status = 'active'
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            ORDER BY started_at
            """,
            (_iso(current),),
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
        return tuple(str(row["call_id"]) for row in rows)

    async def register_backup(
        self,
        *,
        backup_id: str,
        storage_key: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> None:
        current = _utc(now)
        await self._write(
            """
            INSERT INTO backups(backup_id, storage_key, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (backup_id, storage_key, _iso(current), _iso(expires_at)),
        )

    async def queue_retention(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[PurgeItem, ...]:
        """Delete expired database state and durably queue artifact deletion."""
        current = _utc(now)
        database = self._connection()
        async with self._write_lock:
            try:
                cursor = await database.execute("BEGIN IMMEDIATE")
                await cursor.close()
                cursor = await database.execute(
                    """
                    INSERT OR IGNORE INTO purge_queue(
                        storage_key, artifact_kind, queued_at
                    )
                    SELECT recordings.storage_key, 'recording', ?
                    FROM recordings JOIN calls USING(call_id)
                    WHERE recordings.storage_key IS NOT NULL
                      AND calls.status != 'active'
                      AND julianday(calls.ended_at) + calls.purge_after_days
                          <= julianday(?)
                    """,
                    (_iso(current), _iso(current)),
                )
                await cursor.close()
                cursor = await database.execute(
                    """
                    INSERT OR IGNORE INTO purge_queue(
                        storage_key, artifact_kind, queued_at
                    )
                    SELECT storage_key, 'backup', ?
                    FROM backups WHERE expires_at <= ?
                    """,
                    (_iso(current), _iso(current)),
                )
                await cursor.close()
                cursor = await database.execute(
                    """
                    DELETE FROM calls
                    WHERE status != 'active'
                      AND julianday(ended_at) + purge_after_days <= julianday(?)
                    """,
                    (_iso(current),),
                )
                await cursor.close()
                cursor = await database.execute(
                    "DELETE FROM backups WHERE expires_at <= ?",
                    (_iso(current),),
                )
                await cursor.close()
                await database.commit()
                cursor = await database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await cursor.fetchall()
                await cursor.close()
                cursor = await database.execute("VACUUM")
                await cursor.close()
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoicekitError("VK-RES-008", detail=str(exc)) from exc
        cursor = await database.execute(
            "SELECT storage_key, artifact_kind FROM purge_queue ORDER BY storage_key"
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
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
        await self._write("DELETE FROM purge_queue WHERE storage_key = ?", (storage_key,))

    async def _terminal_event_in_transaction(self, call_id: str) -> PersistedEvent:
        row = await _fetch_one(
            self._connection(),
            "SELECT * FROM call_events WHERE call_id = ? AND is_terminal = 1",
            (call_id,),
        )
        if row is None:
            raise VoicekitError("VK-RES-007", detail=call_id)
        return _event_from_row(row)

    async def _delivery_records(
        self,
        where: str,
        parameters: tuple[object, ...],
    ) -> tuple[DeliveryRecord, ...]:
        cursor = await self._connection().execute(
            f"""
            SELECT deliveries.*, call_events.call_id
            FROM deliveries JOIN call_events USING(event_id)
            {where}
            ORDER BY deliveries.next_attempt_at, deliveries.event_id
            """,
            parameters,
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
        return tuple(
            DeliveryRecord.model_validate(
                {
                    "event_id": row["event_id"],
                    "call_id": row["call_id"],
                    "endpoint": row["endpoint"],
                    "status": row["status"],
                    "attempt_count": row["attempt_count"],
                    "next_attempt_at": _parse(str(row["next_attempt_at"])),
                    "last_error": row["last_error"],
                    "delivered_at": (
                        None if row["delivered_at"] is None else _parse(str(row["delivered_at"]))
                    ),
                }
            )
            for row in rows
        )


async def _fetch_one(
    database: aiosqlite.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> aiosqlite.Row | None:
    cursor = await database.execute(statement, parameters)
    row = await cursor.fetchone()
    await cursor.close()
    return row


def _event_from_row(row: aiosqlite.Row) -> PersistedEvent:
    return PersistedEvent.model_validate(
        {
            "event_id": row["event_id"],
            "call_id": row["call_id"],
            "event_type": row["event_type"],
            "body": bytes(row["body"]),
            "created_at": _parse(str(row["created_at"])),
        }
    )


def _delivery_config(row: aiosqlite.Row) -> ResultDeliveryConfig:
    return ResultDeliveryConfig.model_validate(
        {
            "endpoint": row["delivery_endpoint"],
            "include": json.loads(str(row["include_json"])),
            "redact": json.loads(str(row["redact_json"])),
            "purge_after_days": row["purge_after_days"],
            "recording_enabled": row["recording_id"] is not None,
        }
    )


def _utc(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise VoicekitError("VK-RES-008", detail="timestamp must be timezone-aware.")
    return current.astimezone(UTC)


def _expires(now: datetime, ttl: timedelta) -> datetime:
    if ttl.total_seconds() <= 0:
        raise VoicekitError("VK-RES-008", detail="lease TTL must be positive.")
    return now + ttl


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _json(value: object) -> str:
    return json.dumps(
        scrub_secrets(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
