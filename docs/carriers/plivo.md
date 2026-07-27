# Plivo

Plivo is a Beta carrier integration with two explicit paths. Pipecat uses the
Voice API, Plivo XML, and bidirectional Audio Streams. LiveKit uses
API-managed Plivo Zentrunk resources plus native LiveKit SIP trunks and a
dispatch rule. Beta means the complete local protocol and failure suites are
green, while credentialed control-plane, paid PSTN, and physical-handset
evidence remain required before production use.

## Install and credentials

```bash
uv sync --extra plivo
```

Both paths require:

- `PLIVO_AUTH_ID`;
- `PLIVO_AUTH_TOKEN`;
- an owned E.164 number selected during `voicekit init`.

The LiveKit path additionally requires:

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`;
- `VOICEKIT_LIVEKIT_SIP_URI`, such as
  `sip:project-id.sip.livekit.cloud`;
- `VOICEKIT_PLIVO_SIP_USERNAME`;
- `VOICEKIT_PLIVO_SIP_PASSWORD`.

The wizard collects these values in-flow and writes the protected `.env`.
Secrets never enter the manifest, callback paths, provisioning metadata, or
logs.

## Capabilities

| Capability | Pipecat path | LiveKit path |
|---|---|---|
| Inbound / outbound | Voice API + Plivo XML | Plivo Zentrunk SIP |
| Media | bidirectional PCMU at 8 kHz | LiveKit SIP media |
| AMD | signed asynchronous callback | carrier/SIP disposition |
| DTMF receive / send | signed callback + Voice API action | native room event + LiveKit send tool |
| Transfer | native cold transfer | SIP transfer / native LiveKit workflow |
| Recording | signed callback + bounded artifact ingestion | carrier/LiveKit recording policy |
| Native outbound idempotency | unavailable; durable intent fence | unavailable; durable SIP intent fence |

Plivo serves multiple regions. India calls require selecting a compatible
LiveKit region as documented by LiveKit. Region placement is an operator
decision; voicekit does not claim a latency result until the live gate runs
from the intended deployment region.

## Pipecat media and callbacks

The answer document starts a bidirectional stream using
`audio/x-mulaw;rate=8000`. Voicekit uses the installed Pipecat 1.6.0
`PlivoFrameSerializer` with `auto_hang_up=False`; the engine-owned adapter,
not an opaque serializer side effect, controls recording, transfer, and
hangup. The serializer certification exercises 20 ms PCMU output,
`playAudio`, incoming `media`, and `clearAudio` interruption semantics.

The runtime accepts exactly one Plivo `start` frame, validates its call and
stream identifiers, and binds it to a one-use durable reservation before
starting the transport. A media socket closing is not terminal authority. A
verified Plivo callback owns the terminal result, so disconnect recovery
cannot create a duplicate terminal event.

Plivo V3 callbacks carry `X-Plivo-Signature-V3` and
`X-Plivo-Signature-V3-Nonce`. Voicekit uses the helper from the installed
`plivo==4.61.0` package with its actual six-argument contract. It
canonicalizes the configured HTTPS callback URL and form body, rejects an
unsafe host/path, HTTP or WebSocket requests, malformed headers, invalid
signatures, and nonce replay, and parses an event only after verification.

## Routing and outbound safety

`point_inbound()` creates or adopts a deterministic Plivo application,
persists the previous number `app_id` before mutation, updates the number,
and reads it back. Restore is compare-and-swap. A concurrent human change
returns `VK-TEL-006` and is never overwritten.

`start_call()` persists one outbound intent before making the non-idempotent
provider request. A definitive 4xx marks the intent rejected. A timeout, 5xx,
invalid envelope, missing accepted call UUID, or crash window marks it
ambiguous and returns `VK-TEL-007`; voicekit never places a speculative
duplicate call. A signed callback carrying the intent path can bind the
provider UUID.

Recording URLs are trusted only after signature verification. Downloads
require HTTPS without embedded credentials or redirects, enforce declared
and streamed size limits plus an audio allowlist, and copy the bytes into the
engine artifact store. Results expose only the authenticated engine URL,
never the carrier URL.

## LiveKit SIP provisioning

The current official LiveKit provider guide defines an asymmetric
interconnect:

1. voicekit creates a Plivo origination URI
   `<livekit-sip-host>;transport=tcp`;
2. it creates an inbound Plivo trunk using that URI and binds the selected
   number;
3. it creates a credential whose deterministic name contains a truncated
   password hash, allowing safe adoption of a write-only secret without
   exposing it;
4. it creates a Plivo outbound trunk with `secure=true`;
5. it creates a LiveKit inbound trunk and dispatch rule for the native Agent;
6. it creates a LiveKit outbound trunk for the returned Plivo domain using
   TLS and required media encryption.

The documented inbound Plivo-to-LiveKit leg is TCP and the LiveKit inbound
trunk therefore declares media encryption disabled. The secure outbound leg
uses `SIP_TRANSPORT_TLS` and `SIP_MEDIA_ENCRYPT_REQUIRE`. Voicekit does not
claim inbound TLS/SRTP where the provider guide does not specify it.

Every created object is appended to the durable provisioning ledger. Existing
objects must match number, address, username, transport, media policy,
dispatch, and secret-derived fingerprint before adoption. Duplicate names or
drift return `VK-TEL-006`. Rollback runs in reverse order and restores the
previous number route. An uncertain external write is marked ambiguous and
stops for reconciliation.

Official references:

- [LiveKit: create and configure a Plivo SIP trunk](https://docs.livekit.io/telephony/start/providers/plivo/)
- [Plivo Audio Streaming protocol](https://www.plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-reference)
- [Plivo Stream XML](https://www.plivo.com/docs/voice-agents/audio-streaming/xml/stream)
- [Plivo V3 signature validation](https://www.plivo.com/docs/voice/concepts/signature-validation)
- [Plivo Calls API](https://www.plivo.com/docs/voice/api/calls)

## Local operation

Start the selected runtime and inspect the account, balance, number, route,
and LiveKit SIP resources:

```bash
uv run voicekit dev --phone
uv run voicekit doctor
```

The development supervisor probes the public Pipecat route before mutation
and restores it in `finally`. LiveKit development provisions before accepting
calls and reverses the operation during supervised cleanup.

Next: run the guarded commands and physical both-path checklist in
`docs/GAPS.md`. Their route-mutation and paid-call acknowledgements must not
be removed.
