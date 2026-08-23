# Decisions

Decisions are append-only. A superseding decision links the earlier entry and explains the migration impact.

## 2026-08-02 — Deterministic voices and Pipecat/Claude tool turns

- Resolve `Voice(id=None)` to explicit checked-in IDs on both runtimes instead
  of inheriting SDK-specific defaults. The curated defaults are Cartesia
  Skylar (`db6b0ed5-d5d3-463d-ae85-518a07d3c2b4`), ElevenLabs' installed
  LiveKit default (`hpp4J3VqNfWAUOO0d1Us`), and OpenAI `alloy`. The Cartesia
  choice was verified against the authenticated 2026-08-02 voice catalog.
- Disable Anthropic adaptive thinking for the pinned Pipecat 1.6.0 + Claude
  Sonnet 5 path. A credentialed tool-call run proved that Sonnet can emit a
  signature-only thinking block, which this Pipecat adapter persists without
  a role and cannot convert on the following turn. This is a narrow installed-
  version workaround, not a custom conversation abstraction.

## 2026-07-28 — Launch documentation and security evidence

- Generate the config, public Python, webhook, and error reference from
  importable models, declared `__all__` exports, validated Pydantic schemas, and
  the executable error catalog. Commit the Markdown and fail CI on drift so the
  readable reference and machine snapshots have the same source of truth.
- Execute only the marked runtime quickstart command blocks from a fresh wheel.
  Provider-mocked native runtime/media evidence is valid for deterministic
  first-run and documentation checks, but never substitutes for a credentialed
  microphone conversation.
- Check in one locally synthesized MP3 per first-party recipe as an illustrative
  conversation preview. The transcripts and deterministic generation command
  are committed; these files make no provider, naturalness, or latency claim.
- Treat “image secret scan” as an actual canonical production-container test:
  build the generated Python 3.14 image, verify the runtime-only fixture secret
  is absent from image metadata, start it read-only/non-root, prove health and
  SIGTERM drain, then run Trivy vulnerability and secret scanners. Dockerfile
  inspection alone is not release evidence.

## 2026-07-26 — P0 defaults accepted

- **Product name:** keep `voicey` in package, CLI, entry-point groups, docs, and examples through the build. Do not publish or register public resources. Prepare `RENAME.md` for the human-selected final name.
- **Reference latency stack:** Deepgram Nova-3 STT, Anthropic Claude LLM, and Cartesia Sonic 3.5 TTS.
- **Simulation judge:** local Ollama by default, with an explicit cloud-model override in config.
- **Vobiz on LiveKit:** run the P3 SIP feasibility spike. If unsupported, expose Vobiz only on the Pipecat path behind the capability registry; do not silently route or downgrade.
- **Storage topology:** Docker/self-host uses local SQLite + protected local artifacts; Fly/Railway uses managed Postgres + object storage; ephemeral Pipecat/LiveKit Cloud workers use the authenticated user-owned results relay.

These choices apply the documented proposals authorized in the build mandate and close the P0/P1 decision gates without requiring an implementation pause.

## 2026-07-26 — Runtime pins and current APIs

- Pin `pipecat-ai==1.6.0`; use core `pipecat.flows` and do not install standalone `pipecat-ai-flows`.
- Pin `livekit-agents==1.6.7`.
- Use only installed current APIs. In particular, call `WorkerRunner.run(auto_end=False)` and use LiveKit `create_inbound_trunk` / `create_dispatch_rule` instead of their deprecated aliases.
- Maintain Python support at 3.11–3.14.

## 2026-07-26 — P1 local repository schema

- SQLite schema version 2 normalizes call timeline, transcript, tool, and
  latency observations, then adds fenced lifecycle, immutable events, delivery
  outbox, recordings, backup retention, and a durable artifact-purge queue.
- Terminal payload and delivery insertion share one transaction; runtime code
  uses the backend-neutral `StorageRepository` protocol. The P3 Postgres
  backend must implement the same contract rather than expose SQL differences.
- Standard Webhooks interoperability pins Python `standardwebhooks==1.1.0`,
  npm `standardwebhooks@1.0.0`, and Go source commit
  `01d6eb75702229a0927c07d52fda7223e201c03d` for the cross-language vector.

## 2026-07-26 — P1 Twilio adapter pin and safety boundary

- Pin `twilio==9.10.9`; the installed SDK confirms that `Calls.create` has no
  native idempotency key.
- Store routing rollback snapshots and outbound intents in the protected,
  `synchronous=FULL` telephony ledger before any external mutation.
- Never retry an ambiguous outbound create. Correlate by the durable intent in
  the status-callback path and bind only a unique reconciliation candidate.
- Require an expected public base for signature validation; forwarded headers
  are accepted only from configured proxies and must reconstruct that origin.

## 2026-07-27 — P1 playground listener and media boundary

- `voicey dev --port N` binds the public runtime/signaling listener to
  loopback port `N` and the admin playground/read API to loopback port `N + 1`.
  Only the public listener is eligible for tunneling.
- Browser sessions use one-use, short-lived bearer tokens derived with domain
  separation from the configured Standard Webhooks secret. Admission and the
  durable call row are reserved before the token response; a successful offer
  binds that call to its peer before PATCH signaling proceeds, while a failed
  authenticated offer consumes the token and terminalizes the reservation.
- Use the installed Pipecat small-WebRTC transport with `WavMediaManager`.
  Its default `DailyMediaManager` dynamically loads a Daily call-machine bundle;
  the audio-only playground does not need that external dependency. This keeps
  the CSP self-hosted except for the configured public signaling origin.
- Serve the embedded SPA with FastAPI 0.140's verified `app.frontend()` API.
  The hatch build hook is the source of wheel assets; its skip variable is for
  verified prebuilt artifacts only.

## 2026-07-27 — Pipecat Evals dependency compatibility

- Install the official `pipecat-ai[evals]==1.6.0` extra as part of
  `voicey[pipecat]`; the P1 harness uses the installed `pipecat eval`
  commands and eval transport rather than a voicey-owned simulator.
- Accept Rich `>=13.9.4,<16` instead of requiring Rich 15. Pipecat 1.6.0's
  `cli` dependency (included by `evals`) pins Rich below 14, and voicey uses
  only APIs present in both supported ranges. The CLI's behavior and output
  contracts are unchanged and remain regression-tested at the resolver-selected
  Rich 13.9.4.

## 2026-07-27 — Canonical Docker packaging and process boundary

- Build the production runtime from the released voicey wheel plus the
  runtime/carrier extras selected in `voicey.jsonc`. An unpublished `.dev0`
  checkout must pass a locally built wheel explicitly; the generator copies it
  mode 0600 and the final image removes all build inputs.
- Install non-voicey `[project].dependencies` separately and run project
  agent/native-flow modules from `/app`. Do not package an arbitrary flat
  project tree as part of the engine distribution.
- Use a Docker-managed local-driver volume, SQLite WAL/FULL, local artifacts,
  and one steady replica. Same-host generations may overlap only for controlled
  handover and share fenced leases; network volumes and cross-host SQLite are
  rejected at startup.
- Let the voicey container supervisor own SIGINT/SIGTERM and both Uvicorn
  listeners. It closes admission before the bounded call drain, flushes due
  result delivery, then exits. Uvicorn's installed server API is used with
  signal capture disabled rather than installing competing handlers.
- Publish only the runtime listener. The web token/records listener remains an
  authenticated internal Compose service surface.

## 2026-07-27 — Twilio–LiveKit SIP authentication boundary

- Supersede the original build-plan research sentence that put credentials on
  the LiveKit inbound trunk. Current LiveKit provider documentation explicitly
  states that Twilio Elastic SIP Trunking cannot use username/password
  authentication for traffic originating at Twilio.
- Scope the LiveKit inbound trunk to the owned E.164 number with no credentials.
  Put the credential-list username/password on the LiveKit outbound trunk that
  terminates into the Twilio SIP domain.
- Enable Twilio secure trunking, use `;transport=tls` on the Twilio origination
  URI, use LiveKit `SIP_TRANSPORT_TLS` outbound, and allow encrypted media on
  both LiveKit trunks.
- Evidence sources: the current LiveKit inbound-trunk, Twilio provider
  quickstart, and secure-trunking guides, plus Twilio Elastic SIP Trunking
  documentation. The installed `livekit-api==1.2.0` request fields were
  separately inspected and are exercised by the local certification suite.

## 2026-07-27 — Twilio Elastic SIP recording reconciliation

- Automatic Elastic SIP trunk recording has no per-trunk completion callback:
  the official endpoint and pinned SDK expose only recording mode and trim.
  Voicey therefore does not claim a callback that Twilio cannot configure.
- Correlate the call through LiveKit's documented built-in participant
  attribute `sip.twilio.callSid`. The LiveKit SIP service maps Twilio's carrier
  header for inbound and outbound participants; outbound attributes may arrive
  after the initial participant response.
- After terminal persistence, bounded polling queries Twilio Core Recordings by
  that CA SID, accepts exactly one completed recording with source `Trunking`,
  downloads it through the existing Basic-authenticated media path, and emits
  the same `call.recording.ready` contract. A missing SID or timeout remains a
  visible pending recording for recovery; it does not mutate the terminal
  event.
- Amend spec §5.2 so certification requires signed callback ingestion when a
  carrier supports it, or documented authenticated-ID reconciliation when it
  does not. This preserves the external result contract while matching the
  real carrier API.

## 2026-07-27 — LiveKit browser credential boundary

- Keep the one-use voicey session token on the authenticated HTTP exchange:
  the browser sends it only as `Authorization: Bearer …` to the public token
  endpoint after the admin listener has durably reserved the call.
- Return a distinct, short-lived, least-privilege LiveKit room credential only
  after that exchange succeeds. Hand it directly to pinned
  `livekit-client==2.21.0`; do not wrap or replace LiveKit's native signaling
  protocol.
- The official client currently carries its scoped room credential during the
  WebSocket join using provider-native query/protocol fields. This is permitted
  and documented explicitly. The voicey token and LiveKit API key/secret
  never enter a URL or browser bundle.
- Validate LiveKit project credentials during `init`, `keys`, and `doctor`
  through `LiveKitAPI.room.list_rooms(ListRoomsRequest())`, an authenticated
  read-only request verified against installed `livekit-api==1.2.0`.

## 2026-07-27 — Appointment recipe LiveKit workflow shape

- Use LiveKit's native Agent-return handoff contract: intake function tools
  return booking, rescheduling, or cancellation `Agent` instances. Preserve the
  shared calendar and transfer tools on each specialist and preserve chat
  context through the handoff.
- Use the installed beta `GetNameTask` and `GetEmailTask` only from
  `on_enter`, where pinned `livekit-agents==1.6.7` permits awaited
  `AgentTask`s. The installed export is `GetEmailTask`, not older
  `GetEmailAddressTask` examples.
- Require explicit ask and confirmation for captured contact fields, and inject
  completed values back into the workflow as untrusted caller data. Keep
  calendar operations in shared typed tools; do not introduce recipe state,
  a flow DSL, or a second tool protocol.

## 2026-07-27 — Unified native testing boundary

- Keep scenario source runtime-neutral, but compile and execute it only through
  installed native evaluators: Pipecat EvalSuite YAML/transport or LiveKit
  `AgentSession.run()`/`RunResult.expect`. The schema is test input, not a
  conversation flow or runtime abstraction.
- Use local Ollama `gemma2:9b` for both persona-only sim-caller planning and
  cited judging by default. A secret-free `tests/voicey-test.jsonc` may select
  an OpenAI-compatible cloud endpoint and names only the key environment
  variable.
- Treat an initial failure as failed even when one or more of the three reruns
  pass. Preserve all four attempts and report their stability percentage.
- Implement LiveKit audio with attachable PCM input/output on the installed
  session, Kokoro caller synthesis, production STT/LLM/TTS services, and
  Moonshine output transcription. Never label text injection with an audio
  modality and claim that it exercised the audio pipeline.
- Leave `--live` fail-closed until the P3 PSTN loopback harness exists. A lower
  tier is never an implicit substitute.

## 2026-08-02 — Tool-capable local judge supersedes Gemma 2

- Supersede the local-model portion of **2026-07-27 — Unified native testing
  boundary**: use Ollama `qwen3:8b` for both persona-only sim-caller planning
  and cited judging by default. The secret-free cloud override is unchanged.
- A credentialed native LiveKit run established that
  `AgentSession.run()`/`RunResult.expect().judge()` includes the agent's tool
  schemas in the judge request. Ollama `gemma2:9b` rejects that request because
  the model does not support tools, so it cannot satisfy the settled native
  testing boundary. Ollama identifies Qwen3 8B as tool-capable and uses Qwen3
  in its official tool-calling examples.
- Keep native LiveKit judging intact instead of stripping tools or introducing
  a parallel judge path. This preserves the runtime-native testing contract.
- Shared appointment scenarios include explicit LiveKit-targeted caller turns
  that confirm the captured name and email. The runner must not auto-confirm
  or bypass those pinned native contact tasks. Pipecat retains its native final
  contact-confirmation boundary; shared goals, mutations, and hard outcomes are
  unchanged. Runtime-targeted turns are filtered before turn budgets/reporting.
- Send Ollama's supported `think: false` request option for deterministic test
  planning and judging. Bound each native LiveKit judgment to 60 seconds and
  the complete native conversation to the scenario's declared duration; a
  timeout is preserved as failed evidence and never hangs the suite.
- Apply content and goal assertions to the last assistant message in a native
  LiveKit run. Tool-using turns may contain a pre-tool acknowledgement and a
  post-tool answer; judging the first message produces a false failure even
  when the native tool output and final response satisfy the criterion.
- Implement `send_after(event="llm_started")` on LiveKit text simulations with
  the installed native `agent_state_changed` event and `interrupt(force=True)`
  before submitting the next run input. Post-interruption contact confirmation
  remains explicit: LiveKit receives a clean cancellation intent, captures and
  confirms the email, then receives the reference; Pipecat may perform its
  native lookup from one interrupted utterance containing reference and email.
- Preserve caller inputs alongside native assistant events in the cited
  transcript. LiveKit run events omit submitted user text; Pipecat Evals keeps
  it in the turn-tagged debug trace rather than `events_seen`. Reconstruct the
  ordered, mock-data transcript from those native sources before judging.
- After LiveKit prebuilt contact tasks return, use deterministic `session.say`
  prompts rather than an instruction-only LLM generation. Anthropic Sonnet 5
  rejects the intermittent assistant-prefill context produced by that extra
  generation. The confirmed task context remains native and subsequent caller
  input drives the specialist LLM normally.
- The text test tier injects a deterministic native `transfer_to_human` stub,
  matching the existing Pipecat eval agent's fake transfer destination. It
  proves tool selection without claiming a SIP transfer or placing a call.
- The privacy-safe voicemail template says only that the team is returning a
  call; “appointment” and “scheduling request” were purposes and contradicted
  the recipe's own no-purpose rule.
- The appointment text scenario allows 20 seconds for the provider-backed
  calendar search/tool/reply roundtrip. A credentialed run measured 13.259
  seconds against the original arbitrary 12-second bound. This does not change
  the §17 synthesized-audio voice-to-voice p50/p95 latency gate.

## 2026-08-03 — Native Anthropic cloud judge and post-session evaluation

- Keep local Ollama `qwen3:8b` as the product default, but honor the operator's
  cloud-only certification choice through the existing explicit override. Add
  `service: "anthropic"` alongside the existing OpenAI-compatible service;
  store only `api_key_env`, and call Anthropic's native Messages API rather than
  assuming an OpenAI-compatible endpoint.
- Use the installed Pipecat 1.6.0 `judge.eval.factory` contract to return its
  native `AnthropicLLMService`. Use LiveKit 1.6.7's native Anthropic plugin for
  `RunResult.expect().judge()`. The runtime workflow remains native on both
  sides; no second evaluator DSL or tool protocol is introduced.
- A credentialed API probe established that `claude-sonnet-5` rejects the
  legacy `temperature` request field. The native Messages adapter therefore
  omits it. The same probe and a direct Pipecat `EvalJudge` call returned valid
  cloud-model evidence.
- The installed LiveKit Anthropic plugin injects a trailing user sentinel only
  for Claude 4.6 model prefixes, while Sonnet 5 also rejects assistant
  prefilling. Apply the plugin's same behavior in a narrow Sonnet 5 wrapper and
  preserve the original chat context.
- Measure a scenario's duration around the native agent conversation only.
  Close the agent session, then execute the collected native goal judgments
  under their own 60-second bounds. Judge latency must never prevent the final
  business tool call or turn a completed conversation into a scenario timeout;
  completed results remain available for native assertions after session close.
- Use Anthropic structured outputs for sim-caller plans and cited transcript
  verdicts. The Messages request supplies `output_config.format` with a strict
  JSON schema; Pipecat's native judge disables adaptive thinking and uses a
  bounded token budget. A real Sonnet 5 structured-output call and a real
  Pipecat `EvalJudge` call both passed on 2026-08-03.
- A Pipecat turn that asserts only a function call must also wait for the next
  native response event. Pipecat Evals emits the function-call event before the
  post-tool model reply; advancing immediately lets the next caller utterance
  interrupt that reply. This is evaluator synchronization, not a conversation
  DSL or a replacement for native events.
- Give every `voicey test` invocation an immutable run id below
  `.voicey/test-runs/`. Attempt numbers are unique only inside one invocation;
  reusing their SQLite file across commands caused correct duplicate-call-id
  rejection and misleading connection failures.
- The Pipecat eval worker supplies an eval-only transfer destination so the
  production transfer function is exercised without contacting a carrier. On
  native eval-client disconnect it persists the terminal event immediately,
  before EvalSuite can stop the bot process; ordinary session shutdown remains
  idempotent.
- Keep runtime-specific caller pacing explicit. Pipecat receives compact
  identity/reference turns and waits through post-tool replies; LiveKit keeps
  the extra native `GetNameTask`/`GetEmailTask` confirmations. The final booking
  response must include date, time, timezone, and reference on both runtimes.
- Credentialed model-API-only certification on 2026-08-03 regenerated both
  appointment projects from current recipe source and ran all seven text cases
  through the production native paths. Pipecat and LiveKit each passed every
  case on the first attempt with Deepgram Nova-3, Claude Sonnet 5, Cartesia
  Sonic 3.5, native runtime judges, typed tools, and durable result checks.
  Local Ollama was not used for this evidence. Audio, PSTN, and physical-input
  gates remain separate and unpromoted.

## 2026-08-03 — Twilio SIP password validation before mutation

- A credentialed Twilio↔LiveKit provision run established that Twilio rejects
  SIP credential passwords unless they contain at least 12 characters, one
  lowercase letter, one uppercase letter, and one number (`21240`). Validate
  this exact rule in `TwilioLiveKitSipConfig` before creating any LiveKit or
  Twilio resource.
- The first observed rejection occurred after trunks and a credential list had
  been created but before number attachment. The provisioner correctly fenced
  the outcome as ambiguous; the exact ledgered operation was then rolled back,
  and both the API and LiveKit console showed zero temporary resources. A retry
  with a compliant generated password passed provision, idempotent reuse, and
  reverse rollback. This evidence does not promote the paid PSTN gate.

## 2026-08-03 — API-only P3 recipe certification and native ownership

- Honor the operator's model-API-only test choice through the existing native
  Anthropic override. Keep local Ollama as the product default, but make no
  Ollama request part of this certification evidence.
- Pace multi-turn provider scenarios around native post-tool responses rather
  than sending the next caller turn into an unfinished reply. Give each tool
  its complete typed facts and reserve explicit confirmation for mutating
  operations. Read-only lead qualification runs immediately once need,
  timeline, budget range, and company size are present.
- Enforce LiveKit restaurant waitlist ownership structurally: the intake Agent
  can search and hand off but cannot invoke `join_waitlist`; only the native
  `WaitlistAgent` receives that mutating tool. The runtime-specific scenario
  asserts the handoff before consent and mutation.
- Select the local text-tier transfer stub from the scenario's expected native
  tool. A warm-transfer scenario therefore exercises
  `warm_transfer_to_human`, while ordinary transfer scenarios retain the cold
  stub. Successful stubs mirror the production result (`status: transferred`)
  and do not claim a carrier call or human acceptance.
- Treat an answering-machine greeting as an explicit mode switch. Each P3
  recipe uses one short generic callback message, reveals no call purpose or
  caller data, and ends without reverting to its normal inbound greeting.
- Six fresh projects ran complete unfiltered suites on 2026-08-03. Restaurant
  passed 5+5, Front Desk 6+6, and Lead Intake 6+6 across native Pipecat and
  LiveKit; all 34 cases passed on their first attempt with Claude Sonnet 5 and
  native Anthropic judging. Audio, JUnit artifact, PSTN, and physical-transfer
  rows remain separate and unpromoted.

## 2026-08-03 — Live account and no-call control-plane boundary

- Promote only the commands that actually returned zero: Twilio's no-charge
  test-credential API contract, Twilio live account/owned-number readiness,
  Twilio↔LiveKit provision/reuse/rollback, Vobiz live account/owned-number
  readiness, and Vobiz↔LiveKit provision/reuse/rollback.
- Retain provider ownership and cleanup evidence without secrets. Twilio,
  Vobiz, and LiveKit inspections showed zero temporary resources after reverse
  rollback; the Vobiz run restored the exact pre-existing Voice API
  application route.
- Do not infer media from control-plane success. No paid PSTN call, completed
  recording, live private briefing, microphone conversation, or physical-
  handset test is green.
- Pipecat Cloud and LiveKit Cloud CLIs authenticate, and AWS STS authenticates,
  but there is no signed deployed results companion, selected immutable worker
  image, or dedicated disposable object bucket. Fly and Railway are
  unauthenticated, and Docker is stopped. No external deployment is promoted.

## 2026-08-02 — Credentialed Vobiz SIP API envelope correction

- A credentialed control-plane run supersedes the mocked credential/list
  shapes used by the 2026-07-27 Vobiz implementation: existing SIP credentials
  are listed from `/trunks/credentials` under an `objects` envelope, while
  owned numbers are returned under an `items` envelope. An empty trunk list is
  represented as `objects: null`, not an empty array.
- Accept the live `items` envelope in the shared strict list parser and keep
  fail-closed exact-id, exact-number, and exact-username matching. Do not fall
  back to fuzzy adoption or rotate the write-only credential.
- Preserve the full create/reuse/reverse-rollback gate. The correction was
  discovered before any trunk, binding, or LiveKit dispatch resource was
  created.
- The next credentialed attempt established that an owned number may already
  be attached to a Voice API application. Snapshot both `application_id` and
  `trunk_group_id`, ledger number-route rollback ownership before mutation,
  detach the current route, require the documented `204` trunk-assignment
  response, and restore the exact prior application or trunk by compare-and-
  swap. The live application-attachment restore also returns `204`. A 400 must
  never be worked around by discarding the existing route.

## 2026-07-28 — Paid PSTN test boundary

- The P3 harness resolves the earlier `--live` placeholder. For Pipecat, use
  the Fixa architectural pattern—an independent native Pipecat caller through
  a Twilio Media Stream—but build it against installed
  `pipecat-ai==1.6.0`, the reference Anthropic model, voicey's signed
  callback boundary, and its durable intent ledger. Fixa is research input,
  not a dependency or copied runtime.
- For LiveKit, use the agent-simulator architectural pattern: an isolated
  native RTC room, a native caller `AgentSession`, and a target outbound SIP
  participant. The referenced simulator's MCP surface is deliberately not
  adopted; voicey contains no MCP product path.
- Live calls are black-box assertions. Preserve caller/agent transcript,
  carrier terminal status, path, and secret-free provider/runtime ids. Do not
  claim hidden target tool or result evidence.
- Require `I_ACKNOWLEDGE_PAID_PSTN` exactly and a declared call cap of at least
  four calls per selected profile-expanded case before planning or dialing.
  Keep the nightly workflow behind an independent enable variable and a
  protected environment; skipped or credentialless runs stay pending-live.

## 2026-07-27 — Telnyx dual-path carrier boundary

- Certify both required surfaces: native Call Control/TeXML with bidirectional
  RTP-in-JSON media for Pipecat, and FQDN/credential SIP provisioning for
  LiveKit. Neither path silently substitutes for the other.
- Use native Call Control `command_id` plus the durable intent, and bind an
  ambiguous create only through the signed callback's `client_state`. The
  current Voice API does not document a safe call-list-by-command-id
  reconciliation query, so voicey does not invent one.
- Treat number orders as asynchronous and return a phone-number resource only
  after the owned-number API confirms it. A pending order remains an
  indeterminate, inspect-before-retry operation.
- Follow the current official LiveKit Telnyx provider recipe exactly: Telnyx
  FQDN connection and LiveKit outbound signaling use TCP port 5060, media
  encryption is disabled on the LiveKit trunk objects, and the outbound trunk
  maps `X-Telnyx-Username` to the configured SIP username for the initial
  digest challenge. Do not claim TLS/SRTP for this interconnect unless the
  provider contract and certification evidence change together.
- Ingest recording media only from the HTTPS URL carried by a verified
  `call.recording.saved` event. Do not attach the Telnyx API key to that
  potentially provider-hosted presigned URL; disallow redirects, bound size
  and type, then copy into the configured artifact store.

## 2026-07-27 — Runtime parity evidence boundary

- Treat parity as equality of voicey's externally observable contract, not
  identical framework internals. Conversation logic remains native
  `pipecat.flows` or native LiveKit `Agent` workflows; no translation layer or
  custom flow DSL is introduced.
- Require every supported feature-matrix cell to cite checked-in executable
  evidence. A real divergence must be an explicit `declared_exclusion` with a
  reason and target phase. Pipecat warm transfer is the sole P2 exclusion and
  remains assigned to the P3 Twilio conference bridge.
- Keep a separate field-level config matrix because behavioral parity alone
  cannot prove that a canonical value reaches a native runtime mechanism.
  Both matrices pin the installed Pipecat 1.6.0 and LiveKit 1.6.7 versions and
  CI rejects drift.
- Use the current carrier-native recording commands for `phone.record`:
  Twilio live-call recording is dual-channel with signed completed/absent
  callbacks; Telnyx uses idempotent dual-channel MP3 `record_start`. Outbound
  CLI placement reads the actual project `Agent` so AMD and recording policy
  are not guessed from the manifest.
- Keep engine recording URLs credential-free. `GET` authorizes the current or
  previous Results webhook secret as a bearer credential during rotation and
  returns `Cache-Control: private, no-store`; carrier URLs never leave the
  authenticated ingestion boundary.

## 2026-07-28 — Pipecat/Twilio warm-transfer boundary

- Use two Twilio call legs and one named conference. The caller remains on the
  Pipecat Media Stream while the human hears a private briefing and presses 1;
  only then is the caller redirected into the conference. Redirecting earlier
  would terminate the agent media stream before acceptance.
- Make `warm_transfer_to_human` a native global Pipecat Flows function with
  required `briefing` and `caller_consented=true` fields. The recipe remains
  native Flows code; voicey adds no workflow DSL.
- Persist only a SHA-256 briefing digest. Raw briefing text exists transiently
  in escaped TwiML sent to Twilio and is excluded from the ledger, callback
  URLs, results, and logs.
- Fence the human-leg create before the non-idempotent API call. Fence the
  caller redirect before update. Definite rejection is terminal; unknown
  outcomes become `ambiguous` and are never retried.
- Treat the signed Gather callback as the human acceptance point. The human
  joins with `startConferenceOnEnter=false`; the caller joins with
  `startConferenceOnEnter=true`. Duplicate callbacks are idempotent and
  correlation-id drift fails closed.
- On restart, hang up only known pre-bridge orphan human legs. Never tear down
  or recreate an ambiguous/bridged conference based on inference.

## 2026-07-27 — P3 first-party recipe boundaries

- Keep each Pipecat variant as a directly loadable native `NodeConfig` and each
  LiveKit variant as native Agent-returning handoffs. The shared scenario
  source remains evaluator input only and never becomes a flow abstraction.
- Model restaurant waitlisting as a separate confirmed mutation whose result
  is explicitly not a table guarantee.
- Restrict front-desk answers to the configured knowledge tool and keep
  immediate life-safety direction ahead of hold or transfer. Ordinary warm
  transfer requires caller consent and a private briefing.
- Qualify leads only from business-need, timeline, broad budget, and
  organization-size facts. Require explicit retention/follow-up consent and
  prohibit protected traits and unrelated sensitive data.
- Apply the checked-in recipe quality checklist to first-party and community
  sources; community entries do not receive a certification claim until their
  credentialed provider suite has actually run.

## 2026-07-27 — Vobiz dual-path feasibility and safety boundary

- Supersede the conditional Vobiz-on-LiveKit item in the P0 defaults: the
  feasibility result is positive. Vobiz publishes dedicated inbound and
  outbound LiveKit SIP procedures, so the capability registry exposes both
  the Pipecat and LiveKit paths. Credentialed provisioning and PSTN evidence
  remain pending-live and are not represented as green certification.
- Follow the published interconnect literally: Vobiz sends inbound SIP to the
  LiveKit SIP host over UDP/5060; the LiveKit inbound trunk accepts only
  `13.233.44.61/32`; outbound uses the Vobiz-generated
  `*.sip.vobiz.ai` domain and an existing Vobiz credential. LiveKit media
  encryption is disabled for this route. Do not claim TLS or SRTP.
- Require the existing Vobiz credential id, username, and password. The API
  does not reveal stored passwords, so provisioning cannot safely infer
  equivalence or rotate one implicitly.
- Vobiz's current create-trunk documentation exposes two naming variants:
  `trunk_direction`/`concurrent_calls_limit` and
  `trunk_type`/`max_concurrent_calls`. Send both with equal values, read the
  resource back, and make the guarded live provision/reuse/rollback test the
  drift detector. An ambiguous write stops without speculative retry.
- Use Pipecat 1.6.0's installed `PlivoFrameSerializer` only for its compatible
  Vobiz PCMU media envelope, with `auto_hang_up=False`. Parse one start frame,
  bind it to the one-use reservation, and leave all carrier HTTP actions with
  `VobizAdapter`; Pipecat must never contact Plivo for a Vobiz session.
- Treat the signed Vobiz terminal callback as authoritative. A closed media
  socket does not end or duplicate-terminalize the call. Callback HMAC
  verification, nonce replay protection, durable route/intent fences, bounded
  recording ingestion, and reverse SIP rollback are required on every path.

## 2026-07-27 — Plivo and generic-SIP Beta boundaries

- Pin `plivo==4.61.0`. Installed introspection supersedes the stale SDK summary:
  `validate_v3_signature(method, uri, nonce, auth_token, signature, params)`
  is the six-argument helper used for POST form canonicalization. Every
  callback still requires an HTTPS expected origin and one-use nonce.
- Follow the current official LiveKit Plivo procedure literally. Inbound uses
  a Plivo URI `<livekit-sip-host>;transport=tcp`, a Plivo inbound trunk, number
  `app_id` binding, and a LiveKit inbound trunk with media encryption disabled.
  Outbound uses a Plivo credential and `secure=true` trunk plus a LiveKit
  `SIP_TRANSPORT_TLS` trunk with `SIP_MEDIA_ENCRYPT_REQUIRE`. Do not relabel
  the documented inbound leg as TLS/SRTP.
- Put a truncated SHA-256 of the write-only Plivo SIP password in the
  deterministic credential resource name. This binds adoption to secret
  equality without storing or logging the password. All other desired state
  remains in secret-safe provisioning metadata.
- Keep Plivo at Beta even though the local adapter and both runtime paths are
  complete. Account mutation, paid calls, recordings, regional behavior, and
  both-path physical endpoints remain pending until the guarded runbook
  actually passes.
- Define generic SIP as LiveKit-only and operator-managed. Voicey owns only
  the LiveKit inbound trunk, dispatch rule, outbound trunk, ledger, and reverse
  rollback; it never invents an external provider control plane.
- Require explicit `udp|tcp|tls` and `disable|allow|require` values and optional
  exact gateway CIDRs. Reject TLS with disabled media encryption. A blank CIDR
  list is allowed only as a visible operator risk, not as a security claim.

## 2026-07-28 — Cloud results-relay wire and trust boundary

- Use HMAC request authentication over the exact method, path, timestamp,
  nonce, and body digest. A credential is `vkr_<key-id>_<base64url-secret>`;
  the server accepts current plus previous during rotation, while a durable
  nonce claim prevents replay across both.
- Keep the generation fence opaque and server-signed. Validate both the token
  and the repository's current owner/generation before reserving an update so
  a stale worker cannot poison the next sequence.
- Use one gap-free per-call stream for lifecycle, result, timeline,
  transcript, tool, latency, and recording mutations. Reserve journal state
  before applying, acknowledge afterward, and make every mutation semantically
  idempotent so an acknowledgement-loss retry cannot duplicate observations or
  terminal events.
- Keep delivery, retention, artifact access, and stale-call recovery on the
  durable companion. Cloud workers receive only the repository surface needed
  to persist their own call.
- Require an authenticated, explicit protocol/storage readiness response
  before worker admission. Do not treat network reachability, an unsigned
  health route, or default-filled response fields as readiness.
- Retain SQLite only as the local protocol/crash-injection backend. The
  certified Fly companion remains Postgres plus object storage and must pass
  the same invariants before either cloud target is promoted.

## 2026-07-28 — Managed storage implementation

- Use Psycopg 3 async pools for managed Postgres. The local certification pin
  resolved to `psycopg==3.3.4` and `psycopg-pool==3.3.1`; the published
  compatibility range is `>=3.2.10,<4`.
- Use packaged append-only SQL migrations with a transaction-scoped Postgres
  advisory lock and immutable SHA-256 checksums. Apply the schema and checksum
  rows in that same transaction. Use `TIMESTAMPTZ`, `JSONB`, and `BYTEA`; use
  `FOR UPDATE SKIP LOCKED` for multi-replica delivery claims.
- Default each engine-owned pool to 1–10 connections. Deploy-target preflight
  may lower that ceiling to remain inside its managed-database connection
  budget, but a configured minimum may never exceed the maximum.
- Use boto3's S3-compatible client for AWS S3, Fly Tigris, R2, and MinIO. The
  local resolver selected `boto3==1.43.57`; the compatibility range is
  `>=1.40,<2`. Require HTTPS except for loopback emulators, namespace every key,
  attach a voicey SHA-256 digest, and run write/read/delete preflight before
  admission.
- Keep object credentials entirely in the target secret store or workload
  identity. The resource ledger records only bucket/resource identifiers and
  non-secret configuration.

## 2026-07-28 — Results-service process and callback ownership

- Keep the Fly companion free of both runtime dependencies. The
  `voicey[companion]` extra contains managed storage plus carrier callback
  verification/download dependencies; native Pipecat and LiveKit workers
  remain separate deploy artifacts.
- Use two bounded Postgres pools, one for repository work and one for the relay
  journal. Validate their combined maximum against an explicit database
  connection budget before opening either.
- Make the target preflight rollback-only for database evidence: exercise the
  actual calls/events schema, advance generation 1→2, reject the stale writer,
  and insert one terminal event inside a forced-rollback transaction. Object
  preflight writes and deletes exactly one checksummed probe.
- Keep `/healthz` unsigned but make it liveness/drain-only. The only admission
  readiness is the signed relay response covering repository, journal, object
  storage, protocol, and replica admission state.
- Install no carrier callback routes by default. An explicit
  `VOICEY_CALLBACK_PROVIDERS` list installs both signed status and recording
  endpoints for those carriers. Answer/media endpoints remain on the cloud
  worker.
- Let a verified carrier status callback update only the latest durable
  provider observation, never call ownership. On an expired worker lease,
  recovery advances the fence and maps terminal provider truth; missing or
  non-terminal truth becomes `recovery_unknown` instead of invented success.
- Move the recording ingestion implementation to the runtime-neutral results
  package, retaining the Pipecat import as a compatibility alias. The durable
  companion and local Pipecat host now share the exact download, object-write,
  ready-event, bearer-access, and rotation logic.

## 2026-07-28 — Fly companion provisioning and ownership

- Use the current Fly Managed Postgres namespace (`fly mpg`), not the legacy
  unmanaged `fly postgres` commands. Pin the generated cluster to Postgres 17
  and attach its pooled URL as `DATABASE_URL`.
- Use a private Tigris bucket through `fly storage create --app`. Require
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` attachment evidence before
  deployment; the service's startup object round trip proves those credentials
  reach the named bucket.
- Stage relay/results and carrier callback secrets through
  `fly secrets import --stage` over stdin. Keep raw generated values only in
  owner-only `.env` and Fly secrets; store only key ids and SHA-256
  fingerprints in the owner-only resource ledger.
- Require every CLI resource and cost choice explicitly. Reuse only a
  matching ledgered resource; require `--adopt` for exact unledgered resources
  and never grant adopted resources delete ownership.
- Checkpoint after each external mutation and never auto-delete on failure.
  Explicit rollback deletes only voicey-created resources in reverse order:
  Tigris bucket, MPG cluster, then app.
- Run two companion Machines with Fly service-level liveness checks, rolling
  replacement, `SIGTERM`, and a 45-second platform timeout. Promotion requires
  passing platform checks plus signed protocol/storage readiness; unsigned
  `/healthz` is not sufficient evidence.

## 2026-07-28 — Managed cloud worker deployment

- Pin the operator tooling contract to the installed
  `pipecat-cli==0.1.15` and `lk==2.16.2`. Pipecat CLI 0.1.15 requires the
  positional image and exposes no cloud-build fields, so generate a
  secret-free context and require the operator to build and push an immutable
  tag before platform deployment.
- Use glibc multi-stage images and UID/GID 10001. Keep project conversation
  logic in native Pipecat Flows or LiveKit workflows; generated entrypoints
  only adapt installed platform runner/job arguments to the shared runtime.
- Validate signed companion readiness before any platform mutation and before
  every worker begins accepting work. Put worker secrets in an owner-only
  temporary file, delete it after the CLI returns, and exclude database,
  object-store, results-signing, and previous companion credentials.
- Persist a separate owner-only, nonsecret ledger per cloud platform. Reuse
  only exact ledgered identity, require explicit adoption for existing
  resources, retain previous LiveKit version and carrier rollback facts, and
  delete only resources explicitly marked created by voicey.
- Mount stable provider-answer XML on the durable companion. Use the official
  Pipecat Cloud gateway contract for Twilio, Telnyx, Plivo, and the
  Plivo-compatible Vobiz wire. Telnyx's TeXML Application remains external
  provider state, so print the exact URL and require
  `--telnyx-texml-ready` instead of claiming an automatic update.
- Treat platform control-plane smoke as necessary but not sufficient media
  evidence. Pipecat must prove a real platform session begin and terminal
  record; LiveKit must prove named room dispatch and terminal persistence.
  Phone projects additionally require an explicit paid destination and a
  terminal durable result unless the operator visibly selects `--skip-smoke`.

## 2026-07-28 — P4 soak and drain bounds

- Use the actual shared fenced lifecycle as the credential-free soak surface,
  with deterministic simulated user/agent turns and both runtime labels. This
  measures engine ownership, storage, terminal, and resource behavior without
  presenting mocked speech providers as live audio evidence.
- Run `limits.max_concurrent` independently for Pipecat and LiveKit. The
  reference release gate uses eight slots per runtime, so the combined
  dual-runtime harness must reach peak active 16.
- After one warm-up call, require zero active/admission leaks, at most 32 MiB
  retained Python-heap growth, 64 MiB RSS high-water growth, and four additional
  file descriptors. Record peak heap separately for diagnosis; gate retained
  growth because bounded transient allocations are expected.
- Keep the short CI soak as regression evidence only. The release row requires
  `duration_s >= 86400` and is scheduled on a self-hosted Linux runner labeled
  `voicey-soak`; GitHub's documented six-hour hosted-job limit cannot satisfy
  the contract.
- During drain, preserve only reservations already exposed to callers. Reject
  every new browser reservation and unreserved SIP job with `VY-RUN-008` before
  invoking the native runtime drain.

## 2026-07-28 — Metrics and tracing boundary

- Keep the metric registry process-local and label it only by runtime, agent,
  latency kind, and stable error-catalog code. Call ids and all customer/tool
  payloads are prohibited from metric labels.
- Expose Prometheus on a dedicated listener, loopback by default. This keeps
  carrier/browser routes unchanged and gives the process one identical
  lifecycle on Pipecat, LiveKit, Docker, and the managed companion.
- Treat `voicey_calls_total` as the call-rate source; operators use PromQL
  `rate()` rather than a second periodically sampled gauge.
- Use the stable OpenTelemetry Python 1.39 OTLP/HTTP protobuf API with a local
  `TracerProvider`, `BatchSpanProcessor`, and explicit shutdown. Do not replace
  an embedding application's global provider.
- Initialize exporter configuration before admission and recreate it after a
  worker-process PID change. A bad endpoint/header/bind fails with
  `VY-OBS-006`; an unavailable collector after successful startup follows the
  SDK's asynchronous retry/drop behavior and cannot break terminal
  persistence.
- Keep OTLP attributes to opaque ids and bounded operational metadata.
  Transcript text, telephone identifiers, tool arguments/results, exception
  messages, and auth headers never enter spans.

## 2026-07-28 — Railway companion provisioning and ownership

- Pin the supported operator surface to Railway CLI `>=5.30.1,<6`; execute
  5.30.1 in the local gate. Use only its project, service, managed Postgres,
  first-party bucket, domain, variable, deployment, and scale commands. The
  product does not use Railway's optional MCP feature.
- Keep Railway aligned with the locked managed-storage matrix: it runs the
  runtime-neutral results-service companion, while native Pipecat and LiveKit
  conversation workers remain separate cloud artifacts. Managed Postgres and
  the private Railway bucket are connected through Railway variable
  references, not copied credentials.
- Require project, workspace, environment, service, service region, bucket,
  and bucket region explicitly. Adoption of an existing project requires its
  exact id plus `--adopt`; other exact resources may be adopted only after that
  project identity is proven.
- Keep generated relay/results and carrier credentials only in the ignored,
  owner-only `.env`; send values one at a time through
  `railway variable set NAME --stdin`. The resource ledger contains ids,
  ownership flags, and SHA-256 fingerprints only.
- Use Railway's pre-deploy command for the real migration lock/checksum,
  checksummed object round-trip, and rollback-only generation-1→2 fencing
  probe. Run two service replicas in one explicitly selected region with a
  30-second deployment overlap, then require release success, `/healthz`, and
  authenticated `/v1/ready`.
- Checkpoint every mutation and do not auto-delete failed work. Explicit
  rollback removes only voicey-created domain, bucket, Postgres service,
  application service, and project in reverse order. Adopted resources are
  never deleted.

## 2026-07-28 — Upgrade transaction and recipe baseline

- Use the current `uv >=0.11,<1` lockfile-only contract. Stable upgrades run
  `uv lock --upgrade-package voicey --prerelease
  if-necessary-or-explicit`; canaries use `--prerelease allow`. Global
  `disallow` cannot resolve required prerelease-tagged transitive contracts, so
  stable mode instead rejects a prerelease voicey version before sync. Both
  sync and inspect drift with that same explicit prerelease mode; uv treats a
  different or omitted mode as a lock-freshness change. `pyproject.toml`
  remains user-owned.
- Commit `voicey.recipe-lock.json` as the exact upstream source baseline
  copied by `init` or `recipes add`. This is deterministic public metadata, not
  protected state. It enables a true base/local/upstream comparison without a
  remote registry or hidden cache.
- Keep `recipes update-check` read-only. It reports per-path SHA-256 digests and
  five drift states, emits source-free JSON and explicit AI-merge guidance, and
  never changes the manifest, baseline, or recipe-owned source.
- Before mutating package state, migrate a missing baseline only when the
  manifest recipe version exactly matches the installed registry version.
  Otherwise fail closed because the original base cannot be proven.
- Byte-check `pyproject.toml` and all baseline-owned source before and after the
  upgrade. Restore the prior lock and resync on command or verification
  failure. Never restore or overwrite an unexpected source mutation
  automatically; leave it visible for version-control review.

## 2026-08-23 — Pipecat Cloud image protocol drift

- Treat the generic `pipecat.runner.run` server as local development only. A
  real Cloud deployment with that server reachable on `0.0.0.0:7860` remained
  in `Validating`; the current production image protocol is the platform base
  server's `POST /bot` and `/ws` surface on port 8080.
- Pin the derived worker to the empirically available versioned image
  `dailyco/pipecat-base:0.1.0-py3.13`. The documented versioned Python 3.14 tag
  did not exist when pulled, while mutable `latest-*` tags are not a release
  contract. Keep the platform-required `linux/arm64`, glibc, and derived-image
  UID/GID 10001 invariants.
- Reserve `/app` for the base image's server and copy the Voicey project to
  `/voicey/project`. Inherit the base command rather than reproducing its
  private server implementation.
- Accept only exact `pipecatcloud.agent.DailySessionArguments` and
  `WebSocketSessionArguments` at the boundary, converting them to the pinned
  native runner types. Reject the generic transport-free argument and unknown
  lookalikes with `VY-DEP-008`.
- Parse the current CLI's structured status lines (`Agent:`, `Ready:`,
  `Deployment Phase:`, and `Image:`) rather than the removed `Status for
  agent` sentence. `Ready: False` must never satisfy readiness. If the durable
  ledger already records deployment, reconcile the exact immutable image and
  resume at readiness/session smoke without creating another deployment.

## 2026-07-28 — Release compatibility and promotion evidence

- Keep empirically supported runtime windows narrow: Pipecat
  `>=1.6.0,<1.6.1` and LiveKit Agents `>=1.6.7,<1.6.8`. Generated project
  extras continue to pin 1.6.0 and 1.6.7 exactly. Broaden a window only after
  its declared lower and upper edges pass Python 3.11/3.14 and all first-party
  recipes.
- Treat an installed out-of-range version as uncertified, not unavailable.
  Hosts and doctor warn with the compatibility-table link and continue. A
  genuinely missing dependency remains a failed setup check.
- Derive committed public-contract snapshots from serialized `Agent`, the
  validated webhook model, and the installed Typer command tree. A snapshot
  diff requires both changelog and explanatory docs in the pull request.
- Validate canaries from a fresh environment containing the built wheel, both
  runtime extras, and no source-checkout import path. Compile every first-party
  scenario and instantiate every native Pipecat/LiveKit entrypoint.
- A stable artifact requires green canary evidence for the same release line
  and reruns the installed-wheel gate. Release workflows upload private CI
  artifacts only; final naming and any public publication remain human-only.
