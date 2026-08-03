# CLI guide

The voicey CLI is a guided rail over the engine. Bare `voicey` reports the
nearest project state and an exact next command. Every successful command also
prints `Next:`; expected failures print a stable code, fix, documentation link,
and next step.

## Guided setup

Run:

```bash
voicey init ./my-agent
```

The wizard asks at most five product questions, with no selected answer or
"recommended" badge:

1. recipe, with scratch listed last;
2. what the agent should do, used only as the scratch prompt seed;
3. channels as an explicit multi-select;
4. carrier only when phone is selected;
5. runtime and model axes, using factual price, latency, language, and runtime
   compatibility notes.

Advanced and automation inputs have flag twins:

```bash
voicey init ./my-agent \
  --name my-agent \
  --recipe scratch \
  --description "Help callers understand their order status." \
  --channels phone,web \
  --phone-provider twilio \
  --phone-number +14155550123 \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

`--yes` suppresses confirmations; it never supplies a product choice. Missing
choices fail as `VY-CLI-001` in a non-interactive process.

Setup validates each provider credential with a read-only authenticated request
at paste time. Valid keys are written atomically to owner-only `.env`, and
`.gitignore` is updated before the write. Values already injected into the
process take precedence and are not copied into `.env`. The generated
`.env.example` documents variable names only.

An interruption leaves a secret-free `init-checkpoint` in `voicey.jsonc`.
Resume it with:

```bash
voicey init ./my-agent --resume
```

The completed scratch project contains `agent.py`, native runtime `flow.py`,
typed `tools.py`, prompts, tests, and a runtime-extra `pyproject.toml`.
Pipecat emits a native `NodeConfig` entry; LiveKit emits a native
`livekit.agents.Agent` factory. There is no voicey conversation DSL.

`appointment-booking@1.0.0` is available on both runtimes. Its authored prompts,
calendar stub, and selected native flow are copied into the project; Pipecat
also receives the direct text/audio Evals. The scratch-description and
prompt-drafting questions are skipped because this recipe already owns those
sources. See the [recipe guide](recipes/appointment-booking.md).

Capabilities come from the installed build. Future runtimes, carriers, deploys,
and recipe variants are reported as unavailable until their numbered build
units ship; a dead-end choice is never silently accepted.

## Development and calls

```bash
voicey dev
voicey dev --phone --tunnel auto
voicey call +14155550199 --url https://public.example.test --yes
```

`dev` starts the selected production runtime. Pipecat phone mode probes the
public tunnel before temporarily changing the selected Twilio, Telnyx, Vobiz,
or Plivo route. LiveKit supervises its native worker and, with `--phone`,
temporarily provisions the selected carrier's SIP chain. Generic SIP
provisions only the LiveKit side and leaves the external PBX/carrier route
operator-managed. Exit and interruption restore the voicey-managed prior
route/SIP resources and environment/import state.
The public runtime/signaling listener binds to `127.0.0.1:<port>` and the
playground/admin listener binds separately to `127.0.0.1:<port + 1>`. For the
default `--port 7860`, open `http://127.0.0.1:7861`. A tunnel receives only the
public listener; records, recordings, and session issuance remain on the local
admin listener. LiveKit additionally reserves `<port + 2>` for native worker
health. `--no-open` suppresses browser launch without changing the listeners.

The playground is embedded in the installed wheel. It shows live transcript,
turn latency, runtime events, tool calls, captured data, and the exact durable
terminal payload. See the [playground guide](playground.md) for its security
and reload contracts.

Outbound creation is protected by the durable carrier intent ledger. Since it
can spend money, `call` requires interactive confirmation or `--yes`.

## Keys, numbers, and call results

```bash
voicey keys list
voicey keys add deepgram
voicey keys add livekit
voicey keys validate

voicey numbers list
voicey numbers buy US --area 415 --yes
voicey numbers point +14155550123 --url https://public.example.test --yes
voicey numbers restore <rollback-token> --yes
voicey numbers release +14155550123 --yes

voicey calls list
voicey calls list --undelivered
voicey calls show <call-id>
voicey calls redeliver <call-id-or-event-id> --yes
```

LiveKit project credentials are validated by an authenticated, read-only
room-list request and are collected in the same in-flow `.env` path. Key output
is masked. Number purchases, releases, production route changes,
restores, outbound calls, and result redelivery require explicit confirmation.
Call reads and redelivery use the same protected SQLite repository and immutable
terminal-event/outbox contract as the runtime.

## Docker deployment

Generate and validate the canonical self-host artifacts from an agent project:

```bash
voicey deploy docker --skip-smoke
docker compose -f compose.voicey.yaml up -d --build
```

The command refuses to overwrite conflicting artifacts, records `docker` in
the project manifest, validates the Compose model, and prints the explicit
number-cutover and smoke steps. An unpublished checkout additionally requires
`--engine-wheel /absolute/path/to/voicey-*.whl`.

Once HTTPS ingress and number routing are ready, a phone project can run the
paid endpoint-and-call smoke:

```bash
voicey deploy docker \
  --smoke https://voice.example.com \
  --to +15551234567 \
  --yes
```

`--skip-smoke` and `--smoke` are mutually exclusive. Web-only projects require
a real browser conversation instead of treating a health probe as speech
evidence. Storage topology, secret handling, proxy trust, drain, rolling
replacement, and recovery are documented in the
[Docker deployment guide](deploy/docker.md).

## Fly results companion

Deploy the user-owned durable companion for ephemeral cloud workers by making
every placement, database plan, volume, and bucket choice explicit:

```bash
voicey deploy fly \
  --app my-agent-results \
  --org my-fly-org \
  --region iad \
  --postgres-name my-agent-results-pg \
  --postgres-plan Basic \
  --postgres-volume-gb 10 \
  --bucket my-agent-results-objects \
  --yes
```

An unpublished checkout requires `--engine-wheel`. The command checkpoints
every Fly app, Managed Postgres, Tigris, attachment, secret, release, and smoke
step without putting secret values in the resource ledger. Pre-existing
unledgered resources require explicit `--adopt`; credential replacement
requires `--rotate-credentials`; destructive reverse rollback requires
`--rollback-created --yes`. Every successful deployment prints the matching
Pipecat Cloud or LiveKit Cloud command with its signed relay URL. See the
[Fly companion guide](deploy/fly-companion.md).

## Pipecat Cloud

Prepare the secret-free context, build and push the exact immutable image, then
deploy it:

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
  --prepare-only

docker build \
  -t registry.example.com/voicey/my-agent:git-sha \
  .voicey/deploy/pipecat-cloud/context
docker push registry.example.com/voicey/my-agent:git-sha

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
  --yes
```

The installed CLI requires the pre-pushed image. Phone projects also require
`--smoke-to <E.164>` unless `--skip-smoke` is explicit. Telnyx requires the
printed hosted-answer URL on its TeXML Application and
`--telnyx-texml-ready`. Existing agents require `--adopt`; destructive
created-only rollback is `--rollback-created --yes`. See the
[Pipecat Cloud guide](deploy/pipecat-cloud.md).

## LiveKit Cloud

Deploy a native LiveKit worker against the validated companion:

```bash
voicey deploy livekit-cloud \
  --agent my-agent \
  --project my-livekit-project \
  --region us-west \
  --relay-url https://my-agent-results.fly.dev \
  --yes
```

Phone projects require `VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID` and
`--smoke-to <E.164>` unless `--skip-smoke` is explicit. Existing agents require
`--agent-id <id> --adopt`. `--rollback --yes` deletes a first version created
by voicey or restores the exact ledgered previous version. See the
[LiveKit Cloud guide](deploy/livekit-cloud.md).

## Doctor

Run local preflight:

```bash
voicey doctor
voicey doctor --fix
voicey doctor --send-test
voicey doctor --json
```

Checks run concurrently and report `{description, ok, issues, advice}` for:
provider keys and webhook secret, pinned runtime, Python, `ffmpeg`, ports,
tunnel/WebSocket readiness, carrier authentication and inbound route, Twilio
trial/funding/geo/KYC constraints, LiveKit URL/token, signed results-receiver
POST, DLQ depth, clock skew, `.env` documentation drift, and disk space.

`--fix` is intentionally narrow: it adds secret ignore rules, creates the
owner-only local data directory, and generates a valid `whsec_` value when
absent. It does not buy numbers, alter routes, install software, or change
cloud resources.

## Recipe drift and engine upgrades

`voicey recipes update-check` is read-only and supports `--json`. It compares
the committed recipe baseline, current project source, and recipe source in the
installed engine. Human output shows changed paths, conflict status, explicit
AI-merge guidance, and the next command. JSON exposes paths and content digests
without source text.

`voicey upgrade --stable --yes` updates only voicey's `uv.lock` resolution;
`--pre` selects the canary channel. The command syncs the locked environment,
runs drift analysis through that new process, verifies that `pyproject.toml`
and recipe-owned source are unchanged, and restores the prior lock on failure.
Both modes accept `--json`.

The exact compatibility, merge, and recovery procedure is in the
[upgrading guide](upgrading.md).

## Structured output and automation

All read surfaces accept `--json`, including bare status, recipes, keys,
numbers, calls, and doctor. Confirmed upgrade also accepts `--json` for its
post-mutation report. JSON failures include `code`, `cause`, `detail`, `fix`,
`docs`, and `next_step`. Mutating commands use deterministic flags and `--yes`.

Use `voicey <command> --help` for the complete flag surface. Commands reserved
for later phases remain visible only while their capability is unavailable and
return `VY-CLI-005` with the exact capability status.

## Manual P1.8 verification

The automated matrix is in `tests/unit/test_cli*.py`. Two usability gates need
a person and are therefore never inferred from unit tests:

```bash
uv run voicey init /tmp/voicey-human-wizard
```

Complete every prompt without flags, verify no answer is selected initially,
interrupt once, resume, and reach the generated next step.

For broken-machine diagnosis, use the isolated fixture runbook in
[`GAPS.md`](GAPS.md); do not damage a working machine to exercise it.

See the complete [error catalog](errors.md).
