# P1 phase verification

P1 has one executable local gate and explicit external/manual continuations. The
local runner never substitutes mocks or endpoint health for a gate that requires
provider credentials, public media, paid PSTN, or a person.

## Local gate

Build the exact wheel under test, then run the phase aggregator:

```bash
uv build --wheel --out-dir dist
uv run python tests/verification/run_p1_gate.py \
  --wheel dist/voicey-1.0.0-py3-none-any.whl
```

The report is written atomically to
`.voicey/verification/p1-gate-report.json`. CI runs the same command and
uploads the report.

| Check | Current evidence |
|---|---|
| Fresh wheel to first provider-mocked native browser session | green in 34.086 seconds; budget is 300 seconds |
| CLI question twins, exact flag surface, and JSON applicability | green; all 22 actionable and reserved command paths match `p1-cli-matrix.json` |
| Parallel call/result context isolation | green |
| Terminal transaction, late-writer fencing, dual sweeper, and actual SIGKILL recovery | green |
| Public signaling cannot reach the admin/read listener | green |
| Twilio mocked carrier certification and 8 kHz audio rig | green; 34 tests |
| Canonical Docker storage/drain/deployment contract | green; 31 tests |

The quickstart scope is deliberately named in its JSON evidence:
`provider-mocked native runtime and local browser peer`. It installs only the
built wheel and its Pipecat extra into a new virtualenv, invokes the real
non-interactive wizard, imports the generated native `NodeConfig` and typed
tool, connects a local SmallWebRTC peer, terminalizes once, and verifies the
signed result. It does not claim a real microphone or provider call.

## Reference audio latency

The appointment recipe includes `evals/latency-suite.yaml`, a 20-user-turn
native Pipecat audio scenario. The gate runs the locked reference stack and
queries the production `UserBotLatencyObserver` samples in the protected eval
database. It requires at least 20 distinct persisted turns and both:

- p50 end-to-end latency at or below 800 ms;
- p95 end-to-end latency at or below 1500 ms.

After creating the disposable reference project in `GAPS.md` and injecting the
three provider credentials:

```bash
uv run python tests/verification/p1_latency_gate.py \
  --project "$VOICEY_EVAL_PROJECT"
```

Missing credentials return status `pending-live` and exit 2. A missing sample,
failed Pipecat suite, wrong model selection, or budget breach returns
`failed` and exit 1. The 2026-08-03 reference text suite used the available
Deepgram, Anthropic, and Cartesia credentials and passed all seven Pipecat cases
on the first attempt, but that does not generate the 20 real audio samples.
This latency gate has not run and remains pending-live.

## Credentialed aggregate

Once a public runtime, reference-provider credentials, Twilio test/live
credentials, owned numbers, paid destination, transfer destination, and the two
explicit acknowledgements are present:

```bash
uv run python tests/verification/run_p1_gate.py \
  --wheel dist/voicey-1.0.0-py3-none-any.whl \
  --require-live \
  --latency-project "$VOICEY_EVAL_PROJECT"
```

`--require-live` fails before external mutations if any prerequisite or
acknowledgement is absent. It never sets charge or route acknowledgements for
the operator.

## Phase status

All automatable local P1 gates and the credentialed Pipecat reference text
suite are green. Twilio's no-charge API and live account/owned-number readiness
checks are also green without a real call. P1 remains `pending-live` overall
until the reference audio and latency runs, Twilio route/PSTN/public-edge checks,
public Docker smoke, and the human wizard/doctor/microphone/physical-handset
checks in `GAPS.md` actually run green. Implementation may proceed to P2 under
the repository's reality-boundary rule; this status is not promoted.

Next: supply the missing live resources, run the exact commands above and in
`GAPS.md`, and retain their reports with the release evidence.
