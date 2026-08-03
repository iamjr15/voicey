from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from io import BytesIO
from typing import cast

import pytest

from voicey.deploy.managed import managed_persistence_preflight
from voicey.relay.postgres import PostgresRelayJournal
from voicey.storage.postgres import PostgresRepository
from voicey.storage.s3 import S3ArtifactStore, S3Client

pytestmark = pytest.mark.integration


def _dsn() -> str:
    value = os.environ.get("VOICEY_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("VOICEY_TEST_POSTGRES_DSN is not configured")
    return value


@asynccontextmanager
async def _isolated_postgres_dsn() -> AsyncGenerator[str]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    schema = f"voicey_managed_{uuid.uuid4().hex}"
    settings = conninfo_to_dict(_dsn())
    existing_options = settings.get("options", "")
    settings["options"] = f"{existing_options} -c search_path={schema}".strip()
    isolated = make_conninfo("", **settings)
    async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield isolated
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


class _MemoryObjects:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def head_bucket(self, **_kwargs: object) -> Mapping[str, object]:
        return {}

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        key = (cast("str", kwargs["Bucket"]), cast("str", kwargs["Key"]))
        self.objects[key] = (
            cast("bytes", kwargs["Body"]),
            cast("dict[str, str]", kwargs["Metadata"]),
        )
        return {}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        key = (cast("str", kwargs["Bucket"]), cast("str", kwargs["Key"]))
        body, metadata = self.objects[key]
        return {"Body": BytesIO(body), "Metadata": metadata}

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        key = (cast("str", kwargs["Bucket"]), cast("str", kwargs["Key"]))
        self.objects.pop(key, None)
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["fly", "railway"])
async def test_managed_preflight_is_rollback_only_and_checks_all_backends(
    target: str,
) -> None:
    import psycopg

    async with _isolated_postgres_dsn() as dsn:
        objects = _MemoryObjects()
        artifacts = S3ArtifactStore(
            "voicey-artifacts",
            prefix="preflight-test",
            client=cast("S3Client", objects),
        )
        async with (
            PostgresRepository(dsn, max_size=2) as repository,
            PostgresRelayJournal(dsn, max_size=2) as journal,
        ):
            report = await managed_persistence_preflight(
                dsn=dsn,
                repository=repository,
                journal=journal,
                artifact_store=artifacts,
                target=target,
                storage_backend="postgres",
                artifact_backend="s3",
            )
            async with await psycopg.AsyncConnection.connect(dsn) as connection:
                cursor = await connection.execute("SELECT COUNT(*) FROM calls")
                row = await cursor.fetchone()

    assert report.schema_ready
    assert report.target == target
    assert report.relay_journal_ready
    assert report.artifact_round_trip
    assert report.rolling_generation == 2
    assert report.stale_writer_rejected
    assert report.terminal_event_count == 1
    assert row is not None
    assert int(row[0]) == 0
    assert not objects.objects
