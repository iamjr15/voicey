# Twilio

Twilio is the reference carrier. The Pipecat path uses bidirectional Media
Streams; the LiveKit path uses fully API-managed Elastic SIP trunks.

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

| Capability | Pipecat path | LiveKit path |
|---|---|---|
| Inbound / outbound | enabled | enabled through Elastic SIP |
| Async AMD | enabled; media waits for callback | carrier/SIP disposition |
| DTMF receive / send | enabled | native room event + LiveKit send tool |
| Transfer | cold; P3 adds conference warm transfer | cold SIP REFER + native warm-transfer task |
| Recording | dual-channel callback + authenticated ingestion | secure trunk recording + native session recording |
| Native outbound idempotency | unavailable; durable intent fence | unavailable; durable intent fence |

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
the Pipecat path until P3. It is already native on the LiveKit path.

Recording callbacks are signature-verified like every carrier callback.
For inbound Pipecat calls, voicekit first reserves the engine call and then
uses Twilio's live-call Recordings resource to start one dual-channel
recording with `completed` and `absent` callbacks. A read-before-write check
reuses one existing recording and fails closed if the carrier reports more
than one.
Voicekit ignores callback media URLs for ingestion and constructs the recording
resource URL from the validated account and recording SIDs. It downloads with
HTTP Basic authentication, accepts only known audio content types, enforces a
100 MiB default limit, and writes through the protected `ArtifactStore`. The
terminal event remains immutable with a pending engine recording reference;
successful ingestion creates the separate `call.recording.ready` event.
The engine URL is fetched with the current or previous result webhook secret
as a bearer; it never contains the carrier URL or a credential.

## LiveKit Elastic SIP provisioning

`TwilioLiveKitSipProvisioner` treats the carrier and LiveKit changes as one
FULL-durability ledger operation. It creates or reuses, in order:

1. a number-scoped LiveKit inbound trunk with no credentials;
2. an individual-room dispatch rule with explicit `RoomAgentDispatch`;
3. a LiveKit outbound trunk to the Twilio domain with credentials and TLS;
4. a secure Twilio Elastic SIP trunk;
5. a TLS origination URL to the LiveKit SIP host;
6. a Twilio credential list, credential, and trunk binding;
7. the Twilio phone-number binding.

The authentication asymmetry is required by Twilio: Elastic SIP does not
support username/password authentication for traffic originating at Twilio.
Credentials therefore belong only on the LiveKit outbound trunk that terminates
into Twilio. The Twilio origination URI ends in `;transport=tls`, the LiveKit
outbound transport is TLS, and both LiveKit trunks allow encrypted media.

Every confirmed created resource is appended to the ledger before the next
mutation. A definitive failure rolls back in reverse order and restores the
complete phone-number route snapshot. An indeterminate network outcome is
fenced as `ambiguous` and never destructively guessed.

Outbound `create_sip_participant(wait_until_answered=True)` is also preceded by
a durable intent. `SipCallError.sip_status_code` maps a definitive SIP rejection
to `carrier_error`; an unknown outcome becomes `VK-TEL-007` and is not retried.

When `phone.record` is enabled, a newly managed Twilio trunk is configured for
dual-channel record-from-answer. Voicekit refuses to silently change the
recording mode of an existing trunk because that can affect unrelated traffic.

Twilio's Elastic SIP recording resource has no completion callback field—it
supports only `mode` and `trim`. LiveKit provides the Twilio CA Call SID through
the built-in `sip.twilio.callSid` participant attribute. After the terminal
event, voicekit polls Core Recordings by that CA SID, requires exactly one
completed item whose source is `Trunking`, downloads it with Basic
authentication, and emits the same `call.recording.ready` event used by the
Pipecat callback path. A timeout stays visibly pending for retry/recovery.

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

LiveKit SIP protocol, TLS/auth, idempotency, rollback, native DTMF/transfer,
terminal mapping, recording policy, and actual-SIGKILL evidence:

```bash
uv run pytest --no-cov \
  tests/unit/test_livekit_runtime.py \
  tests/integration/test_livekit_sigkill.py \
  tests/certification/test_twilio_livekit_sip.py
```

The no-charge, account-readiness, route mutation, paid PSTN, recording, DTMF,
transfer, and physical-handset commands are recorded verbatim in
[`docs/GAPS.md`](../GAPS.md). They remain pending because no Twilio credentials
or live numbers are available in this workspace.

Next step: start the selected runtime, then run its guarded route and PSTN
commands from `docs/GAPS.md`.
