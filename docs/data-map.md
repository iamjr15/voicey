# Stored-data map and retention

Voicey stores call data only on infrastructure selected and owned by the
operator. It does not send telemetry or call content to a voicey-operated
service. Model, speech, carrier, and deployment providers still process data
under their own contracts.

## Data map

| Data | Location | May contain PII | Secret policy | Retention owner |
|---|---|---:|---|---|
| Production logs | Process stdout/stderr and operator log sink | No | Redacted at every level | Operator log policy |
| Debug logs | Local process output | Yes | Redacted at every level | Operator; debug is off in production |
| Call lifecycle, transcript, tool observations, latency | SQLite for Docker/self-host; Postgres for Fly/Railway companions | Yes | Secret-shaped values scrubbed before persistence | `results.purge_after_days` |
| Immutable result events, outbox, dead letters | Same repository as the call lifecycle | After configured field redaction | Never contains credentials | `results.purge_after_days` |
| Recordings | Protected local artifact directory for Docker; private object storage for managed companions | Yes | Carrier download credentials and signed carrier URLs are not stored as public result URLs | `results.purge_after_days` |
| Backups and artifact-deletion queue | Deployment-owned local or object storage plus repository | Yes | No credentials in backup names or payload metadata | `results.purge_after_days` |
| Results webhooks | Operator-configured HTTPS receiver | Only selected, non-redacted result fields | Signed with current/previous `whsec_` secret; secret is never in the body | Receiver policy |
| Prometheus | Dedicated loopback listener by default | No | No payloads or credentials in labels | Metrics-system policy |
| OTLP traces | Optional operator-configured collector | No | Auth loaded from the named environment variable and excluded from spans | Trace-system policy |
| Browser session credentials | Browser memory | Stable call/session ids only | Short-lived and least-privilege; provider API keys never enter the browser | Expires automatically |

Provider audio, transcripts, prompts, and tool traffic may also pass through the
configured Deepgram, Anthropic, Cartesia, Pipecat transport, LiveKit, carrier,
or SIP systems. Voicey cannot apply its repository purge transaction to
provider-owned copies. Configure those providers' retention and training
settings separately.

## Purge boundary

`results.purge_after_days` covers the call row and normalized observations,
terminal and non-terminal events, delivery attempts, dead letters, recordings,
artifact-deletion work, and engine-owned backups. SQLite purge checkpoints the
WAL after deletion. Managed storage first commits the database purge and an
artifact-deletion work item, then idempotently removes the object; retrying
cannot resurrect a call.

Changing the retention value affects new calls. Treat existing legal holds,
exports, receiver copies, log sinks, and provider copies as separate operator
responsibilities.

## Access boundary

- Local repositories and artifacts use owner-only paths.
- Managed repositories and buckets remain private and are accessed by the
  results companion.
- Recording reads require the current or previous Results webhook secret and
  return `Cache-Control: private, no-store`.
- Results receivers must verify the raw body before parsing it and deduplicate
  by `webhook-id`.
- Public carrier and browser ingress never exposes the protected admin,
  Prometheus, database, or artifact-store listener.

Next: choose and document the retention period in
[Configuration](configuration.md), then test deletion in the selected
[deployment target](index.md#deploy-and-operate).
