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

## 2026-07-28 — Paid PSTN test boundary

- The P3 harness resolves the earlier `--live` placeholder. For Pipecat, use
  the Fixa architectural pattern—an independent native Pipecat caller through
  a Twilio Media Stream—but build it against installed
  `pipecat-ai==1.6.0`, the reference Anthropic model, voicekit's signed
  callback boundary, and its durable intent ledger. Fixa is research input,
  not a dependency or copied runtime.
- For LiveKit, use the agent-simulator architectural pattern: an isolated
  native RTC room, a native caller `AgentSession`, and a target outbound SIP
  participant. The referenced simulator's MCP surface is deliberately not
  adopted; voicekit contains no MCP product path.
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

## 2026-07-28 — Pipecat/Twilio warm-transfer boundary

- Use two Twilio call legs and one named conference. The caller remains on the
  Pipecat Media Stream while the human hears a private briefing and presses 1;
  only then is the caller redirected into the conference. Redirecting earlier
  would terminate the agent media stream before acceptance.
- Make `warm_transfer_to_human` a native global Pipecat Flows function with
  required `briefing` and `caller_consented=true` fields. The recipe remains
  native Flows code; voicekit adds no workflow DSL.
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
- Define generic SIP as LiveKit-only and operator-managed. Voicekit owns only
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
  attach a voicekit SHA-256 digest, and run write/read/delete preflight before
  admission.
- Keep object credentials entirely in the target secret store or workload
  identity. The resource ledger records only bucket/resource identifiers and
  non-secret configuration.

## 2026-07-28 — Results-service process and callback ownership

- Keep the Fly companion free of both runtime dependencies. The
  `voicekit[companion]` extra contains managed storage plus carrier callback
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
  `VOICEKIT_CALLBACK_PROVIDERS` list installs both signed status and recording
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
  Explicit rollback deletes only voicekit-created resources in reverse order:
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
  delete only resources explicitly marked created by voicekit.
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
  `voicekit-soak`; GitHub's documented six-hour hosted-job limit cannot satisfy
  the contract.
- During drain, preserve only reservations already exposed to callers. Reject
  every new browser reservation and unreserved SIP job with `VK-RUN-008` before
  invoking the native runtime drain.

## 2026-07-28 — Metrics and tracing boundary

- Keep the metric registry process-local and label it only by runtime, agent,
  latency kind, and stable error-catalog code. Call ids and all customer/tool
  payloads are prohibited from metric labels.
- Expose Prometheus on a dedicated listener, loopback by default. This keeps
  carrier/browser routes unchanged and gives the process one identical
  lifecycle on Pipecat, LiveKit, Docker, and the managed companion.
- Treat `voicekit_calls_total` as the call-rate source; operators use PromQL
  `rate()` rather than a second periodically sampled gauge.
- Use the stable OpenTelemetry Python 1.39 OTLP/HTTP protobuf API with a local
  `TracerProvider`, `BatchSpanProcessor`, and explicit shutdown. Do not replace
  an embedding application's global provider.
- Initialize exporter configuration before admission and recreate it after a
  worker-process PID change. A bad endpoint/header/bind fails with
  `VK-OBS-006`; an unavailable collector after successful startup follows the
  SDK's asynchronous retry/drop behavior and cannot break terminal
  persistence.
- Keep OTLP attributes to opaque ids and bounded operational metadata.
  Transcript text, telephone identifiers, tool arguments/results, exception
  messages, and auth headers never enter spans.
