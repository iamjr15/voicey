"""Durable replay, request, and ordered-stream journal for the relay."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

import aiosqlite

from voicey.errors import VoiceyError
from voicey.security.files import ensure_private_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_nonces (
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(key_id, nonce)
);
CREATE INDEX IF NOT EXISTS relay_nonces_expiry_idx ON relay_nonces(expires_at);

CREATE TABLE IF NOT EXISTS relay_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request_kind TEXT NOT NULL,
    call_id TEXT NOT NULL,
    response_body BLOB,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relay_streams (
    call_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    CHECK(last_sequence >= 0)
);

CREATE TABLE IF NOT EXISTS relay_updates (
    call_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_body BLOB,
    created_at TEXT NOT NULL,
    PRIMARY KEY(call_id, sequence),
    UNIQUE(call_id, idempotency_key),
    CHECK(sequence >= 1)
);
"""


class RelayJournal(Protocol):
    """Durable protocol state shared by SQLite and Postgres implementations."""

    def ready(self) -> Awaitable[bool]: ...

    def claim_nonce(
        self,
        *,
        key_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> Awaitable[None]: ...

    def reserve_request(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        request_kind: str,
        call_id: str,
        now: datetime,
    ) -> Awaitable[bytes | None]: ...

    def complete_request(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        response_body: bytes,
    ) -> Awaitable[None]: ...

    def reserve_update(
        self,
        *,
        call_id: str,
        sequence: int,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> Awaitable[bytes | None]: ...

    def complete_update(
        self,
        *,
        call_id: str,
        sequence: int,
        idempotency_key: str,
        request_hash: str,
        response_body: bytes,
    ) -> Awaitable[None]: ...

    def next_sequence(self, call_id: str) -> Awaitable[int]: ...


class SQLiteRelayJournal:
    """One durable local journal; the Fly companion uses its Postgres peer."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._database: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

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
        if self._database is not None:
            return self
        try:
            ensure_private_file(self.path)
            database = await aiosqlite.connect(self.path)
            database.row_factory = aiosqlite.Row
            await database.execute("PRAGMA journal_mode = WAL")
            await database.execute("PRAGMA synchronous = FULL")
            await database.execute("PRAGMA busy_timeout = 5000")
            await database.executescript(_SCHEMA)
            await database.commit()
            self._database = database
            return self
        except (OSError, sqlite3.Error) as exc:
            raise VoiceyError(
                "VY-REL-006",
                detail=f"could not open relay journal: {exc}",
            ) from exc

    async def close(self) -> None:
        database, self._database = self._database, None
        if database is None:
            return
        try:
            await database.commit()
            await database.close()
        except sqlite3.Error as exc:
            raise VoiceyError("VY-REL-006", detail="could not close relay journal.") from exc

    async def ready(self) -> bool:
        database = self._connection()
        try:
            cursor = await database.execute("SELECT 1")
            row = await cursor.fetchone()
            await cursor.close()
        except sqlite3.Error as exc:
            raise VoiceyError("VY-REL-006", detail="relay journal readiness failed.") from exc
        return row is not None and int(row[0]) == 1

    async def claim_nonce(
        self,
        *,
        key_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        database = self._connection()
        async with self._lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                await database.execute(
                    "DELETE FROM relay_nonces WHERE expires_at <= ?",
                    (_iso(now),),
                )
                await database.execute(
                    "INSERT INTO relay_nonces(key_id, nonce, expires_at) VALUES (?, ?, ?)",
                    (key_id, nonce, _iso(expires_at)),
                )
                await database.commit()
            except VoiceyError:
                await database.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                await database.rollback()
                raise VoiceyError("VY-REL-003", detail="relay nonce was already used.") from exc
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError("VY-REL-006", detail="could not journal relay nonce.") from exc

    async def reserve_request(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        request_kind: str,
        call_id: str,
        now: datetime,
    ) -> bytes | None:
        """Return a cached acknowledgement or reserve one recoverable request."""
        database = self._connection()
        async with self._lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                await database.execute(
                    """
                    INSERT OR IGNORE INTO relay_requests(
                        idempotency_key, request_hash, request_kind, call_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, request_hash, request_kind, call_id, _iso(now)),
                )
                cursor = await database.execute(
                    "SELECT * FROM relay_requests WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                await database.commit()
            except VoiceyError:
                await database.rollback()
                raise
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError(
                    "VY-REL-006",
                    detail="could not reserve relay request.",
                ) from exc
        if (
            row is None
            or str(row["request_hash"]) != request_hash
            or str(row["request_kind"]) != request_kind
            or str(row["call_id"]) != call_id
        ):
            raise VoiceyError(
                "VY-REL-005",
                detail="idempotency key was reused with different request bytes.",
            )
        response = row["response_body"]
        return None if response is None else bytes(response)

    async def complete_request(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        response_body: bytes,
    ) -> None:
        await self._complete(
            """
            UPDATE relay_requests SET response_body = ?
            WHERE idempotency_key = ? AND request_hash = ?
              AND (response_body IS NULL OR response_body = ?)
            """,
            (response_body, idempotency_key, request_hash, response_body),
            detail="could not acknowledge relay request.",
        )

    async def reserve_update(
        self,
        *,
        call_id: str,
        sequence: int,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> bytes | None:
        """Enforce a gap-free per-call stream and return cached acknowledgements."""
        database = self._connection()
        async with self._lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                await database.execute(
                    "INSERT OR IGNORE INTO relay_streams(call_id) VALUES (?)",
                    (call_id,),
                )
                cursor = await database.execute(
                    "SELECT last_sequence FROM relay_streams WHERE call_id = ?",
                    (call_id,),
                )
                stream = await cursor.fetchone()
                await cursor.close()
                cursor = await database.execute(
                    """
                    SELECT * FROM relay_updates
                    WHERE call_id = ? AND (sequence = ? OR idempotency_key = ?)
                    """,
                    (call_id, sequence, idempotency_key),
                )
                rows = list(await cursor.fetchall())
                await cursor.close()
                if rows:
                    row = rows[0]
                    if (
                        len(rows) != 1
                        or int(row["sequence"]) != sequence
                        or str(row["idempotency_key"]) != idempotency_key
                        or str(row["request_hash"]) != request_hash
                    ):
                        raise VoiceyError(
                            "VY-REL-005",
                            detail="sequence or idempotency key conflicts with the stream.",
                        )
                    await database.commit()
                    response = row["response_body"]
                    return None if response is None else bytes(response)
                last_sequence = 0 if stream is None else int(stream["last_sequence"])
                if sequence != last_sequence + 1:
                    raise VoiceyError(
                        "VY-REL-005",
                        detail=f"expected sequence {last_sequence + 1}, received {sequence}.",
                    )
                await database.execute(
                    """
                    INSERT INTO relay_updates(
                        call_id, sequence, idempotency_key, request_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (call_id, sequence, idempotency_key, request_hash, _iso(now)),
                )
                await database.commit()
                return None
            except VoiceyError:
                await database.rollback()
                raise
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError("VY-REL-006", detail="could not reserve relay update.") from exc

    async def complete_update(
        self,
        *,
        call_id: str,
        sequence: int,
        idempotency_key: str,
        request_hash: str,
        response_body: bytes,
    ) -> None:
        database = self._connection()
        async with self._lock:
            try:
                await database.execute("BEGIN IMMEDIATE")
                cursor = await database.execute(
                    """
                    UPDATE relay_updates SET response_body = ?
                    WHERE call_id = ? AND sequence = ? AND idempotency_key = ?
                      AND request_hash = ?
                      AND (response_body IS NULL OR response_body = ?)
                    """,
                    (
                        response_body,
                        call_id,
                        sequence,
                        idempotency_key,
                        request_hash,
                        response_body,
                    ),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise VoiceyError(
                        "VY-REL-005",
                        detail="relay update acknowledgement no longer matches.",
                    )
                cursor = await database.execute(
                    """
                    UPDATE relay_streams SET last_sequence = ?
                    WHERE call_id = ? AND last_sequence IN (?, ?)
                    """,
                    (sequence, call_id, sequence - 1, sequence),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise VoiceyError(
                        "VY-REL-005",
                        detail="relay stream cursor changed before acknowledgement.",
                    )
                await database.commit()
            except VoiceyError:
                await database.rollback()
                raise
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError("VY-REL-006", detail="could not acknowledge update.") from exc

    async def next_sequence(self, call_id: str) -> int:
        cursor = await self._connection().execute(
            "SELECT last_sequence FROM relay_streams WHERE call_id = ?",
            (call_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (0 if row is None else int(row["last_sequence"])) + 1

    async def _complete(
        self,
        statement: str,
        parameters: tuple[object, ...],
        *,
        detail: str,
    ) -> None:
        database = self._connection()
        async with self._lock:
            try:
                cursor = await database.execute(statement, parameters)
                changed = cursor.rowcount
                await cursor.close()
                await database.commit()
            except sqlite3.Error as exc:
                await database.rollback()
                raise VoiceyError("VY-REL-006", detail=detail) from exc
        if changed != 1:
            raise VoiceyError("VY-REL-005", detail=detail)

    def _connection(self) -> aiosqlite.Connection:
        if self._database is None:
            raise VoiceyError("VY-REL-006", detail="relay journal is not open.")
        return self._database


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VoiceyError("VY-REL-006", detail="relay journal timestamp is naive.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")
