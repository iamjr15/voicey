# Pipecat Cloud deployment

Pipecat Cloud runs the ephemeral media worker. Durable call state, recordings,
result delivery, carrier observations, and stale-call recovery remain on the
user-owned results companion. A worker fails closed unless that companion
passes signed readiness and acknowledges `begin_call`.

The installed deployment contract is `pipecat-cli==0.1.15`. This version
requires a pre-pushed image; it does not expose the cloud-build fields shown in
some newer documentation. Voicey therefore separates secret-free image
preparation from paid platform mutation.

## Prerequisites

1. Deploy and verify a [Fly results companion](fly-companion.md), or an
   equivalent user-owned relay that passes the same signed protocol/storage
   readiness contract.
2. Install and authenticate the Pipecat CLI with `pipecat cloud auth login`.
3. Choose the exact organization, current region from
   `pipecat cloud regions list`, secret-set name, immutable registry tag,
   scaling bounds, and agent profile. Voicey chooses none of them.
4. For an unpublished checkout, build the repository wheel with
   `uv build --out-dir dist`.

The local agent `.env` must contain `VOICEY_RELAY_CREDENTIAL` from the
companion plus its model/carrier credentials. Voicey sends only worker-owned
values. Database, object-store, results-signing, and previous companion
credentials are deliberately excluded.

## Prepare, build, and push

Run this from the agent project:

```bash
voicey deploy pipecat-cloud \
  --agent my-agent \
  --org my-org \
  --region us-west \
  --secret-set my-agent-secrets \
  --image registry.example.com/voicey/my-agent:git-sha \
  --min-agents 1 \
  --max-agents 4 \
  --profile agent-1x \
  --relay-url https://my-agent-results.fly.dev \
  --engine-wheel /absolute/path/to/voicey-0.0.0.dev0-py3-none-any.whl \
  --prepare-only
```

Published releases omit `--engine-wheel`. The command creates
`.voicey/deploy/pipecat-cloud/context`, filters every hidden/VCS/cache path,
rejects symlinks, copies no `.env`, and prints the exact next command:

```bash
docker build \
  -t registry.example.com/voicey/my-agent:git-sha \
  .voicey/deploy/pipecat-cloud/context
docker push registry.example.com/voicey/my-agent:git-sha
```

The multi-stage image uses a glibc Python base and UID/GID 10001. The generated
bot accepts the installed native `RunnerArguments` forms for Daily,
SmallWebRTC, and Twilio/Telnyx/Vobiz/Plivo WebSockets. It does not introduce a
conversation DSL.

## Deploy and verify

For a web-only project:

```bash
voicey deploy pipecat-cloud \
  --agent my-agent \
  --org my-org \
  --region us-west \
  --secret-set my-agent-secrets \
  --image registry.example.com/voicey/my-agent:git-sha \
  --min-agents 1 \
  --max-agents 4 \
  --profile agent-1x \
  --relay-url https://my-agent-results.fly.dev \
  --engine-wheel /absolute/path/to/voicey-0.0.0.dev0-py3-none-any.whl \
  --yes
```

Voicey verifies signed relay readiness before any platform mutation,
authenticates the CLI, validates the selected region, refuses an unledgered
existing agent, syncs secrets through a temporary `0600` file, deploys the
exact image, and requires ready status. It then starts a real Daily-backed
platform session and proves both its durable begin and terminal records.
A successful control-plane smoke is not browser-media evidence; complete one
real browser conversation before promoting a web deployment.

A phone project additionally requires a paid destination unless the operator
explicitly passes `--skip-smoke`:

```bash
voicey deploy pipecat-cloud \
  --agent my-agent \
  --org my-org \
  --region us-west \
  --secret-set my-agent-secrets \
  --image registry.example.com/voicey/my-agent:git-sha \
  --min-agents 1 \
  --max-agents 4 \
  --profile agent-1x \
  --relay-url https://my-agent-results.fly.dev \
  --smoke-to +15551234567 \
  --yes
```

The command cuts the configured number over, places one explicitly confirmed
paid call, and requires a terminal record plus delivered result. Any smoke
failure restores the active cutover token before returning the cataloged
error.

The companion hosts stable provider answer XML at:

```text
https://<relay>/v1/pipecat-cloud/<region>/<org>/<agent>/<provider>/answer
```

Twilio, Vobiz, and Plivo are cut over through their adapter. Telnyx owns a
separate TeXML Application: configure that application's URL to the exact
hosted-answer URL printed by voicey, then pass `--telnyx-texml-ready`.
Voicey will not infer that external application setting.

## Ownership, resume, and rollback

The owner-only nonsecret ledger is
`.voicey/deploy/pipecat-cloud-resources.json`. It records identity,
fingerprints, artifact digest, created/adopted ownership, cutover rollback
token, and smoke call id—never raw credentials.

An exact existing agent requires `--adopt`. Identity, region, relay
credential, or account drift stops the run. Adopted agents are never deleted.
Rerunning the same command resumes and revalidates each checkpoint.

Rollback uses the same required identity flags:

```bash
voicey deploy pipecat-cloud \
  --agent my-agent \
  --org my-org \
  --region us-west \
  --secret-set my-agent-secrets \
  --image registry.example.com/voicey/my-agent:git-sha \
  --min-agents 1 \
  --max-agents 4 \
  --profile agent-1x \
  --relay-url https://my-agent-results.fly.dev \
  --rollback-created \
  --yes
```

It first restores any ledgered carrier route, then deletes only an agent marked
created by voicey. Failed deployments are left checkpointed for inspection;
there is no speculative automatic resource deletion.
