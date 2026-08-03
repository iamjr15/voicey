# P0 walking-skeleton verification

This gate verifies volatile runtime integration points without provider credentials or paid calls. It is intentionally provider-mocked; live carrier certification begins with the P1 Twilio adapter.

## What the gate exercises

Both runtime probes execute:

1. one native runtime bootstrap;
2. one plain voicey `@tool` registered through the runtime-native tool mechanism;
3. one context-local `results.set()` and `set_outcome()`;
4. one browser-session mechanism;
5. one idempotent provider-mocked phone termination;
6. one immutable `call.completed` body delivered to a local receiver and verified with Standard Webhooks headers.

The Pipecat probe uses installed 1.6.0 APIs: core `pipecat.flows`, `PipelineWorker`, `WorkerRunner`, universal context aggregators with VAD placement on `LLMUserAggregatorParams`, RTVI auto-wiring, and a real local SmallWebRTC offer/answer connection.

The LiveKit probe uses installed Agents 1.6.7 APIs: `AgentServer`, `@server.rtc_session`, `AgentSession` with `TurnHandlingOptions`, native `function_tool`, and an access token containing `RoomAgentDispatch`.

## Exact command

```bash
uv sync --frozen --extra pipecat --extra livekit
uv run pytest -m integration --no-cov tests/integration/test_p0_walking_skeleton.py
```

No external gate is implied by this command. Physical-handset, PSTN, carrier-account, and cloud gates remain pending until their complete harnesses land in later phases.

Next: begin P1 unit 1 only after both parametrized runtime probes pass.
