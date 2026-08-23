# Vobiz

Vobiz has two explicit voicey paths. Pipecat uses the Vobiz Voice API,
VobizXML, and a bidirectional media WebSocket. LiveKit uses API-managed Vobiz
and LiveKit SIP resources. Voicey never silently substitutes one path for
the other.

The protocol and failure suites are locally green. On 2026-08-03 the live
account/owned-number check and the Vobiz↔LiveKit no-call provision, exact reuse,
and reverse rollback gate also passed. Paid PSTN, recording, Pipecat route, and
physical-handset rows remain pending until the exact commands in `docs/GAPS.md`
run successfully.

## Install and credentials

```bash
uv sync --extra vobiz
```

The wizard verifies this extra through its installed `multipart` transport
dependency. It does not require or probe for a third-party top-level `vobiz`
Python package.

Both paths require:

- `VOBIZ_AUTH_ID`;
- `VOBIZ_AUTH_TOKEN`;
- the owned E.164 phone number selected during `voicey init`.

The LiveKit path additionally requires:

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`;
- `VOICEY_LIVEKIT_SIP_URI`, for example
  `sip:project-id.sip.livekit.cloud`;
- `VOICEY_VOBIZ_SIP_CREDENTIAL_ID`;
- `VOICEY_VOBIZ_SIP_USERNAME`;
- `VOICEY_VOBIZ_SIP_PASSWORD`.

Create the Vobiz SIP credential in Vobiz before provisioning. Vobiz does not
return an existing credential password, so voicey requires the exact
credential identifier, username, and password. It does not compare or rotate a
write-only secret by guessing.

The guided CLI writes secrets to the protected `.env`; it keeps them out of
`voicey.jsonc`, call records, callback paths, provisioning metadata, and
logs.

## Capabilities

| Capability | Pipecat path | LiveKit path |
|---|---|---|
| Inbound / outbound | Voice API + VobizXML | Vobiz credentialed SIP |
| Media | bidirectional PCMU at 8 kHz | LiveKit SIP media |
| AMD | asynchronous signed callback | carrier/SIP disposition |
| DTMF receive / send | signed callback + Voice API action | native room event + LiveKit send tool |
| Transfer | native cold transfer | SIP transfer / native LiveKit workflow |
| Recording | signed callback + bounded artifact ingestion | carrier/LiveKit recording policy |
| Native outbound idempotency | unavailable; durable intent fence | unavailable; durable SIP intent fence |

Vobiz publishes India-focused service. Region selection is an operator
placement decision, not a latency claim; run the reference latency and live
PSTN gates from the intended deployment region.

## Pipecat media and callbacks

The answer document starts a bidirectional stream with
`audio/x-mulaw;rate=8000`. Voicey uses the installed Pipecat
`PlivoFrameSerializer` because the documented Vobiz media envelope is
compatible with that wire format. It always sets `auto_hang_up=False`;
Pipecat's serializer must never call a Plivo API for a Vobiz call.

The runtime parses exactly one `start` frame before handing the socket to the
transport. That frame must identify the reserved call and stream and negotiate
PCMU at 8 kHz. It is not allowed to consume the first audio frame while
discovering the handshake.

Vobiz signs HTTP callbacks with its V3 or V2 HMAC headers. Voicey:

- reconstructs the exact configured HTTPS callback URL;
- rejects a different host, unsafe path, WebSocket upgrade, bad base64, or
  malformed 20-digit nonce;
- compares the HMAC in constant time;
- rejects nonce replay within the configured window;
- parses an event only after signature verification.

The media WebSocket closing is not authoritative terminal evidence. A signed
Vobiz terminal callback completes the fenced lifecycle; disconnect recovery
waits for that provider signal up to the configured duration bound.

## Routing and outbound safety

`point_inbound()` snapshots the current number application and trunk binding
in the FULL-durability telephony ledger before changing Vobiz. It creates or
adopts a deterministic managed application, detaches an existing trunk when
needed, attaches the application, and reads the number back. Restore is
compare-and-swap: a human change yields `VY-TEL-006` and is never overwritten.
Only an application created by that route operation is deleted during
rollback.

`start_call()` persists an outbound intent before the Voice API request. A
definitive 4xx rejects it. A timeout, 5xx, invalid response, missing accepted
call UUID, or crash window fences it as ambiguous and returns `VY-TEL-007`;
voicey does not place a speculative second call. A signed callback can bind
the durable intent to the provider call UUID.

Recording URLs are accepted only from verified callback handling. The
downloader requires HTTPS without embedded credentials or redirects, checks
the declared and streamed size, permits only documented audio/octet-stream
types, and copies bytes into the configured engine artifact store. Carrier
URLs are not exposed as result URLs.

## LiveKit SIP provisioning

The Vobiz feasibility decision is positive. The official Vobiz LiveKit
examples document:

- inbound calls sent to the LiveKit SIP host over UDP/5060;
- LiveKit accepting only Vobiz gateway `13.233.44.61/32`;
- outbound calls through a Vobiz `*.sip.vobiz.ai` domain with a Vobiz
  credential and owned caller ID.

Voicey therefore creates or verifies:

1. a Vobiz inbound trunk targeting the LiveKit SIP host;
2. a Vobiz outbound trunk using the existing credential;
3. the Vobiz number-to-inbound-trunk binding;
4. a LiveKit inbound trunk restricted to `13.233.44.61/32`;
5. a LiveKit dispatch rule for the selected native Agent;
6. a LiveKit outbound trunk using UDP and the Vobiz credential.

Media encryption is explicitly disabled on these LiveKit trunk objects because
the published interconnect is UDP, not TLS/SRTP. Voicey does not relabel it
as encrypted transport.

Provisioning names and metadata are deterministic. Existing resources must
compare equal before adoption. Duplicate names or any number, address,
credential, transport, gateway, dispatch, or metadata drift returns
`VY-TEL-006`. Every created resource is appended to the ledger and rollback
runs in reverse order. A network failure or 5xx after a mutation is ambiguous
and stops for reconciliation.

Vobiz's current trunk-create documentation shows both
`trunk_direction`/`concurrent_calls_limit` and
`trunk_type`/`max_concurrent_calls`. Voicey sends both documented aliases
with equal values. The guarded live provision/reuse/rollback test is the
release gate for detecting a provider-side contract change.

The credentialed 2026-08-02 control-plane run also established the current
list envelopes: `/trunks/credentials` returns `objects`, while owned-number
listing returns `items`; an empty trunk list is `objects: null`. Voicey accepts
those exact live envelopes and still requires one exact credential id/username
and one exact E.164 match before any mutation.

An owned number can already point at a Voice API application or another SIP
trunk. Voicey snapshots both `application_id` and `trunk_group_id`, records
rollback ownership before detaching either route, assigns the temporary trunk
through the documented `/numbers/{phone_number}/assign` endpoint, and requires
its current `204` success response. Rollback compare-and-swaps the temporary or
intermediate route back to the exact prior application/trunk; concurrent human
changes stop with `VY-TEL-006`. Restoring the prior application attachment is
also a `204` success in the credentialed control plane.

A complete credentialed run on 2026-08-03 then passed account readiness and
LiveKit provision → reuse → reverse rollback against the owned number and
existing SIP credential. It restored the exact prior Voice API application
route. Final Vobiz and LiveKit API/dashboard inspection showed zero temporary
trunks, dispatch rules, or number bindings. This is control-plane evidence
only; no paid PSTN or media result is inferred.

Official references:

- [Vobiz inbound calls with LiveKit](https://vobiz.ai/docs/examples/livekit-vobiz-inbound)
- [Vobiz outbound calls with LiveKit](https://vobiz.ai/docs/examples/livekit-vobiz-outbound)
- [Vobiz assign number to trunk](https://vobiz.ai/docs/trunks/assign-number)
- [Vobiz list account phone numbers](https://vobiz.ai/docs/account-phone-number/list-account-phone-numbers)

## Local operation

Start the selected runtime:

```bash
uv run voicey dev --phone
```

Inspect credentials, balance, number ownership, route state, and LiveKit SIP
settings:

```bash
uv run voicey doctor
```

The Pipecat development supervisor verifies the public probe before changing
the number and restores the route in `finally`. The container runtime uses the
same adapter and closes its durable ledger during drain. LiveKit development
provisions the SIP resources before accepting calls and rolls back its
operation on a supervised failure.

Next: run the guarded Vobiz commands and physical checklist in
`docs/GAPS.md`; never remove their mutation or paid-call acknowledgements.
