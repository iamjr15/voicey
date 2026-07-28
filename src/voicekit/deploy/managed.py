"""Managed Postgres/object-store deployment preflight and fencing probe."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import psycopg

from voicekit.errors import VoicekitError
from voicekit.relay.journal import RelayJournal
from voicekit.storage.s3 import S3ArtifactStore


class ManagedReadyRepository(Protocol):
    """Small structural contract used by deployment preflight."""

    async def ready(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ManagedPersistenceReport:
    """Machine-readable proof for the Fly/Postgres/object-store assignment."""

    target: str
    storage_backend: str
    artifact_backend: str
    schema_ready: bool
    relay_journal_ready: bool
    artifact_round_trip: bool
    rolling_generation: int
    stale_writer_rejected: bool
    terminal_event_count: int


async def managed_persistence_preflight(
    *,
    dsn: str,
    repository: ManagedReadyRepository,
    journal: RelayJournal,
    artifact_store: S3ArtifactStore,
    target: str,
    storage_backend: str,
    artifact_backend: str,
) -> ManagedPersistenceReport:
    """Fail closed unless the managed target's complete persistence matrix works."""
    if target != "fly" or storage_backend != "postgres" or artifact_backend != "s3" or not dsn:
        raise VoicekitError(
            "VK-DEP-002",
            detail="Fly companion requires target=fly, backend=postgres, and artifacts=s3.",
        )
    schema_ready = await repository.ready()
    journal_ready = await journal.ready()
    if not schema_ready or not journal_ready:
        raise VoicekitError(
            "VK-DEP-002",
            detail="managed Postgres schema or relay journal is not ready.",
        )
    await artifact_store.ready()
    await artifact_store.verify_round_trip(uuid.uuid4().hex)
    generation, stale_rejected, terminal_count = await postgres_rolling_generation_invariant(dsn)
    if generation != 2 or not stale_rejected or terminal_count != 1:
        raise VoicekitError(
            "VK-DEP-002",
            detail="managed rolling-generation invariant did not hold.",
        )
    return ManagedPersistenceReport(
        target=target,
        storage_backend=storage_backend,
        artifact_backend=artifact_backend,
        schema_ready=True,
        relay_journal_ready=True,
        artifact_round_trip=True,
        rolling_generation=generation,
        stale_writer_rejected=True,
        terminal_event_count=1,
    )


async def postgres_rolling_generation_invariant(dsn: str) -> tuple[int, bool, int]:
    """Exercise the live schema under rollback so preflight leaves no synthetic calls."""
    call_id = f"call_preflight_{uuid.uuid4().hex}"
    event_id = f"evt_preflight_{uuid.uuid4().hex}"
    started = datetime.now(UTC) - timedelta(seconds=2)
    try:
        async with (
            await psycopg.AsyncConnection.connect(dsn) as connection,
            connection.transaction(force_rollback=True),
        ):
            await connection.execute(
                """
                    INSERT INTO calls (
                        call_id, agent_name, runtime, channel, direction, config_hash,
                        status, webhook_status, started_at, updated_at, owner_id,
                        generation, lease_expires_at, delivery_endpoint
                    ) VALUES (
                        %s, 'deployment-preflight', 'pipecat', 'web', 'inbound', %s,
                        'active', 'not_ready', %s, %s, 'generation-old', 1, %s, %s
                    )
                    """,
                (
                    call_id,
                    "sha256:" + ("0" * 64),
                    started,
                    started,
                    started + timedelta(seconds=1),
                    "https://example.invalid/voicekit-results",
                ),
            )
            cursor = await connection.execute(
                """
                    UPDATE calls
                    SET owner_id = 'generation-new', generation = generation + 1,
                        lease_expires_at = %s, updated_at = %s
                    WHERE call_id = %s AND status = 'active'
                      AND lease_expires_at <= %s
                    RETURNING generation
                    """,
                (
                    started + timedelta(seconds=32),
                    started + timedelta(seconds=2),
                    call_id,
                    started + timedelta(seconds=2),
                ),
            )
            row = await cursor.fetchone()
            generation = 0 if row is None else int(row[0])
            stale = await connection.execute(
                """
                    UPDATE calls SET outcome = 'stale-write'
                    WHERE call_id = %s AND status = 'active'
                      AND owner_id = 'generation-old' AND generation = 1
                    """,
                (call_id,),
            )
            terminal = await connection.execute(
                """
                    UPDATE calls
                    SET status = 'completed', ended_at = %s, terminal_reason = 'agent_hangup',
                        updated_at = %s, webhook_status = 'pending'
                    WHERE call_id = %s AND status = 'active'
                      AND owner_id = 'generation-new' AND generation = 2
                    """,
                (
                    started + timedelta(seconds=3),
                    started + timedelta(seconds=3),
                    call_id,
                ),
            )
            if terminal.rowcount == 1:
                await connection.execute(
                    """
                        INSERT INTO call_events(
                            event_id, call_id, event_type, is_terminal, body, created_at
                        ) VALUES (%s, %s, 'call.completed', TRUE, %s, %s)
                        """,
                    (
                        event_id,
                        call_id,
                        b'{"preflight":true}',
                        started + timedelta(seconds=3),
                    ),
                )
            count_cursor = await connection.execute(
                """
                    SELECT COUNT(*) FROM call_events
                    WHERE call_id = %s AND is_terminal
                    """,
                (call_id,),
            )
            count_row = await count_cursor.fetchone()
            terminal_count = 0 if count_row is None else int(count_row[0])
            return generation, stale.rowcount == 0, terminal_count
    except VoicekitError:
        raise
    except (psycopg.Error, OSError, TimeoutError) as exc:
        raise VoicekitError(
            "VK-DEP-002",
            detail="managed Postgres rolling-generation probe failed.",
        ) from exc
