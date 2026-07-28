# Managed storage

Fly, Railway, and the user-owned cloud-worker companion use managed Postgres
plus private S3-compatible object storage. Docker and self-hosted single-node
deployments continue to use SQLite WAL/FULL plus protected local artifacts.
Ephemeral Pipecat Cloud and LiveKit Cloud workers never own durable state; they
write through the authenticated results relay.

## Install and configure

Install repository/object adapters alone with the cloud extra:

```bash
pip install 'voicekit[cloud]'
```

For the executable results companion, install the companion extra. It adds the
carrier-signature/download dependencies without installing Pipecat or LiveKit:

```bash
pip install 'voicekit[companion]'
```

The resolved P3 implementation uses Psycopg 3 with `psycopg_pool` and boto3.
Applications pass the platform's Postgres DSN to `PostgresRepository` and the
private bucket settings to `S3ArtifactStore`. Credentials belong in platform
secrets or workload identity, never in `voicekit.jsonc`, generated artifacts,
or the resource ledger.

```python
import os

from voicekit.storage.postgres import PostgresRepository
from voicekit.storage.s3 import S3ArtifactStore

repository = PostgresRepository(
    os.environ["VOICEKIT_DATABASE_URL"],
    min_size=1,
    max_size=10,
)
artifacts = S3ArtifactStore(
    "private-voicekit-artifacts",
    prefix="production/relay",
    endpoint_url="https://fly.storage.tigris.dev",
    region_name="auto",
)
```

The object endpoint must use HTTPS, except for an explicit loopback endpoint
used by local S3 emulators. Keys are relative and traversal-safe. Voicekit
stores SHA-256 metadata on every object and rejects reads whose metadata is
missing or whose bytes no longer match.

## Postgres schema and migrations

`PostgresRepository.open()` takes a transaction-scoped advisory lock, applies
the packaged append-only SQL migrations and checksum rows in one transaction,
then validates the complete migration history. Concurrent old/new processes
may open against the same schema. An unknown migration or a changed checksum
fails startup with `VK-OBS-004`.

Migration policy is expand/contract:

1. An expand release adds nullable columns, tables, or indexes that both
   generations can tolerate.
2. At least one full release runs with both representations available.
3. A later contract release removes the retired representation only after the
   rolling-generation invariant has passed.

The repository uses pooled async connections, `TIMESTAMPTZ`, `JSONB`, and
`BYTEA`. Terminal state, immutable event bytes, and delivery insertion remain
one transaction. Delivery workers claim rows with `FOR UPDATE SKIP LOCKED`;
call ownership is fenced by owner plus generation.

## Artifact preflight and retention

`S3ArtifactStore.verify_round_trip(nonce)` writes unpredictable bytes below the
configured prefix, reads and verifies them, and deletes the probe in a
`finally` block. Deploy targets run this after bucket provisioning and before
call admission. Carrier recording callbacks are processed by the durable
companion, which writes the recording first and only then emits
`call.recording.ready`.

Retention first records object deletion in the database purge queue, then
deletes the object and acknowledges that queue item. A crash at either boundary
is replay-safe. Database rows, outbox/dead letters, recording objects, and
backup objects therefore share the configured `purge_after_days` lifecycle.

## Local backend equivalence

Run the same contract and chaos matrix against SQLite and a disposable
Postgres 17 database:

```bash
docker run --rm --name voicekit-postgres-p35 \
  -e POSTGRES_USER=voicekit \
  -e POSTGRES_PASSWORD=voicekit-test \
  -e POSTGRES_DB=voicekit \
  -p 55432:5432 -d postgres:17-alpine
TEST_DB_AUTH='voicekit:voicekit-test'
VOICEKIT_TEST_POSTGRES_DSN="postgresql://${TEST_DB_AUTH}@127.0.0.1:55432/voicekit" \
  uv run pytest -q --no-cov \
  tests/integration/test_repository_backends.py \
  tests/integration/test_postgres_repository.py
docker stop voicekit-postgres-p35
```

The suite verifies observation parity, terminal/outbox atomicity, duplicate
terminal collapse, generation fencing, delivery claim exclusivity, retention,
signed relay operation, migration locking/checksums, and schema rejection.

`tests/integration/test_managed_results_service.py` additionally runs the
target preflight against the actual Postgres schema under forced rollback and
proves that no synthetic rows survive:

```bash
TEST_DB_AUTH='voicekit:voicekit-test'
VOICEKIT_TEST_POSTGRES_DSN="postgresql://${TEST_DB_AUTH}@127.0.0.1:55432/voicekit" \
  uv run pytest -q --no-cov tests/integration/test_managed_results_service.py
```

For a real AWS S3, Tigris, R2, or MinIO-compatible bucket, use the guarded
command in `docs/GAPS.md`. The probe creates and removes only one object under
`VOICEKIT_OBJECT_PREFIX`.
