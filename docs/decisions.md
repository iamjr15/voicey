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
