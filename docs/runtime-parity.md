# Runtime parity

Voicekit exposes one product contract across Pipecat and LiveKit while keeping
conversation logic native to each framework. Pipecat projects use
`pipecat.flows`; LiveKit projects use native `Agent` workflows and tasks.
Voicekit does not introduce a flow DSL or translate one runtime's workflow into
the other.

Two checked-in machine-readable matrices make the boundary auditable:

- [`runtime-parity-matrix.json`](runtime-parity-matrix.json) maps each public
  feature to both runtimes and names its executable evidence.
- [`runtime-config-matrix.json`](runtime-config-matrix.json) maps every shared
  behavior, limit, fallback, language, and recording field to the native
  mechanism in the installed runtime pin.

`tests/parity/` enforces the matrices and runs the same observable checks for
both runtimes. It covers the recipe greeting, typed-tool declaration and
execution order, terminal webhook payload, and every configuration field:

```bash
uv run pytest --no-cov tests/parity
```

The complete local gate also runs formatting, lint, strict type checking, and
the full test suite:

```bash
uv run pre-commit run --all-files
```

## Support and exclusions

A `supported` matrix cell must name a checked-in test file or test function.
CI rejects missing evidence, unknown features, stale runtime pins, or a mapping
whose mechanism is blank or marked pending.

A real runtime difference is represented as `declared_exclusion` with a reason,
target phase, and evidence. It is never silently downgraded. P3 closed the sole
P2 exclusion: Pipecat/Twilio now uses a consent-gated, private-briefing
conference bridge, while LiveKit uses its native warm-transfer workflow.

## Recording mapping

`phone.record` is copied into both runtime policies. On Pipecat phone calls,
voicekit starts carrier-native dual-channel recording only after the durable
call reservation exists and before media is exposed. Twilio uses a signed
completion callback; Telnyx uses its idempotent `record_start` command and
signed event callback. The adapters verify and normalize completion events and
download carrier media into the engine artifact boundary.

On LiveKit, the native session record policy is combined with provider
recording reconciliation. Twilio Elastic SIP has no per-trunk completion
callback, so voicekit correlates the authenticated Twilio call SID and performs
bounded post-call reconciliation.

The carrier commands are based on the current official
[Twilio Recording API](https://www.twilio.com/docs/voice/api/recording) and
[Telnyx recording-start API](https://developers.telnyx.com/api-reference/call-commands/recording-start);
their installed SDK shapes are separately asserted by certification tests.

## Changing a shared field

Any shared configuration or externally observable behavior change must update
the canonical model, both runtime mappings, the appropriate matrix, and its
runtime-parameterized test in the same commit. A contract change also amends
`docs/product-spec.md`.

Next: run the parity test command above before changing a runtime bootstrap or
native workflow.
