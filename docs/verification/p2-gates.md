# P2 phase verification

P2 extends the P1 local gate with both-runtime parity, the complete
configuration matrix, LiveKit crash guarantees, unified native scenario
testing, and both Telnyx carrier paths. External provider, paid-PSTN,
microphone, and handset checks remain explicit pending gates.

## Local aggregate

Build the exact wheel under test and run:

```bash
uv build --wheel --out-dir dist
uv run python tests/verification/run_p2_gate.py \
  --wheel dist/voicekit-0.0.0.dev0-py3-none-any.whl
```

The runner writes `.voicekit/verification/p2-gate-report.json` atomically. It
first reruns the entire P1 local aggregate, then verifies:

- checked-in runtime parity and config-mapping matrices;
- the native LiveKit host, policies, session lifecycle, and actual SIGKILL
  recovery;
- the unified Pipecat EvalSuite and LiveKit `AgentSession.run()` scenario
  compilers;
- Twilio–LiveKit SIP local certification;
- Telnyx Call Control/media and Telnyx–LiveKit SIP local certification.

Any failed local command makes the aggregate exit nonzero. The report's phase
status remains `pending-live` while a live or human row is outstanding, even
when `local_automated_status` is `green`.

## External continuation

The exact credentialed commands and prerequisites are maintained in
[`docs/GAPS.md`](../GAPS.md). They cover:

- reference-provider text/audio/JUnit conversations on both runtimes;
- the LiveKit browser microphone and appointment-workflow conversations;
- Twilio–LiveKit provisioning, paid PSTN, recording, and handset behavior;
- Telnyx Pipecat and LiveKit-path provisioning, paid PSTN, recording, and
  handset behavior.

The aggregate only reports those rows as pending. It never runs a mocked or
lower-tier substitute and calls it live evidence.

Next: run the local aggregate above, then execute each applicable P2 command in
`docs/GAPS.md` when its real credentials and endpoints are available.
