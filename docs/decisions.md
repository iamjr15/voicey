# Decisions

Decisions are append-only. A superseding decision links the earlier entry and explains the migration impact.

## 2026-07-26 — P0 defaults accepted

- **Product name:** keep `voicekit` in package, CLI, entry-point groups, docs, and examples through the build. Do not publish or register public resources. Prepare `RENAME.md` for the human-selected final name.
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

- `voicekit dev --port N` binds the public runtime/signaling listener to
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
  `voicekit[pipecat]`; the P1 harness uses the installed `pipecat eval`
  commands and eval transport rather than a voicekit-owned simulator.
- Accept Rich `>=13.9.4,<16` instead of requiring Rich 15. Pipecat 1.6.0's
  `cli` dependency (included by `evals`) pins Rich below 14, and voicekit uses
  only APIs present in both supported ranges. The CLI's behavior and output
  contracts are unchanged and remain regression-tested at the resolver-selected
  Rich 13.9.4.

## 2026-07-27 — Canonical Docker packaging and process boundary

- Build the production runtime from the released voicekit wheel plus the
  runtime/carrier extras selected in `voicekit.jsonc`. An unpublished `.dev0`
  checkout must pass a locally built wheel explicitly; the generator copies it
  mode 0600 and the final image removes all build inputs.
- Install non-voicekit `[project].dependencies` separately and run project
  agent/native-flow modules from `/app`. Do not package an arbitrary flat
  project tree as part of the engine distribution.
- Use a Docker-managed local-driver volume, SQLite WAL/FULL, local artifacts,
  and one steady replica. Same-host generations may overlap only for controlled
  handover and share fenced leases; network volumes and cross-host SQLite are
  rejected at startup.
- Let the voicekit container supervisor own SIGINT/SIGTERM and both Uvicorn
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
  Voicekit therefore does not claim a callback that Twilio cannot configure.
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

- Keep the one-use voicekit session token on the authenticated HTTP exchange:
  the browser sends it only as `Authorization: Bearer …` to the public token
  endpoint after the admin listener has durably reserved the call.
- Return a distinct, short-lived, least-privilege LiveKit room credential only
  after that exchange succeeds. Hand it directly to pinned
  `livekit-client==2.21.0`; do not wrap or replace LiveKit's native signaling
  protocol.
- The official client currently carries its scoped room credential during the
  WebSocket join using provider-native query/protocol fields. This is permitted
  and documented explicitly. The voicekit token and LiveKit API key/secret
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
  cited judging by default. A secret-free `tests/voicekit-test.jsonc` may select
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

## 2026-07-27 — Telnyx dual-path carrier boundary

- Certify both required surfaces: native Call Control/TeXML with bidirectional
  RTP-in-JSON media for Pipecat, and FQDN/credential SIP provisioning for
  LiveKit. Neither path silently substitutes for the other.
- Use native Call Control `command_id` plus the durable intent, and bind an
  ambiguous create only through the signed callback's `client_state`. The
  current Voice API does not document a safe call-list-by-command-id
  reconciliation query, so voicekit does not invent one.
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

- Treat parity as equality of voicekit's externally observable contract, not
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
