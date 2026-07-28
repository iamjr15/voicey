# Results relay

Pipecat Cloud and LiveKit Cloud workers are ephemeral. They must not own the
only copy of a call record, transcript, result buffer, terminal event, or
recording state. The voicekit results relay is the authenticated protocol
between those workers and the user-owned durable companion.

This page describes protocol version `voicekit-results-relay/v1`. It is not a
conversation abstraction: Pipecat Flows and LiveKit workflows remain native to
their runtimes.

## Admission invariant

A worker must construct and `open()` its `RelayClient` before it starts
accepting jobs. `open()` performs a signed `GET /v1/ready` and requires all
three explicit response fields:

```json
{
  "ready": true,
  "protocol": "voicekit-results-relay/v1",
  "storage_ready": true
}
```

Missing, malformed, unauthenticated, or unavailable readiness fails with
`VK-REL-002`. The worker then remains closed to calls.

For each call, the relay must durably acknowledge `POST /v1/calls/begin`
before the worker exposes media or accepts the runtime job. The response
contains the current lease generation, an opaque server-signed fence, and the
next stream sequence. Browser reservations move to a dispatched worker through
the separately idempotent `POST /v1/calls/claim`; the new owner receives a new
generation and fence.

## Request authentication

Relay credentials use this protected printable form:

```text
vkr_<key-id>_<base64url-encoded-32-byte-or-stronger-secret>
```

The server accepts a current credential and, during bounded rotation, one
previous credential. A request has these headers:

```text
Authorization: VoicekitRelay <key-id>
X-Voicekit-Relay-Timestamp: <unix-seconds>
X-Voicekit-Relay-Nonce: <fresh-random-value>
X-Voicekit-Relay-Signature: <base64url-hmac-sha256>
```

The HMAC input is the UTF-8 encoding of these newline-separated values:

```text
timestamp
nonce
UPPERCASE_METHOD
url_path
hex_sha256_of_exact_body_bytes
```

The timestamp tolerance defaults to five minutes. A nonce is claimed in
durable storage before route execution and cannot be reused, including when a
previous rotation credential is used. Transport retry uses a new nonce and the
same request bytes, idempotency key, and sequence.

Credentials and fence tokens are secrets. They are injected through the cloud
platform secret store, never written to a generated image, manifest, log, test
report, or CLI checkpoint.

## Fencing and ordered updates

Every update uses:

```text
POST /v1/calls/<call-id>/updates
```

The body contains:

- a per-call `sequence`, starting at 1 with no gaps;
- a stable `idempotency_key`;
- the opaque server-issued `fence_token`;
- one typed operation and its strict payload;
- a timezone-aware `requested_at`, retained across transport retries.

The supported worker operations are lease renewal, timeline/transcript/tool/
latency observation, incremental result flush, provider-state update,
terminalization, and recording ready/failed state. Delivery claims, retention,
and stale-call recovery stay on the durable companion; cloud-worker
credentials do not receive those repository operations.

Carrier status and recording callbacks terminate on the companion when their
provider is explicitly configured. Carrier-native signatures are verified
before a status observation is persisted or recording bytes are downloaded.
Those observations never grant a lifecycle fence. If a worker lease expires,
recovery takes a new generation and reconciles the latest authenticated
observation; missing or non-terminal truth becomes `recovery_unknown`, never a
fabricated success.

The relay verifies the cryptographic fence and compares its owner/generation
with durable repository state before it reserves or applies an update.
Observation inserts repeat safely through an operation id bound to call id and
idempotency key. Lifecycle updates use repository compare-and-swap fencing.
Terminalization and recording-ready events return the exact immutable
persisted event in the acknowledgement.

The journal records a request as pending before repository mutation and moves
the stream cursor only after the durable acknowledgement is stored. If the
process exits between those points, the same request can be applied again:
observation inserts are at-most-once and lifecycle/event operations are
idempotent. Reusing a sequence or idempotency key with different bytes, or
sending a future sequence, fails with `VK-REL-005`.

## Rotation

1. Generate a new credential with a new key id.
2. Configure the relay with the new credential as current and the old
   credential as previous.
3. Deploy the relay and verify signed readiness with both credentials.
4. sync the new current credential to every cloud worker.
5. complete a rolling worker cutover and smoke call.
6. remove the previous credential from the relay after the maximum request,
   fence, and deployment-overlap window.

Changing secret bytes without changing the key id is rejected. A stale worker
with an otherwise valid old fence is rejected as soon as ownership advances.

## Local protocol verification

The SQLite journal is the deterministic local protocol and crash-injection
backend. The certified cloud companion uses the Postgres implementation in the
Fly topology; cloud deployment is not green merely because the SQLite suite
passes.

Run the local protocol suite:

```bash
uv run pytest --no-cov tests/unit/test_relay.py
```

It covers lost acknowledgements, exact-once observations, cross-client
handoff, terminal and recording acknowledgements, current/previous credential
rotation, nonce replay, signature/fence tampering, stale generations, sequence
gaps, idempotency conflicts, strict wire validation, and fail-closed startup.

The complete P3.5 gate additionally requires the Postgres repository and
object store, the Fly results-service companion, both cloud deployment
wrappers, external-relay validation, persistence invariants, and real cloud
smokes where credentials are available.

The process-level companion contract and environment are documented in
`docs/deploy/fly-companion.md`.
