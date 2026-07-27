# CLI guide

The voicekit CLI is a guided rail over the engine. Bare `voicekit` reports the
nearest project state and an exact next command. Every successful command also
prints `Next:`; expected failures print a stable code, fix, documentation link,
and next step.

## Guided setup

Run:

```bash
voicekit init ./my-agent
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
voicekit init ./my-agent \
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
choices fail as `VK-CLI-001` in a non-interactive process.

Setup validates each provider credential with a read-only authenticated request
at paste time. Valid keys are written atomically to owner-only `.env`, and
`.gitignore` is updated before the write. Values already injected into the
process take precedence and are not copied into `.env`. The generated
`.env.example` documents variable names only.

An interruption leaves a secret-free `init-checkpoint` in `voicekit.jsonc`.
Resume it with:

```bash
voicekit init ./my-agent --resume
```

The completed scratch project is native Pipecat Flows Python: `agent.py`,
`flow.py`, typed `tools.py`, prompts, tests, and a pinned-extra `pyproject.toml`.
There is no voicekit conversation DSL.

P1.10 also enables `appointment-booking@1.0.0` for Pipecat. Its authored
prompts, calendar stub, native flow, and direct Pipecat text/audio Evals are
copied into the project; the scratch-description and prompt-drafting questions
are skipped because this recipe already owns those sources. See the
[recipe guide](recipes/appointment-booking.md).

Capabilities come from the installed build. Future runtimes, carriers, deploys,
and recipe variants are reported as unavailable until their numbered build
units ship; a dead-end choice is never silently accepted.

## Development and calls

```bash
voicekit dev
voicekit dev --phone --tunnel auto
voicekit call +14155550199 --url https://public.example.test --yes
```

`dev` starts the actual Pipecat host and supervised Uvicorn server. Phone mode
probes the public tunnel before temporarily changing the selected Twilio route.
Exit and interruption restore the prior route and environment/import state.
The public runtime/signaling listener binds to `127.0.0.1:<port>` and the
playground/admin listener binds separately to `127.0.0.1:<port + 1>`. For the
default `--port 7860`, open `http://127.0.0.1:7861`. A tunnel receives only the
public listener; records, recordings, and session issuance remain on the local
admin listener. `--no-open` suppresses the browser launch without changing
either listener.

The playground is embedded in the installed wheel. It shows live transcript,
turn latency, runtime events, tool calls, captured data, and the exact durable
terminal payload. See the [playground guide](playground.md) for its security
and reload contracts.

Outbound creation is protected by the durable carrier intent ledger. Since it
can spend money, `call` requires interactive confirmation or `--yes`.

## Keys, numbers, and call results

```bash
voicekit keys list
voicekit keys add deepgram
voicekit keys validate

voicekit numbers list
voicekit numbers buy US --area 415 --yes
voicekit numbers point +14155550123 --url https://public.example.test --yes
voicekit numbers restore <rollback-token> --yes
voicekit numbers release +14155550123 --yes

voicekit calls list
voicekit calls list --undelivered
voicekit calls show <call-id>
voicekit calls redeliver <call-id-or-event-id> --yes
```

Key output is masked. Number purchases, releases, production route changes,
restores, outbound calls, and result redelivery require explicit confirmation.
Call reads and redelivery use the same protected SQLite repository and immutable
terminal-event/outbox contract as the runtime.

## Doctor

Run local preflight:

```bash
voicekit doctor
voicekit doctor --fix
voicekit doctor --send-test
voicekit doctor --json
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

## Structured output and automation

All read surfaces accept `--json`, including bare status, recipes, keys,
numbers, calls, and doctor. JSON failures include `code`, `cause`, `detail`,
`fix`, `docs`, and `next_step`. Mutating commands use deterministic flags and
`--yes`.

Use `voicekit <command> --help` for the complete flag surface. Commands reserved
for later phases remain visible but return `VK-CLI-005` with the exact
capability status.

## Manual P1.8 verification

The automated matrix is in `tests/unit/test_cli*.py`. Two usability gates need
a person and are therefore never inferred from unit tests:

```bash
uv run voicekit init /tmp/voicekit-human-wizard
```

Complete every prompt without flags, verify no answer is selected initially,
interrupt once, resume, and reach the generated next step.

For broken-machine diagnosis, use the isolated fixture runbook in
[`GAPS.md`](GAPS.md); do not damage a working machine to exercise it.

See the complete [error catalog](errors.md).
