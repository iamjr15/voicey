# Pipecat runtime

Voicekit pins Pipecat to `pipecat-ai==1.6.0` and uses only its current APIs.
Conversation logic stays in native `pipecat.flows` `NodeConfig` code. Voicekit
does not translate a custom state machine or flow DSL.

## Install

For browser-only development:

```bash
uv sync --extra pipecat
```

For Twilio phone calls:

```bash
uv sync --extra pipecat --extra twilio
```

The default model set reads `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, and
`CARTESIA_API_KEY`. Configured fallbacks read the key declared by their
provider-catalog entry. Secrets remain environment values and are never copied
into the manifest, call record, or media handshake.

## Native flow entrypoint

`Agent.flow` is an import reference in `module:attribute` form. The attribute
may be a native `NodeConfig`, a zero-argument factory, or a factory accepting
one native `FlowManager`. Sync and async factories are supported.

```python
from pipecat.flows import FlowManager, NodeConfig


def entry(flow_manager: FlowManager) -> NodeConfig:
    del flow_manager
    return NodeConfig(
        name="entry",
        role_message="You are the clinic receptionist.",
        task_messages=[
            {
                "role": "developer",
                "content": "Greet the caller and help book an appointment.",
            }
        ],
        respond_immediately=True,
    )
```

Tools declared with `@voicekit.tool` become native
`FlowsFunctionSchema` global functions. Each handler returns Pipecat's
`(result, next_node)` pair. `say_while_running`, timeout-bounded execution,
call-local result context, and protected tool observations are applied by the
adapter. Transfers and language fallback are native global flow functions
only when their corresponding config is enabled.

## Host and transports

`PipecatHost` owns one long-lived `WorkerRunner` using
`runner.run(auto_end=False)`. Every admitted browser or phone call gets one
`PipelineWorker`; a zero-call process stays ready for the next call.

```python
from voicekit.runtimes.pipecat import PipecatHost, PipecatHostSettings

host = PipecatHost(
    agent=agent,
    repository=repository,
    settings=PipecatHostSettings.from_env("https://voice.example.com"),
    twilio=twilio_adapter,
)
app = host.app
```

The FastAPI application exposes:

| Route | Purpose | Boundary |
|---|---|---|
| `GET /health` | runner and active-call health | deployment probe |
| `POST /twilio/answer` | reserve storage/capacity and return TwiML | Twilio signature required |
| `WS /twilio/media` | bidirectional phone audio | Twilio signature plus opaque reservation token |
| `POST /twilio/events[/{intent_id}]` | provider lifecycle events | Twilio signature required |
| `POST /twilio/amd` | async answering-machine disposition | Twilio signature required |
| `POST /api/offer` | SmallWebRTC offer/renegotiation | public web signaling |
| `PATCH /api/offer` | SmallWebRTC ICE candidates | public web signaling |

The P1.9 playground unit adds the short-lived web session tokens, rate limits,
and separate admin listener required for deployed public web signaling. Until
that unit lands, deploy only the signed phone routes; do not expose the web
offer route as an unauthenticated production endpoint.

Twilio uses `FastAPIWebsocketTransport` with
`TwilioFrameSerializer.InputParams(twilio_sample_rate=8000,
sample_rate=8000, auto_hang_up=True)`. The transport remains mono 8 kHz
end-to-end. Browser calls use `SmallWebRTCTransport` at 16 kHz.

## Pipeline and policy mapping

The per-call cascade is:

```text
transport input → DTMF policy → STT → user aggregator → LLM
                → TTS → transport output → assistant aggregator
```

It uses `LLMContext` plus `LLMContextAggregatorPair`,
`LLMUserAggregatorParams` for VAD/idle/interruption policy,
`ServiceSwitcher`/`LLMSwitcher` for configured fallbacks, and a duration task
that queues `EndFrame`. RTVI and turn tracking are enabled through
`PipelineWorker`; they are not manually inserted as duplicate processors.

Every shared `Models.fallbacks`, `Limits`, `Behavior`, and
`Voice.fallback_language` field has a named native mechanism and test in
[`runtime-config-matrix.json`](../runtime-config-matrix.json). P2 fills the
LiveKit column and enforces cross-runtime parity.

## Durable call lifecycle

Capacity is reserved atomically before TwiML, WebSocket acceptance, or an SDP
answer becomes visible. The reservation token binds the later media connection
to that exact call. The runtime then:

1. creates the protected active call row and fencing lease;
2. heartbeats the lease while the worker runs;
3. incrementally persists transcript turns, latency, timeline, and tool calls;
4. flushes the call-local results buffer;
5. atomically persists exactly one terminal event and outbox delivery;
6. releases capacity only after terminal persistence succeeds.

Setup, provider, worker, carrier, duration, silence, transfer, voicemail, and
caller/agent termination paths all produce cataloged terminal reasons.
Persistence failure is fail-closed and does not silently free the slot or
discard the fence.

## Verification

Run the local runtime, provider-construction, host, and tool-discovery suite:

```bash
uv run pytest --no-cov \
  tests/unit/test_pipecat_runtime.py \
  tests/unit/test_pipecat_host.py \
  tests/unit/test_pipecat_providers.py \
  tests/unit/test_tools.py
```

The tests include an actual long-lived `WorkerRunner`/`PipelineWorker`
completion, native flow initialization and tool execution, Twilio WebSocket
reservation claiming, SmallWebRTC session startup, field-by-field config
mapping, failover, 8 kHz serializer settings, incremental observations, and
fenced terminal persistence. External PSTN evidence remains separately tracked
in [`docs/GAPS.md`](../GAPS.md).

Next step: run `voicekit doctor`, then use `voicekit dev` for the guided host
startup.
