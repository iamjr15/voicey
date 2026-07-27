# Generic SIP

Generic SIP is a LiveKit-only Beta escape hatch for an existing PBX, SBC, or
carrier that is not represented by a first-party adapter. Voicekit manages
the LiveKit side. The operator owns the external trunk, routing, ACLs,
credentials, regional placement, and rollback. There is no Pipecat path and
no provider API is guessed from generic connection details.

## Install and configuration

```bash
uv sync --extra livekit
```

Select `sip` as the carrier with the LiveKit runtime. The wizard collects:

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`;
- `VOICEKIT_SIP_ADDRESS` as `host` or `host:port`, without `sip:` or
  credentials;
- `VOICEKIT_SIP_USERNAME`;
- `VOICEKIT_SIP_PASSWORD`;
- `VOICEKIT_SIP_TRANSPORT` as `udp`, `tcp`, or `tls`;
- `VOICEKIT_SIP_MEDIA_ENCRYPTION` as `disable`, `allow`, or `require`;
- optional comma-separated `VOICEKIT_SIP_ALLOWED_ADDRESSES` CIDRs.

Every choice is explicit; no transport or encryption recommendation is
preselected. Validation rejects malformed addresses and credentials,
unknown enum values, and the insecure contradiction `tls` plus
`media_encryption=disable`.

## Ownership boundary

Voicekit creates or verifies only these LiveKit resources:

1. an inbound trunk for the selected E.164 number, optionally restricted to
   the exact configured CIDRs;
2. a dispatch rule for the selected native LiveKit Agent;
3. an outbound trunk with the configured address, username, password,
   transport, and media-encryption policy.

The external operator must point the PBX/carrier at the LiveKit SIP endpoint,
configure the reverse route to `VOICEKIT_SIP_ADDRESS`, make the authentication
values agree, and apply matching network and media policies. `voicekit dev`
cannot mutate or restore that external system.

Provisioning metadata records `managed_by=voicekit`, provider `sip`, Beta
tier, and a secret-safe configuration fingerprint. A credential password is
never stored in metadata. Deterministic names allow reuse only when every
readable field matches. Duplicate names, address/CIDR/transport/media drift,
or a changed secret fingerprint returns `VK-TEL-006`.

All created LiveKit objects are recorded before the next write and rolled
back in reverse order. A network error or uncertain result is marked
ambiguous; voicekit does not retry a potentially successful mutation.

## Security and operations

Prefer TLS plus required media encryption when the external provider supports
that exact combination, but configure only what the provider contract proves.
For UDP/TCP, use narrow source CIDRs wherever the provider publishes stable
gateways. A blank allowlist accepts traffic reaching the LiveKit trunk and
must be treated as an explicit operator risk.

Run the local checks:

```bash
uv run voicekit doctor
uv run voicekit dev --phone
```

Doctor validates the local values and the expected LiveKit trunk and dispatch
names. It cannot certify the external PBX route; that remains an operator
check.

Official references:

- [LiveKit SIP trunk setup](https://docs.livekit.io/telephony/start/sip-trunk-setup/)
- [LiveKit inbound trunks](https://docs.livekit.io/telephony/accepting-calls/inbound-trunk/)
- [LiveKit dispatch rules](https://docs.livekit.io/telephony/accepting-calls/dispatch-rule/)

Next: execute the guarded provision/reuse/rollback and paid loopback commands
plus the external-route checklist in `docs/GAPS.md`. Generic SIP remains Beta
even after those gates; it cannot inherit certification from a specific
carrier without that carrier's full checklist.
