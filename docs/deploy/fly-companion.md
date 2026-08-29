# Fly results companion

Pipecat Cloud and LiveKit Cloud workers are ephemeral. Their certified durable
peer is a user-owned Fly application running voicey in results-service mode,
with managed Postgres and a private S3-compatible object store. The companion
does not run an agent or define conversation flow.

`voicey deploy fly` owns the companion's provisioning and release lifecycle.
It uses current Fly CLI surfaces: `fly apps`, `fly mpg` for Managed Postgres,
`fly storage` for private Tigris, staged stdin secret import, and a rolling
`fly deploy`. It never uses the legacy unmanaged `fly postgres` surface.

## Provision and deploy

Install and authenticate the
[Fly CLI](https://fly.io/docs/flyctl/install/), then build a local wheel when
running an unpublished checkout. From the agent project, provide every
resource and cost choice explicitly:

```bash
fly auth whoami
voicey deploy fly \
  --app my-agent-results \
  --org my-fly-org \
  --region iad \
  --postgres-name my-agent-results-pg \
  --postgres-plan Basic \
  --postgres-volume-gb 10 \
  --bucket my-agent-results-objects \
  --engine-wheel /absolute/path/to/voicey-1.0.0-py3-none-any.whl \
  --yes
```

Published releases omit `--engine-wheel`. The command:

1. validates the complete plan, callback credentials, local wheel, and
   existing owner-only checkpoint before any platform mutation;
2. creates or reuses the exact app, Fly Managed Postgres 17 cluster, and
   private Tigris bucket;
3. attaches the pooled `DATABASE_URL`, verifies Tigris credentials are staged,
   generates relay/results credentials when absent, and imports secrets over
   stdin rather than process arguments;
4. writes a two-Machine, drain-aware Fly config and companion-only image;
5. deploys rolling, requires passing Fly service checks, probes unsigned
   liveness, then performs authenticated relay readiness.

Generated artifacts live under `.voicey/deploy/fly/`. The non-secret resource
ledger is `.voicey/deploy/fly-resources.json` with mode `0600`. It records
exact resource identifiers, ownership flags, artifact and secret fingerprints,
and gate status, never secret values. Generated credential material is kept in
the existing owner-only, ignored `.env`.

Pinned `uv` installs the companion into a virtual environment created with
`--without-pip`, so the runtime image does not carry pip or pip's vendored
build-tool dependency set. The final stage removes the base image's system pip
as well.

Rerunning the same command resumes from the ledger and revalidates every
resource. An existing unledgered app, cluster, bucket, `DATABASE_URL`, or
Tigris credential set stops the command. After verifying ownership and
attachment in Fly, rerun with `--adopt`; adopted resources are never deleted by
voicey rollback.

Rotate the relay and protected-results pair with:

```bash
voicey deploy fly \
  --app my-agent-results \
  --org my-fly-org \
  --region iad \
  --postgres-name my-agent-results-pg \
  --postgres-plan Basic \
  --postgres-volume-gb 10 \
  --bucket my-agent-results-objects \
  --rotate-credentials \
  --engine-wheel /absolute/path/to/voicey-1.0.0-py3-none-any.whl \
  --yes
```

The previous pair remains accepted during cutover. The command rejects a local
current secret whose fingerprint differs from the ledger, so accidental
replacement cannot silently strand deployed workers.

Rollback is explicit, destructive, and reverse ordered:

```bash
voicey deploy fly \
  --app my-agent-results \
  --org my-fly-org \
  --region iad \
  --postgres-name my-agent-results-pg \
  --postgres-plan Basic \
  --postgres-volume-gb 10 \
  --bucket my-agent-results-objects \
  --rollback-created \
  --yes
```

Only bucket, MPG cluster, and app rows marked created by voicey are destroyed.
The command never auto-rolls back a failed deployment and never deletes adopted
resources. This preserves evidence and avoids converting a recoverable partial
provision into data loss.

## Install and start

The companion extra contains only the managed-storage and carrier-callback
dependencies; it does not pull either voice runtime into the results process.

```bash
pip install 'voicey[companion]'
python -m voicey.deploy.results_service
```

Configure these values through Fly secrets, not a checked-in environment file:

| Variable | Purpose |
|---|---|
| `VOICEY_PUBLIC_BASE` | Normalized public HTTPS base for artifact and callback URLs |
| `DATABASE_URL` or `VOICEY_DATABASE_URL` | Managed Postgres DSN |
| `VOICEY_OBJECT_BUCKET` | Private object bucket |
| `VOICEY_OBJECT_PREFIX` | Object namespace; defaults to `voicey` |
| `VOICEY_OBJECT_ENDPOINT` | Optional HTTPS S3-compatible endpoint |
| `AWS_REGION` | Bucket region (`auto` for Tigris) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Optional paired object credentials |
| `VOICEY_RELAY_CREDENTIAL` | Current `vkr_` worker credential |
| `VOICEY_RELAY_PREVIOUS_CREDENTIAL` | Optional previous credential during rotation |
| `VOICEY_RESULTS_SECRET` | Current `whsec_` delivery and protected-read secret |
| `VOICEY_RESULTS_PREVIOUS_SECRET` | Optional previous result secret during rotation |
| `VOICEY_DEPLOY_TARGET` | Must be `fly` |
| `VOICEY_STORAGE_BACKEND` | Must be `postgres` |
| `VOICEY_ARTIFACT_BACKEND` | Must be `s3` |
| `VOICEY_CALLBACK_PROVIDERS` | Explicit comma list of carrier callback routes to install |
| `VOICEY_PROMETHEUS_ENABLED` | `1` in generated deployments |
| `VOICEY_PROMETHEUS_BIND` | `0.0.0.0` for Fly's private scraper |
| `VOICEY_PROMETHEUS_PORT` | Dedicated metrics port; generated value `9464` |
| `VOICEY_PROMETHEUS_PATH` | Generated value `/metrics` |
| `VOICEY_OTLP_ENDPOINT` | Optional HTTPS OTLP/HTTP traces endpoint |
| `VOICEY_OTLP_HEADERS` | Optional secret `name=value` header list |
| `PORT` | Public listener port; defaults to `8080` |

`VOICEY_CALLBACK_PROVIDERS` has no default provider. Each selected carrier
requires its documented credentials: Twilio account SID/token; Telnyx API key,
public key, and connection id; Vobiz auth id/token; or Plivo auth id/token.
Install only the providers used by the deployed agents.

The repository and relay journal use separate pools. The default maximum is
five connections each. `VOICEY_DB_CONNECTION_BUDGET` defaults to 20 and
startup rejects a pair of pools that exceeds it.

Generated `fly.toml` contains Fly's native `[metrics]` stanza for port 9464 and
path `/metrics`; that port is not part of the public HTTP service. The
companion reports relay-owned active calls, stable error-code counts, durable
DLQ depth, and latency histograms. Set `VOICEY_OTLP_ENDPOINT` through target
configuration to add PII-safe call/turn/tool spans; keep any collector auth in
the Fly secret `VOICEY_OTLP_HEADERS`.

## Startup preflight

The process admits no worker until all of these checks pass:

1. acquire the migration lock, apply/validate every checksummed Postgres
   migration, and query both repository and relay journal;
2. reach the private bucket and complete a checksummed write/read/delete;
3. run an old-generation/new-generation fence and one-terminal-event probe
   against the real schema inside a forced-rollback transaction.

The probe leaves no synthetic call, event, delivery, or object behind.
`GET /healthz` is an unsigned platform liveness/drain signal only. It makes no
storage-readiness claim. Worker admission requires an authenticated
`GET /v1/ready`, which checks Postgres, relay journal, object storage, protocol
version, and the replica's admission state.

## Surfaces and credentials

| Surface | Authentication |
|---|---|
| `/v1/ready`, `/v1/calls/**` | Exact relay HMAC plus durable nonce replay claim |
| `/v1/admin/**` | `Authorization: Bearer <current-or-previous whsec_>` |
| `/recordings/<id>` | Same current-or-previous result-secret bearer |
| `/<carrier>/events` | Carrier-native request signature |
| `/<carrier>/recordings` | Carrier-native request signature |
| `/healthz` | None; liveness/drain only |

Admin JSON and recording bytes return `Cache-Control: private, no-store`.
Recording URLs contain no token and no carrier URL. A verified carrier callback
downloads into the object store before the repository emits
`call.recording.ready`.

Point carrier status and recording callbacks at the companion, while answer
and media endpoints continue to point at the cloud runtime. Verified status
callbacks update only the durable provider observation; they do not obtain a
call-generation fence. The normal worker remains responsible for terminal
persistence. If that worker dies and its lease expires, the companion takes a
new generation, reconciles the latest signed provider observation, and writes
exactly one terminal event. A missing or still-active observation becomes
`call.failed` with `recovery_unknown`; success is never invented.

## Maintenance and drain

One bounded maintenance pass runs stale-call recovery, result delivery, then
retention. Delivery uses the canonical eight-attempt Standard Webhooks curve.
Retention deletes an object before acknowledging its durable purge row.

On `SIGTERM` or `SIGINT`, the replica first makes `/healthz` and signed
readiness fail and rejects new `begin`/`claim` operations. Already-fenced
updates remain valid during `VOICEY_DRAIN_GRACE_S` (10 seconds by default),
so Fly can shift traffic to the replacement replica. The process then runs one
final maintenance pass and closes both Postgres pools.

## Local verification

The credential-free companion suite uses SQLite plus an in-memory object
contract:

```bash
uv run pytest -q --no-cov tests/unit/test_results_companion.py
```

The managed probe and complete backend matrix require disposable Postgres 17:

```bash
TEST_DB_AUTH='voicey:voicey-test'
VOICEY_TEST_POSTGRES_DSN="postgresql://${TEST_DB_AUTH}@127.0.0.1:55432/voicey" \
  uv run pytest -q --no-cov \
  tests/integration/test_managed_results_service.py \
  tests/integration/test_postgres_repository.py \
  tests/integration/test_repository_backends.py
```

This validates the service and persistence invariants; it does not claim a
real Fly, bucket, carrier callback, or cloud-runtime deployment. Those gates
remain in `docs/GAPS.md` until their guarded commands actually pass.

The command choices above are pinned to the official
[app creation](https://fly.io/docs/flyctl/apps-create/),
[Managed Postgres](https://fly.io/docs/flyctl/mpg-create/),
[MPG attachment](https://fly.io/docs/flyctl/mpg-attach/),
[Tigris](https://fly.io/docs/flyctl/storage-create/),
[stdin secret import](https://fly.io/docs/flyctl/secrets-import/), and
[rolling deploy](https://fly.io/docs/flyctl/deploy/) references.
