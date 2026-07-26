# Runtime compatibility

This table records versions empirically installed and inspected during P0. Runtime code must use only the current symbols listed here; deprecated aliases are excluded from voicekit source.

| Layer | Supported/tested pin | P0 evidence |
|---|---|---|
| Python | 3.11–3.14 | Both runtime pins resolve and install on CPython 3.14.4; CI covers the supported window |
| Pipecat | `pipecat-ai==1.6.0` | Installed from PyPI on 2026-07-26; latest resolver candidate was unchanged |
| Pipecat Flows | core `pipecat.flows` from `pipecat-ai==1.6.0` | `FlowManager` and `NodeConfig` import from core; `pipecat-ai-flows` is not installed |
| LiveKit Agents | `livekit-agents==1.6.7` | Installed from PyPI on 2026-07-26; latest resolver candidate was unchanged |
| LiveKit API | `livekit-api==1.2.0` | Resolved by the LiveKit Agents pin |
| Twilio Python | `twilio==9.10.9` | Installed and its request, call, number, AMD, recording, and update signatures inspected on 2026-07-26 |
| Pipecat client JS | `@pipecat-ai/client-js==1.13.0` | npm registry checked 2026-07-26 |
| Pipecat React | `@pipecat-ai/client-react==1.8.1` | npm registry checked 2026-07-26 |
| Pipecat SmallWebRTC | `@pipecat-ai/small-webrtc-transport==1.10.6` | npm registry checked 2026-07-26 |
| Pipecat Voice UI Kit | `@pipecat-ai/voice-ui-kit==0.13.0` | npm registry checked 2026-07-26 |
| LiveKit client JS | `livekit-client==2.21.0` | npm registry checked 2026-07-26 |

## Verified volatile API points

- Pipecat runner: `WorkerRunner()` then `await runner.run(auto_end=False)` for the long-lived host.
- Pipecat Flows: `from pipecat.flows import FlowManager, NodeConfig`; do not co-install `pipecat-ai-flows`.
- Pipecat context: `LLMContext`, `LLMContextAggregatorPair`, and `LLMUserAggregatorParams`; VAD is configured on `LLMUserAggregatorParams`.
- Pipecat transports: per-provider websocket and SmallWebRTC paths; `FastAPIWebsocketParams` and base `TransportParams` do not accept `vad_analyzer`.
- Twilio serializer: `TwilioFrameSerializer(..., base_url=..., params=TwilioFrameSerializer.InputParams(...))`.
- LiveKit worker: `AgentServer`, `@server.rtc_session`, `setup_fnc`, `AgentSession(turn_handling=TurnHandlingOptions(...))`.
- LiveKit SIP: current trunk/rule methods are `create_inbound_trunk` and `create_dispatch_rule`; their `create_sip_*` aliases are deprecated. `create_sip_participant` and `transfer_sip_participant` remain current.
- Twilio request validation: `RequestValidator.validate(uri, params, signature)`; JSON body hashing is selected by the signed `bodySHA256` query parameter.
- Twilio outbound: `CallList.create` has no idempotency-key parameter; async AMD uses `async_amd` plus its callback fields, and call redirects use `CallContext.update(twiml=...)`.

## Upgrade rule

Pins change only in a contract-alignment commit that:

1. installs both runtime edges on every supported Python version;
2. re-runs symbol introspection and both walking skeletons;
3. updates this table and any affected plan/spec contract;
4. runs parity, schema snapshot, and first-party recipe suites before merge.
