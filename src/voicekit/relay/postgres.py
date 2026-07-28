"""Multi-replica Postgres relay journal for the Fly companion."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Self

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from voicekit.errors import VoicekitError
from voicekit.storage.postgres import PostgresMigrator


class PostgresRelayJournal:
    """Durable distributed journal with row-locked per-call stream cursors."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout_s: float = 10,
    ) -> None:
        if not dsn or min_size < 0 or max_size < 1 or min_size > max_size or timeout_s <= 0:
            raise VoicekitError("VK-REL-006", detail="Postgres journal settings are invalid.")
        self._pool = AsyncConnectionPool(
            dsn,
            connection_class=AsyncConnection[DictRow],
            kwargs={"row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_s,
            open=False,
            name="voicekit-relay",
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
        except VoicekitError:
            await self._pool.close()
            raise
        except (psycopg.Error, TimeoutError) as exc:
            await self._pool.close()
            raise VoicekitError("VK-REL-006", detail="Postgres relay pool is unavailable.") from exc
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

    async def claim_nonce(
        self,
        *,
        key_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "DELETE FROM relay_nonces WHERE expires_at <= %s",
                    (now,),
                )
                await connection.execute(
                    """
                    INSERT INTO relay_nonces(key_id, nonce, expires_at)
                    VALUES (%s, %s, %s)
                    """,
                    (key_id, nonce, expires_at),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise VoicekitError("VK-REL-003", detail="relay nonce was already used.") from exc
        except psycopg.Error as exc:
            raise VoicekitError("VK-REL-006", detail="could not journal relay nonce.") from exc

    async def reserve_request(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        request_kind: str,
        call_id: str,
        now: datetime,
    ) -> bytes | None:
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO relay_requests(
                        idempotency_key, request_hash, request_kind, call_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (idempotency_key, request_hash, request_kind, call_id, now),
                )
                cursor = await connection.execute(
                    """
                    SELECT * FROM relay_requests
                    WHERE idempotency_key = %s FOR UPDATE
                    """,
                    (idempotency_key,),
                )
                row = await cursor.fetchone()
                if (
                    row is None
                    or str(row["request_hash"]) != request_hash
                    or str(row["request_kind"]) != request_kind
                    or str(row["call_id"]) != call_id
                ):
                    raise VoicekitError(
                        "VK-REL-005",
                        detail="idempotency key was reused with different request bytes.",
                    )
                response = row["response_body"]
                return None if response is None else bytes(response)
        except VoicekitError:
            raise
        except psycopg.Error as exc:
            raise VoicekitError("VK-REL-006", detail="could not reserve relay request.") from exc

    async def complete_request(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        response_body: bytes,
    ) -> None:
        changed = await self._execute_count(
            """
            UPDATE relay_requests SET response_body = %s
            WHERE idempotency_key = %s AND request_hash = %s
              AND (response_body IS NULL OR response_body = %s)
            """,
            (response_body, idempotency_key, request_hash, response_body),
        )
        if changed != 1:
            raise VoicekitError(
                "VK-REL-005",
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
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO relay_streams(call_id) VALUES (%s)
                    ON CONFLICT(call_id) DO NOTHING
                    """,
                    (call_id,),
                )
                cursor = await connection.execute(
                    """
                    SELECT last_sequence FROM relay_streams
                    WHERE call_id = %s FOR UPDATE
                    """,
                    (call_id,),
                )
                stream = await cursor.fetchone()
                cursor = await connection.execute(
                    """
                    SELECT * FROM relay_updates
                    WHERE call_id = %s
                      AND (sequence = %s OR idempotency_key = %s)
                    FOR UPDATE
                    """,
                    (call_id, sequence, idempotency_key),
                )
                rows = list(await cursor.fetchall())
                if rows:
                    row = rows[0]
                    if (
                        len(rows) != 1
                        or int(row["sequence"]) != sequence
                        or str(row["idempotency_key"]) != idempotency_key
                        or str(row["request_hash"]) != request_hash
                    ):
                        raise VoicekitError(
                            "VK-REL-005",
                            detail="sequence or idempotency key conflicts with the stream.",
                        )
                    response = row["response_body"]
                    return None if response is None else bytes(response)
                last_sequence = 0 if stream is None else int(stream["last_sequence"])
                if sequence != last_sequence + 1:
                    raise VoicekitError(
                        "VK-REL-005",
                        detail=f"expected sequence {last_sequence + 1}, received {sequence}.",
                    )
                await connection.execute(
                    """
                    INSERT INTO relay_updates(
                        call_id, sequence, idempotency_key, request_hash, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (call_id, sequence, idempotency_key, request_hash, now),
                )
                return None
        except VoicekitError:
            raise
        except psycopg.Error as exc:
            raise VoicekitError("VK-REL-006", detail="could not reserve relay update.") from exc

    async def complete_update(
        self,
        *,
        call_id: str,
        sequence: int,
        idempotency_key: str,
        request_hash: str,
        response_body: bytes,
    ) -> None:
        try:
            async with self._pool.connection() as connection, connection.transaction():
                cursor = await connection.execute(
                    """
                    UPDATE relay_updates SET response_body = %s
                    WHERE call_id = %s AND sequence = %s AND idempotency_key = %s
                      AND request_hash = %s
                      AND (response_body IS NULL OR response_body = %s)
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
                if cursor.rowcount != 1:
                    raise VoicekitError(
                        "VK-REL-005",
                        detail="relay update acknowledgement no longer matches.",
                    )
                cursor = await connection.execute(
                    """
                    UPDATE relay_streams SET last_sequence = %s
                    WHERE call_id = %s AND last_sequence IN (%s, %s)
                    """,
                    (sequence, call_id, sequence - 1, sequence),
                )
                if cursor.rowcount != 1:
                    raise VoicekitError(
                        "VK-REL-005",
                        detail="relay stream cursor changed before acknowledgement.",
                    )
        except VoicekitError:
            raise
        except psycopg.Error as exc:
            raise VoicekitError("VK-REL-006", detail="could not acknowledge update.") from exc

    async def next_sequence(self, call_id: str) -> int:
        try:
            async with self._pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT last_sequence FROM relay_streams WHERE call_id = %s",
                    (call_id,),
                )
                row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise VoicekitError("VK-REL-006", detail="could not read stream cursor.") from exc
        return (0 if row is None else int(row["last_sequence"])) + 1

    async def _execute_count(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> int:
        from typing import LiteralString, cast

        from psycopg import sql

        try:
            async with self._pool.connection() as connection:
                cursor = await connection.execute(
                    sql.SQL(cast("LiteralString", statement)),
                    parameters,
                )
                return cursor.rowcount
        except psycopg.Error as exc:
            raise VoicekitError("VK-REL-006", detail="Postgres journal mutation failed.") from exc
