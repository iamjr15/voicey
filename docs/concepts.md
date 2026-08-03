# Concepts and ownership boundaries

Voicey is an engine around native voice-agent runtimes, not a conversation
framework. A project chooses Pipecat or LiveKit once; shared configuration,
tools, results, testing, telephony, and operations remain portable.

## What belongs where

| Owner | Owns | Does not own |
|---|---|---|
| Your project | persona, prompts, native flow/workflow, typed business tools, policy, result fields | carrier retries, lifecycle fencing, webhook delivery, deployment orchestration |
| Voicey engine | config validation, runtime bootstrap, browser rail, carrier adapters, durable call record, exactly-once terminal event, testing, deploy/drain, observability | business truth or a custom conversation DSL |
| Pipecat / LiveKit | media session, STT/LLM/TTS integration, native turn handling, native workflow/tool calls | project storage and cross-runtime result delivery |
| Carrier / SIP provider | PSTN number, call control, authenticated callbacks, media edge | agent business logic or durable terminal ownership |
| Your receiver | business-side event deduplication and downstream action | retrying or rewriting the immutable engine event |

There is no voicey flow DSL and no MCP product surface. Pipecat projects use
`pipecat.flows` directly. LiveKit projects subclass or return
`livekit.agents.Agent` directly. Tools are typed Python functions or explicit
HTTP endpoints.

## One call through the system

1. Voicey durably reserves capacity, creates the call row, and issues an
   owner/generation fence before returning answer XML, accepting media, or
   issuing a browser token.
2. The selected native runtime owns media, turns, provider calls, and authored
   workflow transitions.
3. Voicey records bounded observations and incrementally flushes transcript
   and structured result state behind the runtime-neutral repository protocol.
4. The current fenced owner terminalizes once. Call state, immutable terminal
   bytes, and the delivery outbox row commit in one transaction.
5. Delivery retries the same event id and bytes with a fresh Standard Webhooks
   timestamp/signature until acknowledged or visibly dead-lettered.

Provider completion, media disconnect, process crash, and deploy drain are all
inputs to that lifecycle; none may create a second terminal event.

## Shared config, native mapping

`voicey.Agent` is intentionally thin. Runtime parity tests map each shared
field to a pinned native mechanism. Examples:

- `models` selects native provider services and explicit axis failover;
- `limits` maps duration, silence, concurrency, and tool bounds;
- `behavior` maps interruption, voicemail, transfer, DTMF, and language
  behavior; and
- `results` controls the shared durable webhook/pull contract, not a
  runtime-specific callback.

The checked-in [config matrix](runtime-config-matrix.json) names every mapping.
A behavior that cannot be implemented natively must be capability-gated and
documented; it is never silently emulated or downgraded.

## Storage and deployment

Self-hosted Docker uses SQLite WAL/FULL and protected local artifacts on one
host. Fly and Railway companions use managed Postgres plus private object
storage. Ephemeral Pipecat Cloud and LiveKit Cloud workers use the signed,
fenced results relay; they never pretend an ephemeral filesystem is durable.

Deployments close admission before drain. Already fenced calls may finish on
the old generation, while stale owners are prevented from appending or
terminalizing after takeover.

## Evidence levels

Local automated evidence proves code, protocol, native symbols, rollback,
fencing, provider-mocked media, and artifact construction. Credentials, funded
PSTN calls, public cloud resources, 24 hours of wall time, microphones, and
physical handsets are separate gates. [GAPS.md](GAPS.md) records exact commands
without promoting unavailable evidence.

Next: follow a [Pipecat](quickstart-pipecat.md) or
[LiveKit](quickstart-livekit.md) quickstart, then read the chosen runtime page.
