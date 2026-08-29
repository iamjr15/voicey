# P2 phase verification

P2 extends the P1 local gate with both-runtime parity, the complete
configuration matrix, LiveKit crash guarantees, unified native scenario
testing, and both Telnyx carrier paths. Reference-provider text is green on
both runtimes. Provider audio, paid-PSTN, microphone, and handset checks remain
explicit pending gates.

## Local aggregate

Build the exact wheel under test and run:

```bash
uv build --wheel --out-dir dist
uv run python tests/verification/run_p2_gate.py \
  --wheel dist/voicey-1.0.0-py3-none-any.whl
```

The runner writes `.voicey/verification/p2-gate-report.json` atomically. It
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

Fresh projects generated on 2026-08-03 ran all seven appointment text cases
with Deepgram Nova-3, Claude Sonnet 5, Cartesia Sonic 3.5, and native Anthropic
judges. Pipecat and LiveKit each passed every case on the first attempt; no
Ollama request contributed to that evidence.

The exact remaining credentialed commands and prerequisites are maintained in
[`docs/GAPS.md`](../GAPS.md). They cover:

- reference-provider audio/JUnit conversations on both runtimes;
- the LiveKit browser microphone and appointment-workflow conversations;
- Twilio–LiveKit paid PSTN, recording, and handset behavior;
- Telnyx Pipecat and LiveKit-path provisioning, paid PSTN, recording, and
  handset behavior.

The Twilio–LiveKit no-call gate passed on 2026-08-03: the live control planes
provisioned, reused, and reverse-rolled back the exact resource set, and both
providers showed zero temporary resources afterward. Twilio's observed SIP
password policy is validated before mutation. That result does not promote a
paid call, completed recording, microphone, or handset gate.

The aggregate only reports those rows as pending. It never runs a mocked or
lower-tier substitute and calls it live evidence.

Next: run the local aggregate above, then execute each applicable P2 command in
`docs/GAPS.md` when its real credentials and endpoints are available.
