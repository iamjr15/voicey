# Runtime compatibility

This table records versions empirically installed and inspected during P0. Runtime code must use only the current symbols listed here; deprecated aliases are excluded from voicey source.

| Layer | Supported/tested pin | P0 evidence |
|---|---|---|
| Python | 3.11–3.14 | Both runtime pins resolve and install on CPython 3.14.4; CI covers the supported window |
| Pipecat | certified `>=1.6.0,<1.6.1`; project extra pins `pipecat-ai[anthropic,cartesia,deepgram,evals,google,webrtc,websocket]==1.6.0` | Installed from PyPI; the only current edge, 1.6.0, runs on Python 3.11 and 3.14 in scheduled CI; live caller symbols (`PipelineWorker`, universal `LLMContext`, `FastAPIWebsocketTransport`, `TwilioFrameSerializer`) re-inspected on 2026-07-28 |
| Pipecat Flows | core `pipecat.flows` from `pipecat-ai==1.6.0` | `FlowManager` and `NodeConfig` import from core; `pipecat-ai-flows` is not installed |
| Pipecat Evals | `pipecat-ai[evals]==1.6.0` | Installed `pipecat eval run/suite`, `EvalRunnerArguments`, `EvalTransportParams`, text/audio scenario parser, and 0/1 exit contract verified 2026-07-27 |
| Rich | `>=13.9.4,<16` (`13.9.4` selected) | Pipecat 1.6.0's CLI extra requires Rich below 14; the full voicey CLI suite is green on the resolver-selected version |
| LiveKit Agents | certified `>=1.6.7,<1.6.8`; project extra pins `livekit-agents==1.6.7` | Installed from PyPI; the only current edge, 1.6.7, runs on Python 3.11 and 3.14 in scheduled CI; `AgentSession.start(room=…, room_options=…)`, native conversation events, and caller policy re-inspected on 2026-07-28 |
| LiveKit API | `livekit-api==1.2.0` | Resolved by the LiveKit Agents pin |
| Twilio Python | `twilio==9.10.9` | Installed and its request, call, number, AMD, recording, and update signatures inspected on 2026-07-26 |
| Plivo Python | `plivo==4.61.0` | Installed on 2026-07-27; Voice/number/recording calls and the six-argument V3 signature helper were introspected and exercised |
| ngrok Python | `ngrok==1.4.0` | Installed on 2026-07-27; `forward(addr, authtoken=…)`, `Listener.url()`, and awaitable `Listener.close()` inspected |
| cloudflared CLI | `2026.3.0` observed locally | Quick-tunnel URL emission and bounded process cleanup ran; public hostname DNS remained unavailable, so external WS evidence is pending |
| Railway CLI | `>=5.30.1,<6` (`5.30.1` executed locally) | Current project/service/Postgres/bucket/domain/variable/deploy/scale/delete JSON surfaces inspected and the version contract executed on 2026-07-28; authenticated mutations remain pending-live |
| uv CLI | `>=0.11,<1` (`0.11.7` executed locally) | `uv lock --upgrade-package voicey --prerelease allow\|if-necessary-or-explicit`, `uv sync --locked`, and `uv run --locked` help surfaces inspected and exercised on 2026-07-28 |
| websockets Python | `>=13.1,<17` (`15.0.1` selected with both runtime extras) | Pipecat uses APIs present across the range; LiveKit 1.6.7's OpenAI plugin requires `<16` |
| Pipecat client JS | `@pipecat-ai/client-js==1.13.0` | npm registry checked 2026-07-26 |
| Pipecat React | `@pipecat-ai/client-react==1.8.1` | npm registry checked 2026-07-26 |
| Pipecat SmallWebRTC | `@pipecat-ai/small-webrtc-transport==1.10.6` | npm registry checked 2026-07-26 |
| Pipecat Voice UI Kit | `@pipecat-ai/voice-ui-kit==0.13.0` | npm registry checked 2026-07-26 |
| LiveKit client JS | `livekit-client==2.21.0` | Installed 2026-07-27; `Room`, `RoomEvent`, microphone enablement, remote audio attachment, transcription, and disconnect surfaces inspected and exercised |

## Verified volatile API points

- Pipecat runner: `WorkerRunner()` then `await runner.run(auto_end=False)` for the long-lived host.
- Pipecat Flows: `from pipecat.flows import FlowManager, NodeConfig`; do not co-install `pipecat-ai-flows`.
- Pipecat Evals: `EvalRunnerArguments` + `create_transport` with an
  `"eval": lambda: EvalTransportParams(...)` factory; invoke bots with
  `-t eval`, and use `pipecat eval suite` for isolated scenario processes.
- Pipecat context: `LLMContext`, `LLMContextAggregatorPair`, and `LLMUserAggregatorParams`; VAD is configured on `LLMUserAggregatorParams`.
- Pipecat transports: per-provider websocket and SmallWebRTC paths; `FastAPIWebsocketParams` and base `TransportParams` do not accept `vad_analyzer`.
- Twilio serializer: `TwilioFrameSerializer(..., base_url=..., params=TwilioFrameSerializer.InputParams(...))`.
- LiveKit worker: `AgentServer`, `@server.rtc_session`, `setup_fnc`, `AgentSession(turn_handling=TurnHandlingOptions(...))`.
- LiveKit project validation: `LiveKitAPI(url, api_key, api_secret)` then
  `room.list_rooms(ListRoomsRequest())`; always `await aclose()`.
- LiveKit browser: construct `Room`, subscribe to `RoomEvent`, connect with the
  short-lived participant token, attach remote `AudioTrack` publications, call
  `setMicrophoneEnabled()`, and `disconnect()` during cleanup.
- LiveKit turn detection: the installed 1.6.7 `livekit.plugins.turn_detector` path is deprecated; use `livekit.agents.inference.TurnDetector(version="v1-mini")` locally.
- LiveKit's 1.6.7 OpenAI plugin requires `websockets<16`; the shared supported range is `>=13.1,<17`, with the lock selecting 15.x when all runtime extras are installed. Voicey uses APIs present throughout that range and tests both runtime extras together.
- LiveKit SIP: current trunk/rule methods are `create_inbound_trunk` and `create_dispatch_rule`; their `create_sip_*` aliases are deprecated. `create_sip_participant` and `transfer_sip_participant` remain current.
- Twilio↔LiveKit SIP: Twilio-originated traffic cannot use username/password auth, so the number-scoped LiveKit inbound trunk is unauthenticated; Twilio termination credentials are configured on the LiveKit outbound trunk. A secure Twilio trunk uses `;transport=tls` for its origination URI and `SIP_TRANSPORT_TLS` outbound. This is the current documented provider contract, not an SDK inference.
- Twilio Elastic SIP automatic recording: the trunk resource exposes only `RecordingContext.fetch()` / `update(mode, trim)` and no status-callback field. LiveKit supplies the authenticated carrier correlation as participant attribute `sip.twilio.callSid`; voicey queries Core Recordings by that CA SID, requires exactly one completed item with source `Trunking`, and then reuses the authenticated media downloader.
- Twilio request validation: `RequestValidator.validate(uri, params, signature)`; JSON body hashing is selected by the signed `bodySHA256` query parameter.
- Plivo request validation: installed
  `validate_v3_signature(method, uri, nonce, auth_token, signature, params)`
  canonicalizes the POST form; V3 signature and nonce headers are both
  mandatory and voicey adds replay rejection.
- Pipecat Plivo media: `PlivoFrameSerializer` consumes and emits the documented
  PCMU/8 kHz media envelope. Voicey sets `auto_hang_up=False` so carrier
  control remains in the fenced adapter.
- Plivo↔LiveKit SIP: the current provider guide uses
  `<livekit-sip-host>;transport=tcp` inbound. The Plivo outbound trunk uses
  `secure=true`; the LiveKit outbound trunk uses TLS with required media
  encryption. The documented inbound LiveKit trunk does not claim encryption.
- Twilio outbound: `CallList.create` has no idempotency-key parameter; async AMD uses `async_amd` plus its callback fields, and call redirects use `CallContext.update(twiml=...)`.
- ngrok Python: synchronous `forward()` returns a listener; `Listener.close()` is awaitable. HTTP endpoints carry WebSocket upgrades without a second endpoint.
- Cloudflared quick tunnels emit their public URL on stderr before edge DNS is necessarily ready; the WebSocket probe retries within one bounded deadline.
- Railway 5.30.1 uses `init`/`link`, `add --service`,
  `add --database postgres`, `bucket create`, `variable set --stdin`,
  detached `up`, JSON deployment polling, `scale <region>=2`, and explicit
  domain/bucket/service/project deletion. Service variables reference managed
  dependencies as `${{Namespace.VARIABLE}}`; voicey never invokes Railway's
  optional MCP surface.
- uv 0.11.7 changes only lock resolution with
  `lock --upgrade-package voicey`; prerelease policy is `allow` for canary
  and `if-necessary-or-explicit` for stable. Stable mode separately rejects a
  prerelease voicey version before sync, while allowing required
  prerelease-tagged transitive contracts. `sync --locked` installs the
  validated resolution without rewriting `pyproject.toml`, and `run --locked`
  executes drift inspection from the synchronized environment. Both commands
  receive the same prerelease mode used by `lock`; uv otherwise rejects the
  lock as stale.

## Upgrade rule

Pins change only in a contract-alignment commit that:

1. installs both runtime edges on every supported Python version;
2. re-runs symbol introspection and both walking skeletons;
3. updates this table and any affected plan/spec contract;
4. runs parity, schema snapshot, and first-party recipe suites before merge.

## Out-of-range behavior

The certified windows are deliberately narrow because only the listed versions
have empirical symbol and recipe evidence. `PipecatHost` and `LiveKitHost`
inspect the installed distribution at startup. A version outside the table
emits `RuntimeCompatibilityWarning` and continues; it does not create an
availability outage merely because a resolver selected an untested version.
`voicey doctor` follows the same rule: missing runtime packages fail the
check, while installed out-of-range or invalid versions produce loud advice and
leave the check green.

The compatibility-edge workflow is the only path to broadening a range. It
installs each declared edge exactly on Python 3.11 and 3.14 and runs all four
packaged recipes through that runtime's native compiler and entrypoint.
