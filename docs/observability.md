# Observability and call records

Voicekit separates operator logs from protected call records. Logs are safe for
central collection at the default level; call records intentionally contain
transcripts and telephony identifiers and remain in protected storage.

## Logging

Production uses newline-delimited JSON:

```python
from voicekit.obs import call_context, configure_logging, get_logger

configure_logging(format="json", level="info")

with call_context(
    "call_01H...",
    config_hash=agent.config_hash,
    runtime=agent.runtime,
):
    get_logger(component="pipecat").info("call.connected", transport="websocket")
```

`call_context` uses context variables, so concurrent calls and worker-thread
context copies cannot leak correlation fields into each other. Development may
select `format="pretty"` for the same structured events.

### Level policy

| Level | Intended content |
|---|---|
| `debug` | Local diagnostics; PII fields may be present, so do not enable in production |
| `info` | Lifecycle names, stable ids, counts, states, and durations; no PII |
| `warning` | Recoverable degradation with a stable `VK-*` code and safe metadata |
| `error` / `critical` | Failed operation and operator action; no raw customer data |

At info and higher, known PII fields are replaced with `[REDACTED]`, including
phone numbers, email addresses, transcript/utterance text, customer names,
recording locations, and tool arguments/results. Phone and email patterns are
also removed from free-form event strings. Secret-shaped keys and values are
redacted at every level.

Application code must still use stable event names rather than interpolating
customer content into the event string. `debug` output is local-sensitive
output and is never a substitute for the protected call record.

## Latency series

Every measurement is associated with a runtime turn id and one-based turn
index. Supported metrics are:

- `stt_partial`: audio start to first useful partial transcript;
- `stt_final`: audio start to final transcript;
- `llm_ttft`: final user turn to first model token;
- `tts_ttfb`: text submission to first synthesized audio;
- `e2e`: user endpoint to first audible assistant response.

`LatencySeries` validates finite non-negative durations, preserves insertion
order, filters by turn id, and computes deterministic nearest-rank p50/p95/max
summaries. The same samples power playground badges, simulation budgets, smoke
reports, and later Prometheus histograms.

## Protected call records

`SQLiteCallRecordStore` is the Docker/self-host call-record foundation. It uses:

- a private `0700` data directory and `0600` database;
- SQLite WAL mode, foreign keys, a 5-second busy timeout, and
  `synchronous=FULL`;
- one serialized writer connection and `BEGIN IMMEDIATE` transactions;
- a versioned schema and fail-closed rejection of unknown versions;
- normalized, ordered tables for timeline events, transcript turns, tool
  observations, and latency samples.

The lifecycle row is created before a call becomes externally visible. Runtime
observers then append transcript, tool, timeline, and latency data
incrementally. Tool observations contain structured errors, never stack traces.
Secret-shaped values are scrubbed before persistence while PII is retained for
the configured retention window.

P1.3 extends this schema and transaction boundary with terminal-event CAS,
fencing leases, immutable result envelopes, delivery outbox, dead letters, and
purge coverage. Code must not mark a call terminal outside that atomic
repository operation.

## Data boundary

| Surface | May contain PII | May contain secrets | Protection |
|---|---:|---:|---|
| Info-or-higher logs | No | No | Structured redaction and leak tests |
| Debug logs | Yes | No | Local-sensitive; disabled in production |
| Call database / WAL | Yes | No | Private path; configured retention |
| Results webhook | After configured field redaction only | No | Standard Webhooks signature |

Next step after diagnosing a call: use `voicekit calls show <call-id>` once the
P1.3 pull surface is installed.

## Prometheus

Prometheus export is explicit and disabled by default:

```python
from voicekit import Observability

observability = Observability(prometheus_enabled=True)
```

Both runtime hosts then serve a dedicated
`http://127.0.0.1:9464/metrics` listener. Configure
`prometheus_bind`, `prometheus_port`, and `prometheus_path` only when the
collector topology requires it. Do not route the endpoint through public
carrier or browser ingress. A Docker collector can share the application
network and use `prometheus_bind="0.0.0.0"` without publishing the port. The
Fly companion generator does this automatically and emits Fly's native
`[metrics]` stanza.

The stable metric surface is:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `voicekit_active_calls` | gauge | `runtime`, `agent` | currently admitted process-local calls |
| `voicekit_calls_total` | counter | `runtime`, `agent` | durably admitted calls |
| `voicekit_errors_total` | counter | `runtime`, `agent`, `code` | failures by bounded `VK-*` catalog code |
| `voicekit_results_dlq_depth` | gauge | `runtime`, `agent` | current durable dead-letter count |
| `voicekit_turn_latency_ms` | histogram | `runtime`, `agent`, `metric` | STT/LLM/TTS/e2e latency |

Call and error rates are derived without another unbounded metric:

```promql
rate(voicekit_calls_total[5m])
rate(voicekit_errors_total[5m])
```

No call id, telephone number, transcript, tool arguments, or result value is a
metric label.

## OTLP tracing

One configuration line enables batched OTLP/HTTP protobuf traces:

```python
observability = Observability(
    otlp_endpoint="https://collector.example/v1/traces",
)
```

Voicekit uses the pinned OpenTelemetry Python SDK and HTTP exporter. It creates
one server-kind `voicekit.call` root span and `voicekit.turn` /
`voicekit.tool` child spans. Attributes are bounded to stable ids, runtime,
channel, direction, provider, role, tool name, status, and duration. Protected
payloads and exception messages are excluded.

For authenticated collectors, keep the header value in the environment:

```python
observability = Observability(
    otlp_endpoint="https://collector.example/v1/traces",
    otlp_headers_env="VOICEKIT_OTLP_HEADERS",
)
```

`VOICEKIT_OTLP_HEADERS` uses `name=value,name2=value2` syntax. It is parsed
only while constructing the exporter and is never emitted. Exporters are
initialized before admission, recreated safely in LiveKit job processes, and
force-flushed during graceful shutdown.

Run the real loopback wire gate:

```bash
uv run python tests/verification/run_p4_observability_gate.py
```

It starts actual Prometheus and OTLP HTTP listeners for both runtime labels,
checks active/terminal/error/DLQ/latency samples, receives protobuf spans, and
scans both outputs for protected transcript and tool payloads.
