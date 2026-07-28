# Railway results companion

Railway is a user-owned deployment target for voicekit's durable
results-service companion. It runs no Pipecat or LiveKit conversation flow.
Ephemeral cloud workers use its signed relay for durable call state, results,
carrier callbacks, recordings, delivery, retention, and stale-call recovery.

The supported operator contract is Railway CLI `>=5.30.1,<6`; 5.30.1 is
executed in the local verification gate. Voicekit uses current project,
service, managed Postgres, private bucket, domain, variable, deployment, and
scale commands. It does not use Railway's optional MCP feature.

## Provision and deploy

Install and authenticate the
[Railway CLI](https://docs.railway.com/guides/cli), build the unpublished
voicekit wheel when working from this repository, and run the command from an
initialized voicekit agent project:

```bash
railway whoami
voicekit deploy railway \
  --project my-agent-results \
  --workspace exact-workspace \
  --environment production \
  --service my-agent-results \
  --bucket my-agent-results-objects \
  --service-region us-east \
  --bucket-region iad \
  --engine-wheel /absolute/path/to/voicekit-0.0.0.dev0-py3-none-any.whl \
  --yes
```

Published releases omit `--engine-wheel`. No resource or region is selected by
default. The command:

1. validates the plan, callback credentials, wheel, CLI range, authentication,
   and owner-only checkpoint before mutation;
2. creates the exact project/environment, application service, managed
   Postgres service, private Railway bucket, and service domain;
3. connects Postgres and bucket variables with Railway references and sends
   relay/results/carrier secrets one at a time over stdin;
4. generates a secret-free, non-root companion image and current
   `railway.json`;
5. runs the managed persistence preflight before the release starts, deploys
   detached, polls the release to success, and scales the selected region to
   two replicas; and
6. probes unsigned `/healthz`, then authenticated relay `/v1/ready`.

Generated artifacts live in `.voicekit/deploy/railway/`. The owner-only,
non-secret checkpoint is `.voicekit/deploy/railway-resources.json`. It stores
exact platform ids, created/adopted flags, artifact and credential
fingerprints, release id, and gate status—never secret values.

The ignored `.env` holds generated current/previous relay and results
credentials. Railway receives secret values only through
`railway variable set NAME --stdin`; they do not appear in process arguments,
generated artifacts, JSON output, or the checkpoint.

## Resume, adoption, and rotation

Rerun the identical deploy command to resume. Every ledgered identity is
revalidated before reuse, and no project, service, Postgres instance, bucket,
or domain is recreated.

An existing unledgered project is never selected by name alone. After checking
its workspace, environment, billing boundary, and contents in Railway, adopt it
with both its exact id and the explicit adoption flag:

```bash
voicekit deploy railway \
  --project my-agent-results \
  --project-id exact-project-id \
  --workspace exact-workspace \
  --environment production \
  --service my-agent-results \
  --bucket my-agent-results-objects \
  --service-region us-east \
  --bucket-region iad \
  --adopt \
  --engine-wheel /absolute/path/to/voicekit-0.0.0.dev0-py3-none-any.whl \
  --yes
```

Adopted resources never gain deletion ownership. Ambiguous names, missing
ledgered ids, and identity drift stop with `VK-DEP-007`.

Rotate the relay/results pair with the same resource flags plus:

```bash
voicekit deploy railway \
  --project my-agent-results \
  --workspace exact-workspace \
  --environment production \
  --service my-agent-results \
  --bucket my-agent-results-objects \
  --service-region us-east \
  --bucket-region iad \
  --rotate-credentials \
  --engine-wheel /absolute/path/to/voicekit-0.0.0.dev0-py3-none-any.whl \
  --yes
```

The prior pair remains accepted for the overlap. A current local credential
whose fingerprint differs from the checkpoint is rejected rather than
silently replacing deployed worker access.

## Rollback

Rollback is explicit and destructive:

```bash
voicekit deploy railway \
  --project my-agent-results \
  --workspace exact-workspace \
  --environment production \
  --service my-agent-results \
  --bucket my-agent-results-objects \
  --service-region us-east \
  --bucket-region iad \
  --rollback-created \
  --yes
```

Voicekit deletes only resources marked created in its checkpoint, in this
order: service domain, private bucket, Postgres service, application service,
project. Failed deployments are left checkpointed for inspection and resume;
there is no automatic destructive rollback. Never use the rollback command on
a production or incompletely reviewed ledger.

## Runtime variables

Railway dependency references supply:

| Variable | Railway reference |
|---|---|
| `DATABASE_URL` | managed Postgres `DATABASE_URL` |
| `VOICEKIT_OBJECT_BUCKET` | bucket `BUCKET` |
| `VOICEKIT_OBJECT_ENDPOINT` | bucket `ENDPOINT` |
| `AWS_REGION` | bucket `REGION` |
| `AWS_ACCESS_KEY_ID` | bucket `ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` | bucket `SECRET_ACCESS_KEY` |

Voicekit also sets the strict topology `VOICEKIT_DEPLOY_TARGET=railway`,
`VOICEKIT_STORAGE_BACKEND=postgres`, and `VOICEKIT_ARTIFACT_BACKEND=s3`. The
companion listens publicly on port 8080. Prometheus listens separately on port
9464 and is not the public service port. Optional OTLP endpoints are
non-secret variables; optional collector headers are passed through stdin and
read indirectly by name.

The generated deployment uses two replicas in the chosen service region,
`RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=30`, a 20-second voicekit drain grace,
`/healthz` as the platform health check, and an on-failure restart policy.
During replacement, the old replica rejects new relay admission before its
grace period while allowing already fenced updates to finish.

## Preflight and readiness

The Railway pre-deploy command runs:

```bash
python -m voicekit.deploy.results_service --preflight-only
```

It applies and validates checksummed migrations under the Postgres migration
lock, queries the repository and relay journal, completes a checksummed bucket
write/read/delete, and proves generation 1→2 fencing plus exactly one terminal
event inside a forced-rollback transaction. No synthetic database row or
object survives.

`GET /healthz` is liveness/drain only. Worker admission requires signed
`GET /v1/ready`, which covers repository, relay journal, bucket, protocol
version, and replica admission. A successful Railway deployment without signed
readiness is not promoted.

## Local and live verification

Run the credential-free gate with disposable PostgreSQL 17:

```bash
export VOICEKIT_TEST_POSTGRES_DSN=postgresql://voicekit:voicekit-test@127.0.0.1:55434/voicekit  # pragma: allowlist secret
uv run python tests/verification/run_p4_railway_gate.py
```

That gate executes the supported CLI version and local invariants; it does not
claim an authenticated Railway deployment. The complete paid project,
rerun/rotation, rolling-call, evidence, and rollback commands are in
`docs/GAPS.md`.

The command choices are pinned to Railway's official
[CLI](https://docs.railway.com/guides/cli),
[configuration](https://docs.railway.com/reference/config-as-code),
[pre-deploy command](https://docs.railway.com/guides/pre-deploy-command),
[variables](https://docs.railway.com/guides/variables),
[PostgreSQL](https://docs.railway.com/guides/postgresql), and
[buckets](https://docs.railway.com/reference/buckets) documentation.
