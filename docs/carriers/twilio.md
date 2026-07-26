# Twilio

Twilio is the P1 reference carrier. The Pipecat path uses bidirectional Media
Streams and the LiveKit SIP path remains capability-disabled until its P2
provisioner and certification suite land.

## Install and credentials

```bash
uv sync --extra twilio
```

The pinned SDK is `twilio==9.10.9`. The adapter reads
`TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`; applications may also pass both
explicitly. Credentials are never written into the manifest, routing ledger,
TwiML, callback paths, or call records.

```python
from voicekit.telephony import load_adapter

adapter = load_adapter(
    "twilio",
    expected_public_base="https://voice.example.com",
)
```

`expected_public_base` is mandatory for webhook verification. Voicekit trusts
forwarded scheme/host fields only from configured proxy addresses and then
requires the reconstructed origin to equal this base. WebSocket upgrades are
verified against the exact `wss://` URL with empty parameters. HTTP form and
JSON `bodySHA256` requests use Twilio's installed `RequestValidator`; voicekit
does not implement a second signature algorithm.

## Capabilities

| Capability | P1 Pipecat path |
|---|---|
| Inbound / outbound | enabled |
| Async AMD | enabled; media waits for the AMD callback |
| DTMF receive / send | enabled |
| Transfer | cold (`Call.update` to `<Dial>`) |
| Recording | dual-channel callback + authenticated engine ingestion |
| Native outbound idempotency | unavailable |
| LiveKit SIP | disabled until P2 |

Twilio media is mono G.711 μ-law at 8 kHz. The certification rig covers
μ-law↔linear conversion, 8↔16 kHz resampling, 20 ms pacing, three-frame jitter
tolerance, playback marks, interruption `clear`, DTMF, and a 440 Hz energy and
frequency loopback. The codec helpers do not depend on Python's removed
`audioop` module.

The adapter declares Twilio's US1, IE1, and AU1 regions. Choose the region and
runtime deployment closest to callers, then use the P1 audio latency gate;
the declaration is not a promise that a distant deployment meets the product's
latency budget. New accounts commonly begin at one outbound call per second,
so higher-volume deployments must have their carrier CPS limit reviewed.

## Temporary inbound routing

```python
from voicekit.telephony import PipecatTarget

target = PipecatTarget(https_base="https://voice.example.com")
token = adapter.point_inbound("+14155550100", target)
try:
    run_development_session()
finally:
    adapter.restore(token)
```

Before changing Twilio, voicekit writes a FULL-durability SQLite snapshot of:

- voice URL/method and fallback URL/method;
- status callback URL/method;
- voice application SID;
- trunk SID.

The last two fields take precedence over a voice URL and are explicitly
cleared while the temporary route is active. Restore is compare-and-swap: the
current carrier settings must still equal what voicekit wrote. A concurrent
manual or automated change produces `VK-TEL-006` and is never overwritten.
`adapter.recover_routes()` reconciles and restores prepared/applied/ambiguous
tokens after a crashed development process.

## Outbound safety

Twilio `Calls.create` has no native idempotency key. Voicekit therefore writes
an `intent_<id>` row before the API call and embeds that id in the status
callback path. A definite Twilio 4xx becomes a rejected intent. A network
failure, 5xx, malformed acceptance response, or crash window becomes
`VK-TEL-007`; the create request is not retried.

```python
call_sid = adapter.start_call(
    "+14155550100",
    "+14155550101",
    target,
    intent_id="intent_customer_123_attempt_1",
    record=True,
)
```

Use `adapter.reconcile_outbound(intent_id)` after an ambiguous outcome. A
callback binds the provider call SID directly. The fallback sweep binds only
one unique from/to/time candidate; zero or multiple candidates remain visibly
ambiguous and require operator review.

Outbound calls use inline TwiML capped at 4 KB and status callbacks for
initiated, ringing, answered, and completed. Stream metadata is nested
`<Parameter>` data—never a WebSocket query string. Async AMD initially serves
hold TwiML, then `resume_after_amd()` connects a human or applies the configured
machine policy, avoiding two competing audio consumers.

## Call controls and recordings

- `send_dtmf(call_sid, "12#")` redirects the call to `<Play digits>`.
- `cold_transfer(call_sid, "+14155550122")` redirects to `<Dial
  answerOnBridge>`. Redirecting exits `<Connect>` and ends the Media Stream.
- `hangup(call_sid)` completes the carrier leg.

Warm transfer needs the P3 conference bridge and is intentionally absent from
P1 capabilities.

Recording callbacks are signature-verified like every carrier callback.
Voicekit ignores callback media URLs for ingestion and constructs the recording
resource URL from the validated account and recording SIDs. It downloads with
HTTP Basic authentication, accepts only known audio content types, enforces a
100 MiB default limit, and writes through the protected `ArtifactStore`. The
terminal event remains immutable with a pending engine recording reference;
successful ingestion creates the separate `call.recording.ready` event.

## Troubleshooting

| Code | Meaning / action |
|---|---|
| `VK-TEL-001` | Install `voicekit[twilio]`; check duplicate third-party entry points |
| `VK-TEL-002` | Correct credentials, HTTPS target, E.164 number, timeout, or DTMF |
| `VK-TEL-003` | Owned/available number lookup was empty or non-unique |
| `VK-TEL-004` | Twilio definitively rejected the request; use the safe HTTP/code in the error |
| `VK-TEL-005` | Stop mutations and repair the protected telephony ledger |
| `VK-TEL-006` | Routing is ambiguous or changed concurrently; inspect before manual restore |
| `VK-TEL-007` | Reconcile the outbound intent; never place a speculative retry |
| `VK-TEL-008` | Reject an unknown/incomplete callback shape |
| `VK-TEL-009` | Check recording SID, auth, availability, type, and size |
| `VK-TEL-010` | Reject an invalid Media Streams frame/codec event |
| `VK-TEL-011` | Carrier availability was indeterminate; reconcile mutations first |

Trial accounts may call only verified destinations and play a trial preamble.
Unfunded accounts and countries with address/bundle/identity requirements must
be resolved in Twilio before certification. `voicekit doctor` will surface
these account facts when the P1 CLI unit lands.

## Verification

Local carrier and media certification:

```bash
uv run pytest --no-cov \
  tests/certification/test_twilio_adapter.py \
  tests/certification/test_twilio_media.py
```

The no-charge, account-readiness, route mutation, paid PSTN, recording, DTMF,
transfer, and physical-handset commands are recorded verbatim in
[`docs/GAPS.md`](../GAPS.md). They remain pending because no Twilio credentials
or live numbers are available in this workspace.

Next step: start the P1 Pipecat public listener, then run the guarded route and
PSTN commands from `docs/GAPS.md`.
