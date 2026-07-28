# Reliability hardening

P4 hardening treats every admitted call as durable work. New generations stop
admission first, already-visible calls retain their reservation, active calls
finish within `limits.max_duration_s`, and the delivery worker receives one final
flush before process exit.

## Chaos matrix

The credential-free chaos gate injects:

- provider connection loss on Pipecat and LiveKit;
- carrier WebSocket loss;
- a bounded tool timeout after a partial transcript write;
- actual `SIGKILL` of each runtime's call owner;
- transaction failure between terminal state and outbox insertion;
- two simultaneous stale-call sweepers; and
- delayed heartbeat, result, and terminal writes from a fenced generation.

Every row asserts one immutable terminal event and one visible delivery. Delivery
may be acknowledged, scheduled for retry, or dead-lettered; it may never
disappear.

Run the local gate against both repository backends:

```bash
export VOICEKIT_TEST_POSTGRES_DSN='postgresql://voicekit:voicekit-test@127.0.0.1:5432/voicekit' # pragma: allowlist secret
uv run python tests/verification/run_p4_hardening_gate.py
```

Reports are written atomically under `.voicekit/verification/`.

## Drain contract

- Pipecat and LiveKit close new admission atomically.
- A browser reservation issued before drain remains valid; a reservation or
  unreserved SIP job arriving after drain is rejected with `VK-RUN-008`.
- Docker stops both listeners only after the host drain and final delivery pass.
- LiveKit Cloud uses the installed `AgentServer.drain(timeout=...)` contract
  after closing the Voicekit admission gate.
- Results companion readiness closes before its grace window and final
  maintenance pass.
- Fly, Pipecat Cloud, LiveKit Cloud, Railway, and Docker still require their
  target-specific live rolling-generation run. Those commands remain
  `pending-live` until an authenticated target actually runs them.

## Soak

The soak runner creates deterministic simulated caller and agent turns through
the shared fenced lifecycle for both runtimes. Each worker persists timeline,
transcript, result, terminal, and outbox state. It verifies:

- peak calls reach `max_concurrent` for each selected runtime;
- every started call reaches exactly one readable terminal event;
- no admission or active-call counter remains;
- retained Python heap growth is at most 32 MiB;
- process RSS high-water growth is at most 64 MiB; and
- open file descriptors grow by at most four.

A short run is a CI regression check, not the production soak:

```bash
uv run python tests/verification/p4_soak.py \
  --duration-s 30 \
  --max-concurrent 8 \
  --call-hold-s 0.05 \
  --runtime both
```

The release gate is the full wall-clock run:

```bash
uv run python tests/verification/p4_soak.py \
  --duration-s 86400 \
  --max-concurrent 8 \
  --call-hold-s 1 \
  --runtime both
```

Do not mark the 24-hour gate green from a shortened run.
