# Troubleshooting

Start with:

```bash
voicekit doctor
```

Doctor checks keys, runtime versions, Python, audio dependencies, ports,
tunnel reachability, carrier routing, LiveKit SIP state, receiver signature
round-trip, dead letters, clock skew, environment drift, and disk space.
Every failed check prints one fix and the next command.

## Route by stable code

| Code family | Surface | First action |
|---|---|---|
| `VK-CFG-*` | `Agent` or `voicekit.jsonc` | Run `voicekit doctor`, then correct the named field |
| `VK-CLI-*` | command input, state, confirmation, or provider key | Run the printed command/fix exactly |
| `VK-TEL-*` | carrier control, signature, number, SIP, transfer | Open the selected [carrier guide](index.md#carriers) |
| `VK-RUN-*` | runtime startup, admission, media, drain | Confirm the [compatibility table](compatibility.md) and runtime guide |
| `VK-RES-*` | lifecycle, result signing/delivery, recording | Inspect [Results and webhooks](results-webhooks.md) |
| `VK-OBS-*` | protected records, metrics, OTLP | Inspect disk/permissions and [Observability](observability.md) |
| `VK-TST-*` | scenario discovery, simulator, native evaluator | Read [Testing](testing.md) |
| `VK-DEP-*` | deployment plan, identity, smoke, rollback | Open the exact target guide under `docs/deploy/` |
| `VK-REL-*` | results relay or companion | Read [Results relay](results-relay.md) |
| `VK-UPG-*` | uv upgrade or recipe drift | Read [Upgrading](upgrading.md) |
| `VK-SEC-*` | permissions, symlink, or secret boundary | Stop mutation and restore an owner-only regular file |

The complete keyed index is the generated [error catalog](api/errors.md).
Each code links to its detailed cause and copy-paste fix in
[docs/errors.md](errors.md).

## Common symptoms

### The browser opens but audio does not start

Confirm you opened the admin URL printed by `voicekit dev`, allowed microphone
access, and did not tunnel the admin port. For LiveKit, check `LIVEKIT_URL` and
the token-exchange result; for Pipecat, check the one-use offer/token and public
Origin. Then run `voicekit doctor`.

### Phone rings but no agent speaks

Compare the owned number's live route with the carrier guide. A Twilio
application or trunk association overrides `VoiceUrl`; Telnyx, Vobiz, and
Plivo have analogous application/trunk precedence. Do not repoint manually
while a voicekit rollback token is active.

### Webhook verification fails

Read the raw body exactly once, pass the three lowercase Standard Webhooks
headers, and use the `whsec_` value rather than its decoded bytes. Check system
clock skew and current/previous rotation variables. Copy one of the three
[receiver examples](results-webhooks.md#receiver-examples).

### Calls are complete but downstream work is missing

```bash
voicekit calls list --undelivered
voicekit calls show <call-id>
```

If delivery is dead-lettered, fix the receiver and explicitly run
`voicekit calls redeliver <call-id-or-event-id> --yes`. Redelivery preserves the
event id and immutable body.

### An upgrade reports recipe drift

Run `voicekit recipes update-check --json`. Treat the output as a three-way
merge plan: preserve local policy and native runtime code, compare against the
recorded baseline, and apply upstream changes manually. The command never
overwrites authored source.

### Runtime version is outside the certified range

Voicekit warns but continues. Consult [Compatibility](compatibility.md), pin
the project extra back to the tested version for production, and reproduce the
issue before proposing a range expansion. A missing runtime package is
different and must be installed.

Next: if the code is absent from the generated catalog, rerun with `--verbose`;
an unmapped error is a bug and the CLI prints a pre-filled issue link.
