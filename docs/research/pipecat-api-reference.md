# Pipecat Stack — Implementation-Grade Research (July 2026)

## Context

We're building a production voice-agent toolchain **on top of** Pipecat. This document is the engineering reference for the *current* stack (as researched 2026-07-26): exact class/function names, import paths, current versions, constructor params, and version-dependent gotchas — not marketing. Sources are official (`docs.pipecat.ai`, the auto-generated `reference-server.pipecat.ai`, PyPI, and the GitHub repo/CHANGELOG). Anything uncertain is flagged inline.

> **Single most important finding — a big rename landed in `pipecat-ai` 1.3.0 (late May 2026).** The long-standing `PipelineTask` / `PipelineRunner` API was renamed to **`PipelineWorker` / `WorkerRunner`** as part of a new multi-agent `pipecat.workers` framework. The old names still work as **deprecated aliases** (emit `DeprecationWarning`, slated for removal in **2.0.0**). Almost every tutorial, blog post, and Context7 snippet online still shows the old names. **A new toolchain built now should target the new names.**

> **P0 empirical resolution (2026-07-26):** installed `pipecat-ai==1.6.0` on CPython 3.14.4. Flows imports from core `pipecat.flows`; standalone `pipecat-ai-flows` is not installed. `FastAPIWebsocketParams` and `TransportParams` do not accept `vad_analyzer`; `LLMUserAggregatorParams` does. `TwilioFrameSerializer` includes `base_url`. For a long-lived host, construct `WorkerRunner()` and pass `auto_end=False` to `runner.run()`.

---

## 1. Versions, Python support, cadence

| Package | Current | Notes |
|---|---|---|
| `pipecat-ai` | **1.6.0** (2026-07-21, per CHANGELOG) — PyPI project page cache showed **1.5.0**; treat 1.5/1.6 as current | Status: Production/Stable. **Python `>=3.11`** (3.10 dropped). BSD-2-Clause. ~1.09M downloads/month. |
| `pipecat-ai-flows` | **1.2.0** (2026-05-30) | Production/Stable. Python `>=3.11`. Depends on **`pipecat-ai >=1.3.0,<2`**. Now documented as "built into Pipecat." |

**Release cadence** (fast — roughly a minor every ~3–4 weeks; breaking changes gated behind deprecation aliases, removals deferred to 2.0.0):
- `pipecat-ai`: 1.3.0 (2026-05-28, multi-agent `pipecat.workers` + the Task→Worker rename) → 1.6.0 (2026-07-21). 1.6.0 added `on_heartbeat_timeout`, shared `TaskManager(loop=, context=)` across a `WorkerRunner`.
- `pipecat-ai-flows`: 0.0.x through 2026-03 (last 0.0.24 on 2026-03-20) → **1.0.0 (2026-04-15)** → 1.1.0 (05-07) → 1.1.1 (05-28) → 1.2.0 (05-30). The 0.0.x→1.0 jump reworked the node/function API (see §3).

**Recent breaking-change history to be aware of** (all with back-compat shims today, removed in 2.0.0):
- `PipelineTask`→`PipelineWorker`, `PipelineRunner`→`WorkerRunner` (module `pipecat.pipeline.task`→`pipecat.pipeline.worker`; runner moved to `pipecat.workers.runner`), `PipelineTaskParams`→`WorkerParams` (1.3.0).
- `tool_resources`→`app_resources` (1.2.0); `FrameProcessor.pipeline_task`→`pipeline_worker`.
- Flows: `role_messages` (list)→`role_message` (str) deprecated since 0.0.24; `FlowResult` deprecated (return any JSON-serializable); `task=`→`worker=` on `FlowManager` (1.2.0).
- Per-provider `OpenAILLMContext` → universal **`LLMContext`** + **`LLMContextAggregatorPair`** (see §3/§9).

Sources: https://pypi.org/project/pipecat-ai/ · https://pypi.org/project/pipecat-ai-flows/ · https://github.com/pipecat-ai/pipecat/blob/main/CHANGELOG.md · https://github.com/pipecat-ai/pipecat/releases/tag/v1.3.0

---

## 2. Core runtime

### Pipeline
`from pipecat.pipeline.pipeline import Pipeline` — `Pipeline(processors: list[FrameProcessor])`. Canonical cascaded order:
```python
pipeline = Pipeline([
    transport.input(),
    stt,
    context_aggregator.user(),
    llm,
    tts,
    transport.output(),
    context_aggregator.assistant(),
])
```

### PipelineWorker (was PipelineTask)
`from pipecat.pipeline.worker import PipelineWorker, PipelineParams` (old: `from pipecat.pipeline.task import PipelineTask` still re-exports).
Full signature (stable reference server):
```
PipelineWorker(
  pipeline, *, active=True, params: PipelineParams|None=None,
  observers: list[BaseObserver]|None=None,
  idle_timeout_secs: float|None=300,
  idle_timeout_frames=(BotSpeakingFrame, UserSpeakingFrame),
  cancel_on_idle_timeout=True, cancel_runner_on_idle_timeout=True,
  cancel_timeout_secs=20.0,
  enable_turn_tracking=True,       # turn tracking ON by default
  enable_rtvi=True,                # RTVI ON by default
  enable_tracing=False,            # OpenTelemetry
  rtvi_processor: RTVIProcessor|None=None,
  rtvi_observer_params: RTVIObserverParams|None=None,
  conversation_id: str|None=None, additional_span_attributes=None,
  app_resources=None,              # shared bag for tool handlers (was tool_resources)
  clock=None, task_manager=None, check_dangling_tasks=True, name=None,
  exclude_frames=None, bridged=None,
)
```
**Event handlers** (register with `@worker.event_handler("...")`): `on_pipeline_started`, `on_pipeline_finished` (inspect frame: `StopFrame`/`EndFrame`/`CancelFrame`), `on_pipeline_error` (`ErrorFrame`), `on_idle_timeout`, `on_heartbeat_timeout`, `on_frame_reached_upstream/downstream`. These are the primary hooks for cleanup/resilience.

### PipelineParams
`PipelineParams(*, audio_in_sample_rate=16000, audio_out_sample_rate=24000, enable_heartbeats=False, enable_metrics=False, enable_usage_metrics=False, heartbeats_period_secs=1.0, heartbeats_monitor_secs=10.0, report_only_initial_ttfb=False, send_initial_empty_metrics=True, start_metadata={})`
> **GOTCHA:** `allow_interruptions` is **no longer a `PipelineParams` field** in 1.x — interruptions are VAD-driven and on by default. Older tutorials still pass it. Sample rates default 16 kHz in / 24 kHz out; **for Twilio set both to 8000** (see §4). Idle/lifecycle params live on `PipelineWorker`, not `PipelineParams`.

### WorkerRunner (was PipelineRunner)
`from pipecat.workers.runner import WorkerRunner` (old: `from pipecat.pipeline.runner import PipelineRunner`).
```
WorkerRunner(*, name=None, bus: WorkerBus|None=None, handle_sigint=True,
             handle_sigterm=False, force_gc=False, check_dangling_tasks=True,
             task_manager: BaseTaskManager|None=None)
```
Modern run pattern (single-pipeline bot):
```python
runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
await runner.add_workers(worker)     # preferred; passing worker to run() is deprecated
await runner.run()                    # auto_end=True → runner exits when the pipeline finishes
```
`run(worker=None, *, auto_end=True)`, plus `end(reason)` / `cancel(reason)`.
> **FastAPI hosting:** the dev runner and per-call servers create **one `PipelineWorker` per connection in a single process** (one process, many concurrent calls). For a **long-lived runner** that services many sessions, pass **`auto_end=False`** so it doesn't exit when it briefly has zero workers. Pipecat Cloud instead does session isolation at the platform level.

### Development runner (how one `python bot.py` serves web + telephony)
- `from pipecat.runner.run import main` → `if __name__ == "__main__": main()` starts a FastAPI/uvicorn dev server (default **`localhost:7860`**) that wires up web (SmallWebRTC) + telephony endpoints.
- `from pipecat.runner.utils import create_transport` — pick a transport from a `{ "daily": lambda: DailyParams(...), "webrtc": lambda: TransportParams(...), "twilio": lambda: FastAPIWebsocketParams(...), "websocket": lambda: FastAPIWebsocketParams(...) }` dict of factory lambdas.
- `from pipecat.runner.types import RunnerArguments, DailyRunnerArguments, SmallWebRTCRunnerArguments` (+ telephony variants). Entry point is `async def bot(runner_args: RunnerArguments): transport = await create_transport(runner_args, transport_params); await run_bot(transport)`.
- **CLI flags:** `--host` (default localhost), `--port` (7860), `-t/--transport {daily,webrtc,twilio,telnyx,plivo,exotel}` (omit → serve all), `-x/--proxy <hostname>` (public proxy for telephony webhooks — required for telephony), `--allowed-origins`, `--esp32`, `-d/--direct` (Daily), `--dialin`, `--whatsapp`, `-v/--verbose`.
- Examples: `python bot.py -t webrtc` (opens browser UI) · `python bot.py -t twilio -x your_domain.ngrok.io`.

Sources: https://reference-server.pipecat.ai/en/stable/api/pipecat.pipeline.worker.html · https://reference-server.pipecat.ai/en/stable/api/pipecat.workers.runner.html · https://docs.pipecat.ai/api-reference/server/utilities/runner/guide · https://docs.pipecat.ai/api-reference/server/pipeline/pipeline-worker

---

## 3. Pipecat Flows (`pipecat-ai-flows` 1.2.0)

Flows structures a conversation as a **graph of nodes**; each node focuses the LLM on one task with only the tools it needs. **Requires a cascaded STT→LLM→TTS pipeline with function calling** (OpenAI, Anthropic, Google Gemini, AWS Bedrock, OpenAI-compatible). **Speech-to-speech / realtime models are NOT supported** (Gemini Live, OpenAI Realtime, Ultravox, AWS Nova Sonic) — Flows rewrites context+tools mid-session and those APIs don't expose the controls.

### FlowManager
`from pipecat.flows import FlowManager, NodeConfig, FlowsFunctionSchema, FlowArgs`
```
FlowManager(*, llm, context_aggregator, worker,   # worker= replaces deprecated task=
            context_strategy: ContextStrategyConfig|None=None,
            transport=None, global_functions: list|None=None)
```
- `await flow_manager.initialize(initial_node: NodeConfig | None = None)` — call once (typically inside `@transport.event_handler("on_client_connected")`).
- `await flow_manager.set_node_from_config(node_config)` — manual transition (prefer returning next node from a function).
- `flow_manager.state: dict[str, Any]` — persistent data across nodes.
- `flow_manager.register_action(action_type, handler)` — custom actions; modern handler signature `(action, flow_manager)` (single-arg legacy deprecated → removed 2.0.0).
- `global_functions=[...]` — functions available at every node (e.g. "transfer to human").

### Static vs dynamic
The 1.x code-first model is effectively **dynamic**: you write `NodeConfig`s (as objects or plain dicts — both work) and transition by returning `(result, next_node)` from a function, or `set_node_from_config()`. The legacy **static `FlowConfig` JSON** (a full node graph, produced by the **Visual Flow Editor** export) is still supported for declaratively-defined flows, but new code typically builds nodes in Python.

### NodeConfig fields
`task_messages` (**required**, `list[dict]`, uses role `"developer"`), `role_message` (**str**, new — sent as system instruction via `LLMUpdateSettingsFrame`, persists across nodes until re-set; replaces deprecated `role_messages` list), `name`, `functions` (list of `FlowsFunctionSchema` or direct functions), `pre_actions`, `post_actions`, `context_strategy` (`ContextStrategyConfig`), `respond_immediately` (bool, default `True` — set `False` to wait for user input, e.g. after a `tts_say` pre-action).

### Functions
- **Direct function (preferred):** one async fn is both handler and schema. First param is `flow_manager: FlowManager`; remaining params become the tool args; schema (name/description/params/required) is auto-derived from signature + Google-style docstring.
  ```python
  async def record_favorite_color(flow_manager: FlowManager, color: str) -> tuple[str, NodeConfig]:
      """Record the user's favorite color.
      Args:
          color: The user's favorite color."""
      return color, create_end_node()
  ```
- **`FlowsFunctionSchema` (advanced):** explicit control (strict `enum`, numeric `min/max`). Fields: `name`, `description`, `properties` (JSON-Schema dict), `required` (list[str]), `handler`, plus cancel-on-interruption and per-tool timeout options (overrides LLM's `function_call_timeout_secs`).
- **Return contract — `ConsolidatedFunctionResult = tuple[Any, NodeConfig | None | NO_RESPONSE]`:** first element = result fed back to LLM (any JSON-serializable, or `None`); second = next node (`NodeConfig`), `None` (stay + respond), or `NO_RESPONSE` (finish without transitioning/responding). "Node functions" return `(result, None)`; "edge functions" return `(result, next_node)`.

### Actions & ending a call
- `pre_actions` run on node entry (before LLM); `post_actions` run after LLM inference **and after TTS finishes speaking**.
- Built-in action types: **`tts_say`** (`text`), **`end_conversation`** (`text` optional goodbye; `append_text_to_context` default `True`), **`function`** (`handler`).
- **End a call from a flow:** `post_actions=[{"type": "end_conversation", "text": "Goodbye!"}]`. Timing guarantees the bot finishes speaking before the conversation ends.

### Canonical wiring (modern API, end-to-end)
```python
context = LLMContext()
context_aggregator = LLMContextAggregatorPair(
    context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))
pipeline = Pipeline([transport.input(), stt, context_aggregator.user(), llm, tts,
                     transport.output(), context_aggregator.assistant()])
worker = PipelineWorker(pipeline)
flow_manager = FlowManager(worker=worker, llm=llm,
                           context_aggregator=context_aggregator, transport=transport)

@transport.event_handler("on_client_connected")
async def _(transport, client):
    await flow_manager.initialize(create_initial_node())

runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
await runner.add_workers(worker)
await runner.run()
```

Sources: https://docs.pipecat.ai/api-reference/pipecat-flows/flow-manager · https://docs.pipecat.ai/api-reference/pipecat-flows/types · https://docs.pipecat.ai/pipecat-flows/guides/functions · https://docs.pipecat.ai/pipecat-flows/guides/actions · https://docs.pipecat.ai/pipecat-flows/guides/quickstart

---

## 3.5 Cross-cutting: three more renames in flight (all deprecated-alias, removed in 2.0.0)

Beyond Task→Worker (§2), a new toolchain must target the *new* side of all of these:

| Migration | Old (deprecated) | New (current) |
|---|---|---|
| **Transport module paths — old aliases DELETED (PR #4225, 2026-04-02, raises `ModuleNotFoundError`)** | `pipecat.transports.network.*`, `pipecat.transports.services.*` | per-provider paths (see §4/§5 table) |
| **Universal context** | `OpenAILLMContext`/`AnthropicLLMContext`, `OpenAILLMContextFrame`, `llm.create_context_aggregator(ctx)` | `LLMContext`, `LLMContextFrame`, `LLMContextAggregatorPair(ctx)` |
| **Service settings** | direct `model=`/`voice=` kwargs, `InputParams`+`params=` | nested `Service.Settings(...)` + `settings=` (canonical since 0.0.105) |
| **Idle / turn / interruptions** | `UserIdleProcessor`, `STTMuteFilter`, `PipelineParams.allow_interruptions` | fields on `LLMUserAggregatorParams` (`user_idle_timeout`, `user_mute_strategies`, `user_turn_strategies`) |
| **Client class** | `RTVIClient` (+ `RTVIClientProvider`/`useRTVIClient`) | `PipecatClient` (+ `PipecatClientProvider`/`usePipecatClient`). Event names (`RTVIEvent`, `useRTVIClientEvent`) stay RTVI-prefixed. |

> **Tooling that solves this staleness problem for us:** `pipecat-ai-context-hub` (§10.4) — an official local-first index + **MCP server** exposing `check_deprecation`, `search_api`, `search_docs`. It is purpose-built so a coding agent verifies every symbol against the installed version before writing. **We should wire this into our toolchain.**

---

## 4. Twilio telephony path

**Imports (current — old `transports.network.*` deleted):**
```python
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer
```

### FastAPIWebsocketTransport
`FastAPIWebsocketTransport(websocket: WebSocket, params: FastAPIWebsocketParams, input_name=None, output_name=None)`. Raises `ValueError` if `params.allowed_origins` is set and the connection `Origin` is missing/disallowed. Event handlers: `on_client_connected`, `on_client_disconnected`, `on_session_timeout`.

**FastAPIWebsocketParams** (subclasses `TransportParams`, keyword-only): `add_wav_header=False` (**must stay False for raw telephony audio**), `serializer: FrameSerializer|None=None`, `session_timeout: int|None=None`, `fixed_audio_packet_size: int|None=None`, `allowed_origins` (from `PIPECAT_ALLOWED_ORIGINS`), `ws_close_timeout=0.5`. Inherited: set `audio_in_enabled=True`, `audio_out_enabled=True` (both default False); `audio_in_sample_rate`/`audio_out_sample_rate`, `audio_*_channels=1`. **No `vad_analyzer` field on the reference-server signature (see §5 conflict note).**

### TwilioFrameSerializer
```python
TwilioFrameSerializer(
    stream_sid: str,                 # required
    call_sid: str|None=None,         # required for auto hang-up
    account_sid: str|None=None,      # required for auto hang-up
    auth_token: str|None=None,       # required for auto hang-up
    region: str|None=None, edge: str|None=None,   # pair together for Twilio REST edge
    params: TwilioFrameSerializer.InputParams|None=None,
)
```
**InputParams:** `twilio_sample_rate=8000`, `sample_rate: int|None=None` (else from StartFrame), `auto_hang_up=True`, `ignore_rtvi_messages=True`, `resampler_clear_after_secs=0.2` (set `None` for irregular telephony gaps — *docs-only, verify in your pin*). `base_url` appears in docs but the released source builds URL from region/edge — **version-dependent, verify**.
- **`auto_hang_up`**: on `EndFrame`/`CancelFrame`, POSTs Twilio REST `Calls/{call_sid}.json` with `Status=completed` (HTTP Basic `account_sid:auth_token`). **`__init__` raises `ValueError` if `auto_hang_up=True` and any of call_sid/account_sid/auth_token missing.**
- **8 kHz µ-law + resampling built in** via `pipecat.audio.utils.pcm_to_ulaw`/`ulaw_to_pcm` with separate in/out resamplers. Inbound `media.payload` (base64 µ-law@8k) → `InputAudioRawFrame`; outbound `AudioRawFrame` → µ-law@8k → `{"event":"media","streamSid":...}`. DTMF `{"event":"dtmf"}` → `InputDTMFFrame(KeypadEntry(digit))`.
- **Set pipeline sample rates to 8000 in/out for Twilio** (`PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000)`).

### TwiML — bidirectional (`<Connect><Stream>`, not listen-only `<Start>`)
```xml
<Response><Connect><Stream url="wss://your-host.ngrok.io/ws" /></Connect></Response>
```
- **Inbound:** number's "A call comes in" webhook (POST) returns this XML (or a TwiML Bin). The dev runner **auto-serves this stub on `POST /`** when launched `-t twilio -x <proxy>` (it needs `--proxy` to fill the `wss://.../ws` URL). Media WS is always `/ws`.
- **Outbound:** `calls.create(...)` with the same TwiML; attach `<Parameter name=".." value=".."/>` → arrive in Twilio's `start.customParameters` → exposed as `runner_args.call_data["body"]`.
- **Bot code is identical inbound vs outbound** — only call origination and the `call_data` lookup differ.

### Interruptions (built in)
On barge-in the pipeline emits `InterruptionFrame` (was `StartInterruptionFrame`); the serializer converts it to Twilio **`clear`** (`{"event":"clear","streamSid":...}`) which flushes Twilio's buffered outbound audio. `mark` events are available on the Twilio protocol for explicit playback-completion tracking. (You no longer need the old manual InterruptionHandler processor.)

Sources: https://docs.pipecat.ai/api-reference/server/services/transport/fastapi-websocket · https://docs.pipecat.ai/api-reference/server/services/serializers/twilio · https://docs.pipecat.ai/pipecat/telephony/twilio-websockets · https://reference-server.pipecat.ai/en/latest/_modules/pipecat/serializers/twilio.html · https://github.com/pipecat-ai/pipecat-examples/tree/main/twilio-chatbot

---

## 5. Browser / WebRTC path

**Imports (current):**
```python
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection, IceServer
from pipecat.transports.base_transport import TransportParams
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIObserver, RTVIObserverParams
```
Install extra: `pipecat-ai[webrtc]` (audio via `aiortc`).

### SmallWebRTCTransport / SmallWebRTCConnection
`SmallWebRTCTransport(webrtc_connection: SmallWebRTCConnection, params: TransportParams, input_name=None, output_name=None)` — SmallWebRTC uses **base `TransportParams`** (no subclass). Event handlers: `on_client_connected`, `on_client_disconnected`, `on_app_message(transport, message, sender)`.

`SmallWebRTCConnection(ice_servers: list[str]|list[RTCIceServer]|None=None, connection_timeout_secs=60)` (`IceServer` = alias for aiortc `RTCIceServer`). Methods: `await initialize(sdp, type)`, `get_answer()` → `{"sdp","type","pc_id"}`, `await renegotiate(sdp, type, restart_pc=False)`, `send_app_message(msg)`, `pc_id`; events `connecting/connected/closed/failed/new/track`. **TURN required** for prod / strict-NAT / macOS-Docker. **HTTPS required** in prod (mic/cam blocked on insecure origins). Env `PIPECAT_SCTP_MAX_CHUNK_SIZE` (default 1100) if the data channel stalls on low-MTU paths.

### Signaling — `POST /api/offer`
Browser POSTs its SDP offer `{sdp, type, pc_id?, restart_pc?}`; server returns the SDP answer JSON. Minimal pattern:
```python
@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    conn = SmallWebRTCConnection(ice_servers)
    await conn.initialize(sdp=request["sdp"], type=request["type"])
    background_tasks.add_task(run_bot, conn)
    return conn.get_answer()          # {"sdp","type","pc_id"}
```
Trickle-ICE candidates via `PATCH /api/offer`. Helper the runner uses: `pipecat.transports.smallwebrtc.request_handler.SmallWebRTCRequestHandler(ice_servers=None, esp32_mode=False, host=None, connection_mode=ConnectionMode.MULTIPLE)` → `await handle_web_request(request, cb)`. **There is no public `SmallWebRTCPrebuiltUI` class** — the prebuilt browser UI is a *dev-runner* feature served at **`/client`**.

### RTVI wiring — now AUTO
`PipelineWorker` **auto-inserts `RTVIProcessor` at pipeline start and registers `RTVIObserver`** (constructor `enable_rtvi=True` by default). You do **not** add them to `Pipeline([...])`. Access the processor via `worker.rtvi`; customize via `PipelineWorker(rtvi_processor=..., rtvi_observer_params=RTVIObserverParams(...))`.
```python
worker = PipelineWorker(pipeline, params=PipelineParams(enable_metrics=True, enable_usage_metrics=True))

@worker.rtvi.event_handler("on_client_ready")
async def _(rtvi):
    await worker.queue_frames([LLMRunFrame()])     # bot-ready is set automatically
```
- **`RTVIProcessor`** = inbound client→server messages (client-ready, actions, `client-message`, DTMF, UI events). **`RTVIObserver`** = outbound internal frames → RTVI wire messages (speaking state, transcriptions, LLM/TTS, metrics, `RTVIServerMessageFrame`). Push a custom server→client message from any processor: `push_frame(RTVIServerMessageFrame(data=...))` → client `onServerMessage`.
- **RTVIObserverParams** (dataclass, notable defaults): `bot_llm_enabled=True`, `bot_tts_enabled=True`, `bot_speaking_enabled=True`, `user_transcription_enabled=True`, `metrics_enabled=True`, `system_logs_enabled=False`, `bot_audio_level_enabled=False`.
- **Classic manual wiring** (still valid via deprecated `PipelineTask`, and what most old tutorials show): `RTVIProcessor` right after `transport.input()` in the pipeline + `observers=[RTVIObserver(rtvi)]` on the task.
- RTVI server protocol version: **2.1.0**.

### Client that connects
`@pipecat-ai/client-js` + `@pipecat-ai/small-webrtc-transport` (server↔client transports are a matched pair). Connect param is **`webrtcUrl`** (the `/api/offer` endpoint) — see §6.

> **CONFLICT TO VERIFY — `vad_analyzer` placement.** The Twilio/WebRTC reference-server signatures **no longer list `vad_analyzer`** on `TransportParams`/`FastAPIWebsocketParams`, and the current **Flows quickstart** puts VAD on the user aggregator: `LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))`. **But** the runner-guide `create_transport` examples still pass `vad_analyzer=SileroVADAnalyzer()` to `DailyParams`/`TransportParams`. Likely both are accepted transitionally; **prefer the user-aggregator placement for new code** and pin-check your version.

Sources: https://docs.pipecat.ai/api-reference/server/services/transport/small-webrtc · https://docs.pipecat.ai/api-reference/server/rtvi/{introduction,rtvi-processor,rtvi-observer} · https://reference-server.pipecat.ai/en/latest/api/pipecat.transports.smallwebrtc.{transport,connection,request_handler}.html · https://github.com/pipecat-ai/pipecat-examples/blob/main/p2p-webrtc/video-transform/server/server.py

---

## 6. Client SDKs (JS / React)

**Versions (npm `dist-tags`, verified):** `@pipecat-ai/client-js` **1.13.0** · `@pipecat-ai/client-react` **1.8.1** · `@pipecat-ai/small-webrtc-transport` **1.10.6** · `@pipecat-ai/daily-transport` **1.6.8** · `@pipecat-ai/voice-ui-kit` **0.13.0**.

> **Rename: `RTVIClient` → `PipecatClient`** (class, provider, audio, video, hooks). Event system stays RTVI-prefixed (`RTVIEvent`, `useRTVIClientEvent`, `RTVIMessage`). `RTVIClient`→`PipecatClient`, `RTVIClientProvider`→`PipecatClientProvider`, `RTVIClientAudio`→`PipecatClientAudio`, `useRTVIClient`→`usePipecatClient`. Whether the old aliases still ship in 1.13 is unconfirmed — write `Pipecat*`.

### client-js (`new PipecatClient(options)`)
`PipecatClientOptions`: `transport: Transport` (required, e.g. `new SmallWebRTCTransport({...})`), `callbacks: RTVIEventCallbacks`, `enableMic=true`, `enableCam=false`, `enableScreenShare=false`, `disconnectOnBotDisconnect=true` (since 1.7.0), `timeout` (ms).

**Connect lifecycle — the endpoint URL comes from *your* server:**
- `startBot(params: APIEndpoint)` → POSTs to your REST endpoint, returns `TransportConnectionParams`. `APIEndpoint = {endpoint, headers?, requestData?, timeout?}` (auth in `headers`, prompt/config in `requestData`).
- `connect(connectParams?)` → establishes session, resolves on bot-ready. **Passing a `ConnectionEndpoint` directly to `connect()` is deprecated since 1.2.0** — fetch params via `startBot`.
- `startBotAndConnect(params: APIEndpoint)` → the common one-shot.
```ts
await pcClient.startBotAndConnect({ endpoint: "/api/start", requestData: { llm_provider: "openai" } });
// or give transport params directly (no server round-trip):
await pcClient.connect({ webrtcUrl: "/api/offer" });        // SmallWebRTC
await pcClient.connect({ url: roomUrl, token: roomToken }); // Daily
```
Other methods: `initDevices()`, device get/update, `sendClientMessage/sendClientRequest`, `sendText`, `appendToContext`, `registerFunctionCallHandler`, `sendDTMF`; accessors `.state/.connected/.transport`. `TransportState`: `disconnected|initializing|initialized|authenticating|authenticated|connecting|connected|ready|disconnecting|error`.

**Key callbacks:** `onBotReady(BotReadyData)`, `onUserTranscript(TranscriptData{text,final,timestamp,user_id})`, **`onBotTranscript` DEPRECATED → `onBotOutput(BotOutputData{text,spoken,aggregationType})`**, `onBotTtsText`, `onBotLlmText`, `onUser/BotStartedSpeaking`, `onServerMessage(data)`, `onError(RTVIMessage)` (`data.fatal`→auto-disconnect), `onLLMFunctionCallInProgress`.

### client-react
Components: `PipecatClientProvider client={pcClient}` (also provides conversation state), `PipecatClientAudio` (headless, mounts hidden bot-audio `<audio>`), `PipecatClientVideo participant=...`, `PipecatClientMic/CamToggle` (render-prop), `VoiceVisualizer participantType=...`.
Hooks: `usePipecatClient()`, `useRTVIClientEvent(RTVIEvent.X, useCallback(fn,[]))` (preferred over `client.on` in React), `usePipecatClientTransportState()`, `usePipecatClientMediaDevices()`, `usePipecatClientMediaTrack(kind, participant)`, `usePipecatClientMic/CamControl()`, `usePipecatConversation({onMessageCreated,onMessageUpdated})` → `{messages, injectMessage}` (assistant text split into `{spoken,unspoken}` for live styling).

### Transports
- **`@pipecat-ai/small-webrtc-transport`** `new SmallWebRTCTransport({ iceServers?, waitForICEGathering?, webrtcUrl?, audioCodec?, videoCodec?, mediaManager? })`; connect `{ webrtcUrl }` (**renamed from `connectionUrl` in 1.2.0** — stale READMEs still show `connection_url`). Built-in reconnection (≤3 attempts).
- **`@pipecat-ai/daily-transport`** `new DailyTransport({dailyFactoryOptions?})`; connect `{ url, token }` (Daily room + meeting token) or `{ endpoint }`. Wraps `@daily-co/daily-js ^0.90`.

### voice-ui-kit (0.13.0) — **embeddable, not full-page-only**
Tailwind 4 + shadcn primitives; docs `voiceuikit.pipecat.ai`. Import styles once: `import '@pipecat-ai/voice-ui-kit/styles'`. Three tiers:
1. **`ConsoleTemplate`** — drop-in debug/console; fills its parent, embeds anywhere: `<ConsoleTemplate transportType="smallwebrtc" connectParams={{ webrtcUrl: "/api/offer" }} noUserVideo />`.
2. **`PipecatAppBase`** — headless base for a fully custom UI; owns the client, exposes `{ client, handleConnect, handleDisconnect, error }` via render-prop. Props `transportType`, `connectParams`, `noThemeProvider`.
3. **Building blocks** to compose in your own page: `ConnectButton`, `ControlBar`, `UserAudioControl`, `VoiceVisualizer`, `ErrorCard`, `ThemeProvider`, `Card`, etc. `transportType` ∈ `smallwebrtc|daily|websocket`. (A shadcn-registry "copy source into your app" path is WIP.)

Sources: https://docs.pipecat.ai/api-reference/client/js/{client-constructor,client-methods,callbacks,transports/small-webrtc,transports/daily} · https://docs.pipecat.ai/api-reference/client/react/{components,hooks} · https://voiceuikit.pipecat.ai · npm `registry.npmjs.org/-/package/@pipecat-ai/<pkg>/dist-tags`

---

## 7. Observability (metrics, transcripts, observers)

### Metrics
`PipelineParams(enable_metrics=…, enable_usage_metrics=…, report_only_initial_ttfb=…, send_initial_empty_metrics=…)` (see §2 for defaults; both `enable_*` default **False**). `enable_metrics` → per-service TTFB/TTFA/processing; `enable_usage_metrics` → LLM tokens + TTS characters (**per-interaction, not cumulative**).

**`MetricsFrame`** (`from pipecat.frames.frames import MetricsFrame`) carries `frame.data: list[MetricsData]`. Data classes (`from pipecat.metrics.metrics import …`), all with `processor: str` (e.g. `"DeepgramSTTService#0"` — how you attribute a metric to a service) + optional `model`:
| Class | Field |
|---|---|
| `TTFBMetricsData` | `value: float` (s) |
| `TTFAMetricsData` | `ttfa`, `ttfb`, `leading_silence` (TTS) |
| `ProcessingMetricsData` | `value: float` |
| `LLMUsageMetricsData` | `value: LLMTokenUsage(.prompt_tokens,.completion_tokens)` |
| `TTSUsageMetricsData` | `value: int` (chars) |
| `TextAggregationMetricsData`, `TurnMetricsData` | — |

Capture: `observers=[MetricsLogObserver(include_metrics={LLMUsageMetricsData, TTSUsageMetricsData})]`, or a custom `FrameProcessor` after TTS inspecting `MetricsFrame.data`.

### Transcripts
`from pipecat.processors.transcript_processor import TranscriptProcessor` — `TranscriptProcessor(process_thoughts=False)`. One instance → `.user()` (place after STT) + `.assistant()` (after TTS output), sharing `on_transcript_update(processor, frame)` where `frame.messages: list[TranscriptionMessage]` and **`TranscriptionMessage(role: str, content: str, timestamp: str|None)`** (ISO-8601). Emits incrementally on final utterances.
Newer alternative (recommended): turn events on the aggregators — `on_user_turn_stopped(agg, strategy, UserTurnStoppedMessage)` / `on_assistant_turn_stopped(agg, AssistantTurnStoppedMessage)` (`.role/.content/.timestamp`). Caveat: realtime/S2S mode gives `UserTurnStoppedMessage.content = None` (use `on_user_turn_message_added`).

### Observers & per-turn latency
`from pipecat.observers.base_observer import BaseObserver` — override `on_push_frame(FramePushed)`, `on_process_frame(FrameProcessed)`, `on_pipeline_started()`; `FramePushed` has `source, destination, frame, direction, timestamp`. **Attach via the worker constructor `observers=[…]`, NOT PipelineParams** (`PipelineParams.observers` is deprecated). Built-ins (`pipecat.observers.loggers.*`): `LLMLogObserver`, `TranscriptionLogObserver`, `MetricsLogObserver`, `TurnTrackingObserver`, `UserBotLatencyObserver`, `StartupTimingObserver`; + `RTVIObserver`.
- **`TurnTrackingObserver(turn_end_timeout_secs=2.5)`** — auto-on when `enable_turn_tracking=True` (default). Events `on_turn_started(obs, turn_number)`, `on_turn_ended(obs, turn_number, duration, was_interrupted)`. (Add a custom instance only with `enable_turn_tracking=False` to avoid a duplicate.)
- **Per-turn STT/LLM/TTS latency**, three combinable paths: (1) `MetricsFrame` per-service TTFB/tokens keyed by `processor`; (2) `TurnTrackingObserver` + `UserBotLatencyObserver` for turn boundaries + end-to-end user→bot latency; (3) **OpenTelemetry** (`PipelineWorker(enable_tracing=True)`) auto-attaches `UserBotLatencyObserver` + `TurnTraceObserver`, services decorated `@traced_llm`/`@traced_tts` emit per-turn spans with per-service TTFB/processing/token attributes.

Sources: https://docs.pipecat.ai/pipecat/fundamentals/metrics · https://docs.pipecat.ai/pipecat/fundamentals/saving-transcripts · https://docs.pipecat.ai/api-reference/server/utilities/observers/{observer-pattern,turn-tracking-observer} · https://reference-server.pipecat.ai/en/stable/api/pipecat.processors.transcript_processor.html

---

## 8. Resilience

### ServiceSwitcher (fallback services) — new; in stable docs+API
`from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual, ServiceSwitcherStrategyFailover`. A specialized `ParallelPipeline` (works for STT/TTS/LLM/any processor); only the active service processes frames.
```python
switcher = ServiceSwitcher(services=[primary_stt, backup_stt],
                           strategy_type=ServiceSwitcherStrategyFailover)  # pass the CLASS
@switcher.strategy.event_handler("on_service_switched")
async def _(strategy, service): ...   # app-level recovery
```
- **Manual** (default): switch via `ManuallySwitchServiceFrame(service=...)` (`from pipecat.frames.frames`). **Failover** (subclass): auto-advances on a **non-fatal `ErrorFrame`** from the active service (round-robin). Fatal errors still shut the pipeline down.
- `LLMSwitcher(llms=[...], strategy_type=...)` appears in flows examples (LLM wrapper; **exact import unconfirmed** — `ServiceSwitcher` is the confirmed mechanism).

### User idle
**Deprecated:** `from pipecat.processors.user_idle_processor import UserIdleProcessor` — `UserIdleProcessor(callback, timeout)`; callback may be 1-arg `(processor)` or **2-arg `(processor, retry_count) -> bool`** (auto-detected; return False to stop). `processor.retry_count`.
**Current replacement** (`LLMUserAggregatorParams(user_idle_timeout=5.0)`): events `on_user_turn_idle(agg)` / `on_user_turn_started(agg, strategy)`; backed by `UserIdleController` (correctly suppresses the timer during active turns/pending function calls). Runtime toggle: `UserIdleTimeoutUpdateFrame(timeout=…)` (0 disables).

### Call duration / max session
- **Idle detection ≠ max duration.** Idle is on the worker: `idle_timeout_secs=300` (None disables), `idle_timeout_frames=(BotSpeakingFrame, UserSpeakingFrame)`, `cancel_on_idle_timeout=True`, `cancel_runner_on_idle_timeout=True`; handler `on_idle_timeout(worker)`.
- **Hard cap = manual asyncio timer** → speak goodbye (`TTSSpeakFrame`) then `await worker.queue_frame(EndFrame())` (graceful). **Pipecat Cloud** enforces platform `max_session_duration` (default **7200 s**, forced, no goodbye).

### Errors & shutdown
`from pipecat.frames.frames import ErrorFrame, FatalErrorFrame` (both `SystemFrame`, never dropped). `ErrorFrame(error: str, fatal=False, processor=None, exception=None)`; `FatalErrorFrame` has `fatal=True`. Raise from a processor via `await self.push_error(msg, exception=None, fatal=False)`. Propagates **upstream** → `on_pipeline_error(worker, frame)`; if fatal, worker cancels after the handler. Non-fatal ErrorFrames are what Failover intercepts.
| Shutdown | Behavior | Trigger |
|---|---|---|
| `EndFrame` | graceful (drains) | `await worker.queue_frame(EndFrame())` |
| `CancelFrame` | immediate | `await worker.cancel()` |
| `EndWorkerFrame`/`CancelWorkerFrame` | graceful/immediate **from inside** pipeline | `push_frame(EndWorkerFrame(), DOWNSTREAM)` |
Terminal state → `on_pipeline_finished(worker, frame)` (Stop/End/Cancel) for cleanup. (`EndTaskFrame`/`CancelTaskFrame`/`task.cancel()` are the deprecated aliases.)

Sources: https://docs.pipecat.ai/api-reference/server/utilities/service-switchers/service-switcher · https://docs.pipecat.ai/pipecat/fundamentals/detecting-user-idle · https://docs.pipecat.ai/pipecat/learn/pipeline-termination · https://docs.pipecat.ai/api-reference/server/frames/system-frames · https://docs.pipecat.ai/api-reference/server/pipeline/pipeline-idle-detection

---

## 9. Service classes (pip extra · import · key params)

**Universal pattern:** `Service.Settings(...)` + `settings=` is canonical (since 0.0.105). Old `model=`/`voice=`/`InputParams`+`params=` still work but deprecated → removed 2.0.0. Runtime deltas via `LLM/TTS/STTUpdateSettingsFrame(delta=Service.Settings(...))`.

- **Deepgram STT** — `pipecat-ai[deepgram]` · `from pipecat.services.deepgram.stt import DeepgramSTTService`. `DeepgramSTTService(api_key, base_url="", encoding="linear16", sample_rate=None, live_options=None [deprecated], settings=DeepgramSTTService.Settings(...))`. Settings defaults: `model="nova-3-general"`, `language=Language.EN`, `interim_results=True`, `punctuate=True`, `smart_format=False`. Multilingual: `Settings(language="multi")`. `from pipecat.transcriptions.language import Language`.
- **Anthropic LLM** — `pipecat-ai[anthropic]` · `from pipecat.services.anthropic.llm import AnthropicLLMService`. `AnthropicLLMService(api_key, settings=AnthropicLLMService.Settings(...), client=None [inject Bedrock/Vertex], retry_timeout_secs=5.0, retry_on_timeout=False)`. Settings: `model` (**source default `claude-sonnet-4-6`; docs examples `claude-sonnet-4-5-20250929` — set explicitly**), `system_instruction=None`, `max_tokens=4096`, `enable_prompt_caching=False`, `temperature/top_k/top_p=NOT_GIVEN`, `thinking=NOT_GIVEN`. Has `run_inference(context, max_tokens, system_instruction)` for one-shot calls.
- **Cartesia TTS** — `pipecat-ai[cartesia]` · `from pipecat.services.cartesia.tts import CartesiaTTSService` (+ `CartesiaHttpTTSService`). `CartesiaTTSService(api_key, sample_rate=None, encoding="pcm_s16le", container="raw", settings=CartesiaTTSService.Settings(...))`. Settings: `model` (`"sonic-3.5"`/`"sonic-2"`), `voice` (id), `language`, `generation_config`. Word timestamps auto-on.
- **ElevenLabs TTS** — `pipecat-ai[elevenlabs]` · `from pipecat.services.elevenlabs.tts import ElevenLabsTTSService` (+ Http). `ElevenLabsTTSService(api_key, sample_rate=None, auto_mode=None, settings=ElevenLabsTTSService.Settings(...))`. Settings: `model`, `voice`, `language` (**only honored by multilingual models** `eleven_multilingual_v2`/`eleven_turbo_v2_5`/`eleven_flash_v2_5`), `stability`, `similarity_boost`, `style`, `speed`, `apply_text_normalization`.
- **OpenAI** — `pipecat-ai[openai]` · LLM `from pipecat.services.openai.llm import OpenAILLMService` (default `gpt-4.1`; `api_key` via base). **Quickstart uses `OpenAIResponsesLLMService`** (Responses API) — pick per Chat-Completions vs Responses. STT `OpenAISTTService` (models `gpt-4o-transcribe`/`gpt-4o-mini-transcribe`/`whisper-1`). TTS `OpenAITTSService` (`Settings(voice, model, instructions, speed)`; 24 kHz PCM; voices alloy/ash/ballad/cedar/coral/echo/fable/marin/nova/onyx/sage/shimmer/verse).

### Universal context (the recent change)
`from pipecat.processors.aggregators.llm_context import LLMContext` · `from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams, LLMAssistantAggregatorParams`. Frames: `LLMContextFrame`, run inference with `LLMRunFrame`.
```python
context = LLMContext(messages=[...], tools=ToolsSchema([...]))   # provider-agnostic; enables runtime LLM switching
user_agg, assistant_agg = LLMContextAggregatorPair(context)      # returns a TUPLE
```
Tools: `FunctionSchema` (`pipecat.adapters.schemas.function_schema`) + `ToolsSchema` (`…tools_schema`); a tool carrying a `handler` is auto-registered (no `register_function`). `LLMUserAggregatorParams`: `user_idle_timeout`, `vad_analyzer`, `user_turn_stop_timeout`, `user_turn_strategies` (incl. smart-turn `LocalSmartTurnAnalyzerV3`), `user_mute_strategies`. Deprecated per-provider `OpenAILLMContext` (`pipecat.processors.aggregators.openai_llm_context`) lingers only for realtime/S2S services that don't yet support universal context. Prefer `system_instruction` on the LLM Settings over a system message in `messages`.

Sources: reference-server pages `pipecat.services.{deepgram.stt,anthropic.llm,cartesia.tts,elevenlabs.tts,openai.llm}` · https://docs.pipecat.ai/pipecat/migration/migration-1.0 · https://docs.pipecat.ai/pipecat/learn/context-management

---

## 10. Project conventions & deployment

### 10.1 CLI + `pipecat init` (`pipecat-ai-cli` 1.3.0)
First-party `uv`-installed CLI (not cookiecutter). `uv tool install pipecat-ai-cli` (or via framework extra `uv tool install "pipecat-ai[cli]"`). Binary `pipecat` (alias `pc`). Commands: `pipecat init`, `pipecat cloud …`, `pipecat tail` (live session dashboard), `pipecat eval`.
`pipecat init [DIR] [OPTS]` always writes `AGENTS.md` + `CLAUDE.md` (never overwrites existing; `--overwrite-guide` to refresh) and optionally scaffolds a **runnable** bot. **Bare `init` is an interactive wizard — pass any scaffold flag or `--config` to run non-interactively (required for agents/CI; the wizard hangs headless).** Preset: `pipecat init quickstart`.

**Generated layout:**
```
my-bot/
├── AGENTS.md            # Pipecat coding-agent guide
├── CLAUDE.md            # just "@AGENTS.md"
├── server/
│   ├── bot.py           # runnable: services + pipeline + runner
│   ├── evals/           # with --eval (starter_text.yaml, starter_audio.yaml)
│   ├── pyproject.toml   # uv-managed (NOT requirements.txt)
│   ├── .env.example
│   ├── Dockerfile       # with --deploy-to-cloud
│   └── pcc-deploy.toml  # with --deploy-to-cloud
├── client/              # optional (react|vanilla)
└── README.md
```
Key flags (discover via `pipecat init --help` / `--list-options` JSON): `--bot-type web|telephony`, `-t/--transport` (`daily,smallwebrtc,twilio,telnyx,plivo,exotel,daily_pstn,twilio_daily_sip`, repeatable), `--mode cascade|realtime`, `--stt/--llm/--tts <id>`, `--realtime openai_realtime|gemini_live_realtime`, `--client-framework react|vanilla|none`, `--client-server vite|nextjs`, `--eval`, `--deploy-to-cloud`, `--enable-krisp`, `--config <json>`, `--dry-run`.

### 10.2 Development runner (recap, §2) — the production caveat
`pipecat.runner` is a **local-dev tool, explicitly not for production**. Canonical bot: `async def bot(runner_args): transport = await create_transport(runner_args, transport_params); …` + `if __name__=="__main__": from pipecat.runner.run import main; main()`. Serves `localhost:7860`, UI at `/client`, session start `POST /start` (**same contract as Pipecat Cloud** — a client built against the runner runs unchanged on PCC). Client picks transport per session via `"transport"` in the `/start` body. `RunnerArguments` subclasses (`pipecat.runner.types`): `DailyRunnerArguments(room_url, token)`, `SmallWebRTCRunnerArguments(webrtc_connection)`, `WebSocketRunnerArguments(websocket, transport_type)`, `LiveKitRunnerArguments`, `EvalRunnerArguments`; base fields `body`, `call_data`, `session_id`, `cli_args`.

### 10.3 Pipecat Cloud deploy + `pcc-deploy.toml`
`pipecat cloud deploy [AGENT] [IMAGE] [OPTS]`. Container-based. Secrets = **secret sets** (`pipecat cloud secrets …`, bind `--secrets`). Agent name: lowercase/digits/hyphens, ≤54 chars.

**Installed-pin correction (verified 2026-07-28):** the resolved `pipecat-cli==0.1.15`
requires the positional `IMAGE`; its installed `DeployConfigParams` has no
cloud-build/context/Dockerfile fields. The earlier upstream example that
allowed omitting the image does not describe this pin. Voicekit therefore
generates a secret-free build context with `--prepare-only`, prints the exact
`docker build` and `docker push` commands for an operator-selected immutable
tag, and deploys that exact tag. It does not claim a Pipecat-managed build.
The generated image uses a glibc Python base, a non-root runtime user, and the
installed `RunnerArguments`/`main` entrypoint.
```toml
agent_name = "my-voice-agent"          # required
image = "you/my-agent:0.1"             # or build_id / cloud build
region = "us-west"
secret_set = "my-agent-secrets"
agent_profile = "agent-1x"             # agent-1x|2x|3x
max_session_duration = 300             # 60..14400 (default 7200)
[scaling]
min_agents = 1                         # keep warm → no cold start (default 0)
max_agents = 20                        # 1..50
[krisp_viva]
audio_filter = "tel"                   # tel|pro | null
[build]
context_dir = "."
dockerfile = "Dockerfile"
```

### 10.4 AGENTS.md conventions + Context Hub — **adopt for our toolchain**
Pipecat's generated **AGENTS.md** (`src/pipecat/cli/agent_templates/AGENTS.md`) codifies: (1) **scaffold first, never hand-write boilerplate**; (2) scaffold **non-interactively**; (3) **"don't guess — learn then verify"**: reflexively `check_deprecation` any symbol typed from memory (canonical `PipelineTask`→`PipelineWorker`) because training data is stale; (4) **`--eval` behavioral harness** (`pipecat.evals`, YAML scenarios) to verify behavior headless; (5) **use `uv`**; (6) `CLAUDE.md` = `@AGENTS.md` import.

**Pipecat Context Hub** (`pipecat-ai-context-hub` 0.2.1) — a **local-first** index (ChromaDB + SQLite FTS5) of docs + framework source (AST-indexed) + examples + Flows + **TS SDKs**, as **both a CLI and an MCP server**:
```bash
uvx pipecat-ai-context-hub refresh
uvx pipecat-ai-context-hub check-deprecation PipelineTask
claude mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve
```
MCP tools: `search_docs`, `search_examples`, `search_api`, `get_doc`, `get_example`, `get_code_snippet`, `check_deprecation`, `get_hub_status` (version-aware; extra repos via `PIPECAT_HUB_EXTRA_REPOS`). Also `docs.pipecat.ai/llms.txt` (index) + `llms-full.txt` (bulk). **This is the antidote to the rename/staleness churn documented throughout — recommend wiring it into our build loop.**

Sources: https://docs.pipecat.ai/api-reference/cli/{overview,init} · https://docs.pipecat.ai/api-reference/cli/cloud/deploy · https://docs.pipecat.ai/pipecat/deployment/running-bots-locally · https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/cli/agent_templates/AGENTS.md · https://docs.pipecat.ai/api-reference/context-hub

---

## Build recommendations for our toolchain (synthesis)

1. **Target the new API surface now** — `PipelineWorker`/`WorkerRunner` (`pipecat.pipeline.worker`, `pipecat.workers.runner`), per-provider transport paths, `LLMContext`+`LLMContextAggregatorPair`, `Service.Settings(...)`, `PipecatClient`. Old names work but are 2.0.0-removal aliases; every online example is stale.
2. **Adopt the `bot(runner_args)` + `create_transport` shape** so one codebase serves browser (SmallWebRTC), Twilio, and Pipecat Cloud unchanged. Own the `/start` + `/api/offer` contract.
3. **For a long-lived FastAPI host:** one process, many concurrent `PipelineWorker`s; `WorkerRunner()` then `await runner.run(auto_end=False)`. Cap calls with a manual `asyncio` timer → `EndFrame`; rely on `idle_timeout_secs` for dead-air.
4. **Observability:** `enable_metrics=enable_usage_metrics=True` + `TranscriptProcessor` (or aggregator turn events) + `TurnTrackingObserver`/`UserBotLatencyObserver`; OTel (`enable_tracing=True`) for per-turn/per-service spans.
5. **Resilience:** `ServiceSwitcher(strategy_type=ServiceSwitcherStrategyFailover)` for STT/LLM/TTS fallback (validate on your pin); `on_pipeline_error`/`on_pipeline_finished` for cleanup.
6. **Wire in `pipecat-ai-context-hub` (MCP + `check_deprecation`)** and pin an exact `pipecat-ai` version, diffing against `reference-server.pipecat.ai/en/stable` — the prose docs still contain deleted `transports.network.*` imports.

### Open items to verify against the pinned version
- Exact current `pipecat-ai` (PyPI page showed **1.5.0**; CHANGELOG shows **1.6.0** dated 2026-07-21 — pin and check).
- `vad_analyzer` on transport params vs `LLMUserAggregatorParams` (§5 conflict).
- `TwilioFrameSerializer` `base_url`/`resampler_clear_after_secs`; `LLMSwitcher` import path; whether `RTVIClient` JS aliases still ship in 1.13.
