# Build progress

Last updated: 2026-07-27

## Current checkpoint

- **Phase:** P2 — LiveKit parity, unified testing, Telnyx
- **Current unit:** P2.3 appointment-booking LiveKit recipe variant
- **Next task:** author the native LiveKit appointment workflow and its runtime-specific tests while preserving the existing recipe contract
- **Completed:** all P0 and P1 units plus P2.1–P2.2. The production LiveKit runtime, browser client/token path, three-process local supervisor, native scratch quickstart, current provider/fallback mapping, fenced incremental lifecycle, actual-SIGKILL recovery, and Twilio Elastic SIP provisioning/controls/recording reconciliation are green. Credentialed/PSTN/microphone P1 and P2 gates remain pending with exact guarded commands in `docs/GAPS.md`

## Gate status

| Gate | Status | Evidence / next command |
|---|---|---|
| P0 pins and Flows location | green | `pipecat-ai==1.6.0`; core `pipecat.flows`; `livekit-agents==1.6.7` installed on Python 3.14 |
| P0 spec sync | green | Standard Webhooks, recording, storage/relay, and Python matrix contracts aligned in spec + plan |
| P0 security baseline | green | Four-version CI, edge integrations, pre-commit, secret scan, dependency audit, Apache-2.0, SECURITY.md, protected-file tests |
| P0 Pipecat walking skeleton | green | Native FlowManager/PipelineWorker, connected SmallWebRTC, tool/results, mocked termination, verified signed delivery |
| P0 LiveKit walking skeleton | green | Native AgentServer/AgentSession/function_tool, dispatch token, tool/results, mocked termination, verified signed delivery |
| P0 exact verification | green | `uv run pytest -m integration --no-cov tests/integration/test_p0_walking_skeleton.py` → 2 passed |
| P1.1 configuration unit | green | `uv run pytest` → 54 passed; ruff and strict pyright green; total branch coverage 90.57% |
| P1.2 observability unit | green | Correlation/PII leak, latency, WAL/FULL durability, reopen, schema, and parallel-write tests green; `uv run pytest` → 73 passed |
| P1.3 results/reliability unit | green | Terminal/outbox rollback, fencing, dual sweeper, actual SIGKILL, retry/DLQ, recording, retention, pull parity, and official Python/Node/Go Standard Webhooks interop green; `uv run pytest` → 103 passed |
| P1.4 tools unit | green | Typed schemas, async/thread execution, 8s timeout, safe errors, GET-only retry, env auth, protected observations, and 40-call context isolation green; `uv run pytest` → 117 passed, 91.84% branch coverage |
| P1.5 Twilio implementation/local certification | green | Protocol/registry, FULL ledger, CAS rollback, ambiguity fencing, signatures, calls/controls/recording/AMD, and audio rig green; full suite → 161 passed, 92.09% branch coverage |
| P1.5 Twilio credential/PSTN certification | pending-live | Four guarded commands in `docs/GAPS.md`; current environment has no Twilio variables, so 4 live tests skip rather than claim evidence |
| P1.6 Pipecat runtime | green | Current 1.6.0 APIs, actual long-lived runner completion, native flows/tools, provider failover, 13-row config matrix, Twilio WebSocket at 8 kHz, SmallWebRTC signaling, incremental observations, admission and fenced terminal failure paths; `uv run pytest` → 225 passed, 4 honest live skips, 90.78% branch coverage; ruff + strict pyright green |
| P1.7 tunnel implementation/local verification | green | Resolution order, current ngrok SDK, exact cloudflared exec, strict URL parsing, redaction, full pipe drain, terminate→kill, challenge route, and real local WS round trip green; `uv run pytest` → 243 passed, 5 honest live skips, 90.19% branch coverage; ruff + strict pyright green |
| P1.7 cloudflared public WS edge | pending-live | Harness ran three times; generated domains failed DNS for 60s; exact rerun in `docs/GAPS.md`; no orphan processes |
| P1.8 CLI implementation/local verification | green | Capability-gated zero-default wizard, secret-free resume, live key validation, atomic 0600 `.env`, native-flow scratch scaffold, supervised dev/phone rollback, operational command tree, full doctor, JSON reads, confirmations, next-step engine, error-doc anchors, and static error-catalog coverage; `uv run pytest` → 309 passed, 5 honest live skips, 90.13% branch coverage |
| P1.8 full human wizard | pending-live | Disposable interactive runbook in `docs/GAPS.md`; requires a human and provider credentials |
| P1.8 doctor broken-machine usability | pending-live | Safe disposable broken-project harness and exact commands in `docs/GAPS.md`; requires human observation |
| P1.9 playground implementation/local verification | green | Two-listener public/admin isolation, pre-token durable reservation, scoped one-use session tokens, failed-offer terminalization, Origin/Host/proxy validation, abuse limits, exact durable payload reads, safe two-tier reload, FastAPI `app.frontend()`, wheel build, React/RTVI UI, axe scan, desktop/mobile browser QA, and npm/pip audits green; `uv run pytest` → 334 passed, 5 honest live skips, 90.32% branch coverage |
| P1.9 real microphone/provider browser call | pending-live | Exact disposable-project command in `docs/GAPS.md`; requires reference-provider credentials, a human microphone grant, and speech |
| P1.10 appointment recipe/local Evals contract | green | Native wheel-packaged recipe; `init`/no-overwrite copy; production-session `EvalTransport`; 7 text + 3 audio scenarios parse on Pipecat 1.6.0; installed CLI 0/1 contract runs in CI; `uv run pytest` → 343 passed, 5 honest live skips, 90.05% branch coverage; wheel and dependency audits green |
| P1.10 credentialed text/audio Evals | pending-live | Exact disposable-project commands in `docs/GAPS.md`; current environment lacks Anthropic credentials and local Ollama |
| P1.11 Docker implementation/local verification | green | Deterministic artifacts, Compose validation, env-only secrets, local WAL/FULL/artifact preflight, invalid-topology rejection, fenced rolling handover, production drain, health/smoke contracts, and CI image lifecycle/scan; final local image ran read-only/non-root with build-asserted NLTK data, health green, SIGTERM drain exit 0/OOM false, and Trivy fixed HIGH/CRITICAL + secret scans at 0; full suite → 377 passed, 5 honest live skips, 90.04% branch coverage |
| P1.11 public HTTPS + paid Docker smoke | pending-live | Exact build, ingress, number-point, paid-call, verification, and rollback commands in `docs/GAPS.md`; requires public ingress and live funded Twilio resources |
| P1.12 local phase verification | green | Final wheel quickstart 34.086s/300s; exact 22-path CLI matrix; parallel isolation; rollback/fencing/dual-sweeper/actual-SIGKILL chaos; admin negative; 34 Twilio certification and 31 Docker cases; aggregate report `local_automated_status=green`; full suite 381 passed, 5 honest live skips, 90.04% branch coverage |
| P1.12 reference audio latency | pending-live | Dedicated 20-turn native audio harness requires 20 distinct persisted `e2e` turns and p50 ≤800ms + p95 ≤1500ms; untracked backup lacks Anthropic; exact command in `docs/GAPS.md` |
| P1 phase overall | pending-live | All local automation green; reference providers, Twilio/PSTN, public edge/Docker, and human wizard/doctor/microphone/handset evidence remain unpromoted; see `docs/verification/p1-gates.md` |
| P2.1 LiveKit runtime/local certification | green | Native `AgentServer`/`rtc_session`, current 1.6.7 session APIs, provider fallbacks, complete config policy, least-privilege token, incremental persistence, heartbeat/fencing, actual SIGKILL recovery, native DTMF/cold+warm transfer, TLS/auth-correct Twilio SIP provision/reuse/rollback/ambiguity, outbound SIP mapping, and CA-SID recording reconciliation; full suite 447 passed, 8 honest live skips, 90.06% branch coverage; ruff/format/strict pyright green |
| P2.1 Twilio–LiveKit account/PSTN certification | pending-live | Three guarded account/PSTN commands plus the physical inbound/outbound, DTMF, hangup, recording, transfer, terminal-delivery, and rollback checklist are ready in `docs/GAPS.md`; no required account variables exist locally |
| P2.2 LiveKit playground + quickstart | green | Pinned native client, Authorization-only one-use voicekit token exchange, durable pre-token reservation, least-privilege room credential, remote audio/mic/transcription/state mapping, failure terminalization, three-process `dev`, in-flow read-only credential validation, exact managed SIP resource inspection, native scratch scaffold import, desktop/mobile browser QA; frontend 11 tests/build/audit green; full Python suite 466 passed, 8 honest live skips, 90.01% branch coverage |
| P2.2 real LiveKit microphone/provider browser call | pending-live | Exact disposable-project command in `docs/GAPS.md`; requires a LiveKit project, reference-provider credentials, browser microphone permission, and human speech |
| Physical handset/manual gates | pending-live | Outbound and inbound appointment commands are ready; exact runbook in `docs/GAPS.md` |

No credential-, paid-account-, cloud-, handset-, or wall-clock gate is marked green unless it actually ran.
