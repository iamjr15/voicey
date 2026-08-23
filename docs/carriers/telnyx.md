# Telnyx

Telnyx is certified through both product paths. Pipecat uses the Voice API
(Call Control JSON or TeXML) with bidirectional RTP-in-JSON streaming. LiveKit
uses an API-managed Telnyx FQDN/credential SIP connection. Voicey does not
silently substitute one path for the other.

## Install and credentials

```bash
uv sync --extra telnyx
```

The wizard verifies this extra through its installed `cryptography`
dependency. Voicey's HTTP integration does not require a top-level Telnyx SDK
module.

The Pipecat path requires:

- `TELNYX_API_KEY`;
- `TELNYX_PUBLIC_KEY`, the 32-byte webhook Ed25519 public key in hex or base64;
- `TELNYX_CONNECTION_ID`, the Voice API or TeXML connection assigned to the
  number.

The LiveKit path additionally requires:

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`;
- `VOICEY_LIVEKIT_SIP_URI`, such as
  `sip:project-id.sip.livekit.cloud`;
- `VOICEY_TELNYX_SIP_USERNAME` and
  `VOICEY_TELNYX_SIP_PASSWORD`.

The CLI writes these to the protected `.env`, never to `voicey.jsonc`.
`voicey doctor` performs the balance, number-ownership, connection-route,
LiveKit-project, and managed-SIP-resource checks.

## Capabilities

| Capability | Pipecat path | LiveKit path |
|---|---|---|
| Inbound / outbound | Voice API or TeXML | FQDN/credential SIP |
| AMD | native asynchronous Call Control event | carrier/SIP disposition |
| DTMF receive / send | signed webhook + native action | native room event + LiveKit send tool |
| Transfer | native cold transfer | cold SIP REFER + native warm-transfer task |
| Recording | signed saved/failed callback + artifact ingestion | carrier/LiveKit recording policy |
| Outbound idempotency | native `command_id` plus durable intent | durable SIP intent fence |

The certified media profile is PCMU at 8 kHz, 20 ms frames. The runtime rejects
a different negotiated encoding instead of decoding it optimistically. The
offline rig covers RTP packet serialization, pacing, jitter, `clear`, DTMF, and
tone loopback through the installed Pipecat `TelnyxFrameSerializer`.

Telnyx exposes the following anchor-site choices: latency-based selection,
Chicago, Ashburn, San Jose, Sydney, Amsterdam, London, Toronto, Vancouver, and
Frankfurt. Use `Latency` unless a measured deployment topology requires a
specific site. Carrier location is not a substitute for the end-to-end latency
gate.

## Signed callbacks and WebSocket admission

Voice API and TeXML callbacks are accepted only after Ed25519 verification of
the exact raw `{timestamp}|{body}` bytes. Timestamps outside the configured
five-minute replay/future window fail closed. Unknown events and malformed
payloads return `VY-TEL-008`.

Telnyx media WebSocket upgrades do not carry that HTTP webhook signature.
Voicey therefore reserves the call before answering and places a
cryptographically opaque, one-use, short-lived capability in the stream path.
The upgrade must claim that exact capability, and a second claim is rejected.
The capability is not a carrier or provider credential.

## Call Control and TeXML

`start_call()` persists an intent before `POST /v2/calls`, passes the same
identifier as Telnyx `command_id`, and embeds it in base64 `client_state`.
A definite 4xx rejects the intent. A timeout, 5xx, invalid response, or crash
window becomes `VY-TEL-007` and is never followed by a speculative second call.
The signed callback binds its `client_state` to the provider call ID; until
that callback arrives, reconciliation remains visibly pending.

After an inbound `call.initiated`, the host reserves durable state, answers the
call, and starts bidirectional media with:

```json
{
  "stream_track": "both_tracks",
  "stream_bidirectional_mode": "rtp",
  "stream_bidirectional_codec": "PCMU",
  "stream_bidirectional_sampling_rate": 8000
}
```

The equivalent TeXML response is native `<Connect><Stream>` XML with the same
codec, mode, sampling rate, status callback, and nested parameters. Inline
TeXML is capped at 4 KiB.

Native controls are:

- `send_dtmf(call_control_id, "12#")`;
- `cold_transfer(call_control_id, "+14155550122")`;
- `hangup(call_control_id)`.

`normal_clearing` and `originator_cancel` map to a completed provider hangup.
All other hangup causes and streaming failures map to `carrier_error`; no
unknown terminal cause is treated as successful.

## Numbers, routing, and recordings

Number orders are asynchronous. Voicey submits one order, then polls the
owned-number resource for at most 60 seconds. It returns the phone-number
resource ID—not the order ID—only after ownership is confirmed. A still-pending
order produces `VY-TEL-011` with the order ID and must be inspected before any
retry.

Temporary routing snapshots the current number `connection_id` in the
FULL-durability telephony ledger before mutation. Restore is compare-and-swap:
voicey overwrites only the route it wrote or an already-restored snapshot.
A concurrent operator change becomes `VY-TEL-006`.

`call.recording.saved` and `call.recording.failed` arrive through the signed
webhook. A saved event includes the stable recording ID and a provider-issued
HTTPS media URL. Voicey downloads that URL without forwarding the API key,
does not follow redirects, accepts known audio types only, enforces a 100 MiB
default, and writes through `ArtifactStore`. Only a URL from the verified
callback is eligible. The terminal event retains its immutable pending
recording reference; ingestion emits `call.recording.ready`.

For inbound Call Control calls, voicey issues `record_start` after durable
reservation and before media startup, using dual-channel MP3 plus a
deterministic `command_id`. Outbound creation carries the equivalent recording
policy. The ready engine URL requires the current or previous result webhook
secret as a bearer and never exposes the signed Telnyx source URL.

## LiveKit FQDN SIP provisioning

`TelnyxLiveKitSipProvisioner` applies the two control planes as one ledgered
operation:

1. create or reuse the number-scoped LiveKit inbound trunk;
2. create or reuse its individual-room dispatch rule;
3. create or reuse the LiveKit outbound trunk to `sip.telnyx.com`;
4. create or reuse a conversational/global Telnyx outbound voice profile;
5. create or reuse the Telnyx credential FQDN connection;
6. attach the LiveKit SIP hostname as an A-record FQDN;
7. attach the Telnyx number to that connection.

Every confirmed new resource is recorded before the next mutation. A
definitive failure deletes new resources in reverse order and restores the
number snapshot. An indeterminate carrier response is marked `ambiguous` and
is not destructively rolled back.

The current official [LiveKit Telnyx provider
guide](https://docs.livekit.io/telephony/start/providers/telnyx/) specifies TCP
port 5060 for this FQDN route and disables the LiveKit media-encryption field.
It also requires the exact outbound
`X-Telnyx-Username` header-to-attribute mapping so Telnyx's initial digest
challenge selects the intended connection. Voicey follows that current
provider recipe and rejects a different explicit port; it does not claim
TLS/SRTP on this certified interconnect.

## Troubleshooting

| Code | Meaning / action |
|---|---|
| `VY-TEL-001` | Install `voicey[telnyx]`; resolve duplicate carrier entry points |
| `VY-TEL-002` | Correct credentials, E.164 values, HTTPS/SIP target, timeout, DTMF, or anchor site |
| `VY-TEL-003` | Owned/available number lookup was empty or non-unique |
| `VY-TEL-004` | Telnyx definitively rejected the API request; inspect account/KYC/destination permissions |
| `VY-TEL-005` | Stop mutations and repair the protected telephony ledger |
| `VY-TEL-006` | Route/provisioning drift, conflict, or ambiguity requires operator reconciliation |
| `VY-TEL-007` | Wait for the signed callback; never redial an ambiguous intent |
| `VY-TEL-008` | Reject an unknown or incomplete signed callback |
| `VY-TEL-009` | Check signed recording URL availability, content type, and size |
| `VY-TEL-010` | Reject an invalid media frame or non-PCMU/8 kHz negotiation |
| `VY-TEL-011` | Carrier availability/order outcome is indeterminate; inspect before retrying |

Paid accounts can still be blocked by KYC, address/bundle requirements,
destination permissions, concurrent-call limits, or an unfunded balance.
Resolve those in the Telnyx Mission Control Portal and rerun `voicey doctor`.

## Verification

Both local carrier paths:

```bash
uv run pytest --no-cov \
  tests/certification/test_telnyx_adapter.py \
  tests/certification/test_telnyx_media.py \
  tests/certification/test_telnyx_livekit_sip.py
```

The read-only account command, guarded route/provisioning commands, paid PSTN
commands, recording-ingestion command, and physical checklist are in
[`docs/GAPS.md`](../GAPS.md). They remain pending until their real accounts and
PSTN endpoints run green.

Next step: export the documented variables and run the read-only Telnyx account
gate in `docs/GAPS.md`.
