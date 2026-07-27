# Voicekit — Product Specification (production-grade)

> **Status:** Design-complete, pre-build. Greenfield — nothing carried over from parley.
> **Name:** "voicekit" is a placeholder throughout; final name TBD.
> **Scope mandate:** This is NOT an MVP. Every surface below ships complete, hardened, tested, and documented, or it does not ship. Where breadth must be staged, integrations carry an honest certification tier (Certified / Beta) — never a half-working default path.

---

## 0. Product statement

**The production toolchain around Pipecat and LiveKit.** A developer declares a voice agent with a thin typed config plus native framework flow code; voicekit supplies everything around the conversation that every team otherwise rebuilds badly: guided project creation, a browser playground, simulated-caller testing, telephony across carriers, deployment, and a signed results contract — identical on both runtimes.

One line: **choose your engine per project, keep your workflow forever.**

### Non-goals (hard boundaries)

- **No custom flow DSL.** Conversation logic is 100% native `pipecat-flows` or native LiveKit agent workflows. Voicekit never sits between the user and the framework's conversation APIs.
- **No MCP** anywhere in the product. Tools are plain Python functions or HTTP endpoints.
- **No hosted cloud, no no-code builder** in this repo/phase. (Future commercial layer; keep engine boundaries clean so it can exist later without relicensing.)
- **No provider marketplace, no in-house STT/LLM/TTS.**
- **No multi-tenancy** in the engine. The self-hoster is the tenant.

### The five axes of choice

| Axis | Options at launch |
|---|---|
| Runtime | Pipecat, LiveKit — chosen per project at `init`, full parity in toolchain |
| STT | Anything the chosen runtime supports, by id (`deepgram/nova-3`, …) |
| LLM | Same (`anthropic/claude-sonnet-5`, `openai/…`, `google/…`) |
| TTS | Same (`cartesia/sonic-3.5`, `elevenlabs/…`, …) |
| Telephony | Twilio, Telnyx, Vobiz (Certified); Plivo, generic SIP (Beta at launch) |

---

## 1. System architecture

```
user project (owned, git)                 engine (pip: voicekit, semver)
├── agent.py      typed config      ┌── shared core ──────────────────────────┐
├── flow.py       NATIVE flows      │ config schema + validation              │
├── prompts/*.md                    │ provider catalog + key validation       │
├── tools.py      plain functions   │ telephony adapter layer                 │
├── tests/*.py    sim scenarios     │ results contract (sign/deliver/DLQ)     │
└── voicekit.jsonc  manifest        │ playground dev server                   │
                                    │ sim-test runner                         │
                                    │ CLI (init/dev/test/deploy/doctor/…)     │
                                    │ observability (structured logs/metrics) │
                                    └─────────────────────────────────────────┘
                                    ┌── runtime bootstraps (thin) ────────────┐
                                    │ pipecat: Pipeline + transports + RTVI   │
                                    │ livekit: AgentSession + worker + SIP    │
                                    └─────────────────────────────────────────┘
```

Rules that keep this honest:

1. **Bootstraps stay thin.** A bootstrap may assemble, register, and observe; it may never reinterpret conversation semantics. If a feature requires wrapping a runtime's conversation API, the feature is redesigned or dropped.
2. **Everything in shared core is runtime-blind.** Shared core communicates with bootstraps only through defined interfaces (§5 telephony targets, §6 results events, §8 client session tokens).
3. **The manifest (`voicekit.jsonc`)** records init choices (runtime, recipe + version, channels, carriers + selected E.164 phone number, deploy target) so every command is resumable and `recipes update-check` / `upgrade` can reason about drift.

Packaging: single distribution `voicekit` with extras — `voicekit[pipecat]`, `voicekit[livekit]` (each pinning a tested version range of its runtime), `voicekit[twilio,telnyx,vobiz,plivo]`. `init` installs exactly what the wizard's answers require.

---

## 2. Agent configuration — `agent.py`

Pydantic models; typed code is canonical, serializes to JSON (the wire/storage form; what tests diff and a future UI edits). Full schema:

```python
from voicekit import Agent, Models, Voice, Phone, Web, Results, Limits, Behavior

agent = Agent(
    name="clinic-front-desk",                       # [a-z0-9-], unique per deploy
    runtime="livekit",                              # "pipecat" | "livekit" (set by init)

    models=Models(
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
        # optional failover, applied by bootstrap where runtime supports it:
        fallbacks={"tts": "elevenlabs/flash-2.5"},
    ),

    voice=Voice(
        id=None,                                    # provider voice id; None → curated default
        language="en",                              # BCP-47; drives STT/TTS config
        fallback_language=None,
        speed=1.0,
    ),

    persona="Warm, brisk, professional. Never robotic. Company: Sunrise Dental.",

    flow="flow:entry",                              # module:attr of native flow entrypoint

    tools="tools",                                  # module to auto-collect @tool functions
                                                    # (or explicit list of callables)

    phone=Phone(                                    # None → web-only agent
        provider="twilio",                          # twilio|telnyx|vobiz|plivo|sip
        number="+14155550123",
        inbound=True,
        outbound=True,
        record=True,                                # consent handling is flow content (recipes include it)
    ),

    web=Web(enabled=True, allowed_origins=["https://sunrisedental.com"]),

    results=Results(
        webhook="https://api.sunrisedental.com/voice-results",
        secret_env="VOICEKIT_WEBHOOK_SECRET",       # never inline secrets
        previous_secret_env=None,                   # overlap window during rotation
        include=["transcript", "data", "recording", "metrics"],
        redact=["phone_number"],                    # field-level redaction before delivery
        purge_after_days=30,                        # records, artifacts, outbox, and backups
    ),

    limits=Limits(
        max_duration_s=600,
        max_concurrent=20,                          # admission control per instance
        silence_hangup_s=30,
        daily_spend_alert_usd=None,
    ),

    behavior=Behavior(
        allow_interruptions=True,
        voicemail="hangup",                         # "hangup" | "leave_message" (uses prompts/voicemail.md)
        dtmf=True,
        transfer_number=None,                       # enables warm-transfer tool when set
        end_call_phrases=["goodbye", "bye now"],
    ),
)
```

**Validation (at import, at `dev`, at `deploy`):** model ids resolve against the provider catalog for the chosen runtime; language is servable by chosen STT+TTS (else error names which axis fails and lists alternatives); phone number is E.164 and owned by the configured carrier account (live check); webhook URL is HTTPS; secret env var exists. Every validation error message includes the fix.

**Versioning:** config carries an implicit `config_hash` (canonical-JSON SHA-256) stamped into call records and webhook payloads — deterministic "what was live when this call happened."

---

## 3. Conversation layer — native per runtime

- **Pipecat projects:** `flow.py` is `pipecat-flows` code — `FlowManager`, `NodeConfig`s, transition/record functions. Simple agents may use a single system prompt (recipe variant "prompt-only").
- **LiveKit projects:** `flow.py` is native LiveKit Agents code — `Agent` subclasses per stage, `@function_tool` methods, handoffs, session userdata for state, prebuilt tasks where they fit (e.g. warm transfer).
- Prompt text lives in `prompts/*.md`, loaded by flow code via `voicekit.prompts.load()` (a file-reader with variable interpolation — a utility, not an abstraction).
- The ONLY engine touchpoint inside flow code is the results recorder: `from voicekit import results; results.set("slot", value)` (and `results.set_outcome("booked")`). One import, two functions; everything else in `flow.py` is pure framework code any Pipecat/LiveKit tutorial applies to.

**Engine ↔ flow contract per runtime (bootstrap responsibilities):** register tools natively; install transcript + latency observers; enforce `limits`/`behavior` via native mechanisms (Pipecat: pipeline params, idle/duration processors; LiveKit: session options, tasks); flush `results` buffer into the call record at termination.

---

## 4. Tools contract

A tool is a plain typed Python function:

```python
from voicekit import tool

@tool(
    say_while_running="Let me check that for you…",      # optional filler line
    mutating=False,                                      # set True for writes
)
def check_slots(date: str, party_size: int = 1) -> list[str]:
    """Return open appointment slots for a date."""     # docstring → tool description
    return clinic_api.free_slots(date, party_size)
```

- Signature + docstring → JSON schema; bootstrap registers natively (Pipecat flow function / LiveKit function tool). Sync and async supported; sync runs in a worker thread.
- Tools that create, update, delete, transfer, purchase, or otherwise commit an external side effect declare `mutating=True`. Runtime adapters let read tools be interrupted but make a started mutating tool uninterruptible through the native framework mechanism; retry/idempotency remains the tool owner's responsibility.
- **HTTP tools** for remote APIs: `tool.http(name=…, url=…, method=…, headers_env=…, timeout_s=8)` — engine handles auth header injection from env, timeout, retry (idempotent GET only), and error mapping.
- **Failure semantics (fixed, documented):** timeout/exception → the LLM receives a structured tool-error result (never a stack trace, never a hang); default timeout 8s; the `say_while_running` line covers slow calls. Tool calls + results + latency are recorded per call.

---

## 5. Telephony layer

### 5.1 Adapter interface (shared core)

```python
class TelephonyAdapter(Protocol):
    provider: str
    capabilities: Caps          # {inbound, outbound, amd, dtmf, transfer, recording, regions[]}

    # number lifecycle
    def list_numbers(self) -> list[NumberInfo]: ...
    def buy_number(self, country: str, area: str | None) -> NumberInfo: ...          # always CLI-confirmed
    def release_number(self, number: str) -> None: ...

    # routing (the dev↔prod URL-swap surface)
    def point_inbound(self, number: str, target: RuntimeTarget) -> RollbackToken: ...
    def restore(self, token: RollbackToken) -> None: ...

    # outbound
    def start_call(self, from_no: str, to_no: str, target: RuntimeTarget) -> ProviderCallId: ...

    # webhook plumbing (pipecat path)
    def verify_request(self, request) -> bool: ...        # carrier signature validation — MANDATORY
    def answer_response(self, target) -> XML: ...          # TwiML/TeXML/Plivo-XML/Vobiz-XML
    def parse_event(self, request) -> CallEvent: ...       # answered|completed|failed|recording_ready
```

`RuntimeTarget` is per-runtime: `PipecatTarget(https_base, ws_path)` — adapter points the number's voice webhook at it and templates the stream URL into answer XML; `LiveKitTarget(project, sip_uri)` — adapter provisions the carrier-side SIP trunk/origination toward LiveKit and ensures LiveKit-side inbound trunk + dispatch rule exist (idempotent).

For Twilio Elastic SIP, the idempotent LiveKit target is asymmetric by carrier
contract: the number-scoped LiveKit inbound trunk has no username/password
because Twilio cannot authenticate calls it originates; the LiveKit outbound
trunk carries the Twilio termination credentials. Secure trunking uses TLS for
both the Twilio origination URI and the LiveKit outbound transport. Rollback
must restore the complete pre-trunk number route.

### 5.2 Carrier matrix at launch

| Carrier | Tier | Pipecat path | LiveKit path | Notes |
|---|---|---|---|---|
| Twilio | **Certified** | Media Streams WS + TwiML | Elastic SIP trunk | Reference implementation |
| Telnyx | **Certified** | TeXML + streaming | SIP trunk | |
| Vobiz | **Certified** | Answer XML + WS (India) | SIP if supported, else Pipecat-path only (capability-flagged) | India wedge |
| Plivo | Beta | Plivo XML + streams | SIP trunk | |
| Generic SIP | Beta | — (use LiveKit path) | Direct trunk | Escape hatch for PBX/other carriers |

**Certification checklist (required for "Certified"; CI-enforced):** inbound + outbound live tests pass nightly; signature verification implemented and negative-tested; DTMF in/out; hangup semantics (both directions, all terminal reasons mapped); recording completion ingestion (signed callback when the carrier surface supports one, otherwise a documented bounded reconciliation keyed by an authenticated carrier call ID); transfer where capability-flagged; rollback-on-Ctrl-C proven; region/latency notes documented; runbook page for common carrier errors (mapped into the CLI error catalog §7.6).

### 5.3 Audio/media correctness (Pipecat path)

Per carrier: codec/sample-rate handling (e.g. mulaw 8k ↔ 16k linear resample), frame pacing, jitter tolerance, mark/clear (interruption flush) semantics — each with automated audio-loopback tests (tone in, transcript/energy out) so barge-in cutting and audio artifacts are regression-tested, not vibes-tested.

---

## 6. Results & webhook contract

The engine's promise: **every call has exactly one terminal event persisted once, then delivery is attempted until acknowledged or visibly dead-lettered — never silence.** A delivery outage cannot create a second terminal event.

### 6.1 Events

`call.started` (optional, off by default), `call.completed` (terminal success), `call.failed` (terminal infra-level failure before/without conversation), and `call.recording.ready` (non-terminal artifact update). The terminal event is immutable after it is persisted. When recording is enabled, `call.completed` is emitted immediately with a stable engine-owned recording reference in `pending` state; carrier processing later produces `call.recording.ready` for the same reference. Same envelope:

```json
{
  "event": "call.completed",
  "id": "evt_…",                       // unique per delivery-content; idempotency key for receivers
  "call": {
    "id": "call_…", "direction": "inbound",
    "from": "+1415…", "to": "+1415…",
    "started_at": "…", "ended_at": "…", "duration_s": 143,
    "ended_reason": "caller_hangup"    // enum, fixed taxonomy, documented
  },
  "agent": { "name": "clinic-front-desk", "runtime": "livekit", "config_hash": "sha256:…" },
  "outcome": "booked",                 // results.set_outcome(); null if unset
  "data": { "name": "…", "slot": "2026-07-30T14:00" },
  "transcript": [ { "role": "user", "text": "…", "t_ms": 4210 }, … ],
  "recording": {                       // null unless phone.record
    "id": "rec_…",                     // stable engine-owned reference
    "status": "pending",               // pending | ready | failed
    "url": null                        // engine access URL when ready; never a raw carrier URL
  },
  "metrics": { "turns": 12, "interruptions": 2, "latency_ms": { "p50": 780, "p95": 1310 } }
}
```

### 6.2 Delivery

- **Signing:** Standard Webhooks format verbatim. Requests carry `webhook-id`, `webhook-timestamp`, and `webhook-signature`. Each configured secret is serialized as `whsec_<base64-key>`; implementations remove `whsec_`, base64-decode the remainder, then compute `base64(HMAC-SHA256(key, "{id}.{timestamp}.{raw_body}"))`. The signature header contains space-separated `v1,<base64-signature>` values for the current and previous secrets during rotation. Receivers verify the raw body and reject timestamps outside a 5-minute window. `webhook-id` is the stable event id used for consumer deduplication. A `voicekit.verify_webhook()` helper, official-library interoperability vectors, and copy-paste snippets (Python/Node/Go) ship in docs.
- **Retries:** the single canonical policy is the Standard Webhooks/Svix curve: attempts at 0s, 5s, 5m, 30m, 2h, 5h, 10h, and 10h, with ±20% jitter per delay relative to the preceding failure. After 8 failed attempts the delivery is visibly dead-lettered. Every retry and manual redelivery reuses the event id and immutable raw body, but uses a fresh timestamp/signature. `voicekit calls list --undelivered`, `voicekit calls redeliver <id>`, and replay-since-timestamp operate the queue. DLQ depth is exported as a metric and surfaced by `doctor`.
- **Pull parity:** everything push-delivered is also pull-readable — `voicekit calls show <id>` (and the local API the playground uses) returns the identical payload. Push-only was a parley design bug; not repeated.
- **Crash invariant:** a durable call row is created before any externally visible action. One fenced transaction CAS-transitions the call from active to terminal and inserts the immutable terminal envelope plus delivery rows. A partial unique index permits only one terminal event per call. Active owners hold generation/fencing tokens; stale owners cannot complete after takeover. Recovery reconciles provider state before terminalizing a stale call.

---

## 7. CLI specification

### 7.1 Principles (product requirements, not style)

1. Rail, not toolbox: every command ends with the printed next step; bare `voicekit` = status + suggested next action.
2. Plain language, and **no option is ever pre-selected or badged "recommended" — every choice is the user's.** Guidance means each option carries a neutral, factual one-line description (what it is, cost class, language coverage), never a default we chose for them.
3. Validate at entry: every key live-tested at paste time; nothing completes setup broken.
4. Every completed wizard yields a working, talking agent — including the scratch path; wizard ≤ 5 questions, each an explicit selection; advanced options are flags only.
5. Confirm money & live changes (`--yes` for automation); everything interrupted is resumable (state in manifest); Ctrl-C restores what was changed.
6. Every interactive question has a flag twin — the same CLI is deterministic for CI and coding agents. `--json` on all read commands.

### 7.2 Command tree

```
voicekit                     status + next step
voicekit init                wizard (--recipe --runtime --models --phone-provider --yes --resume)
voicekit dev                 run + browser playground (--phone --tunnel cloudflared|ngrok|url --port --no-open)
voicekit call <e164>         outbound test call through the dev/deployed agent
voicekit test                sim scenarios (--filter --audio --live --report junit|json)
voicekit deploy [target]     docker|pipecat-cloud|livekit-cloud|fly|railway (--yes --skip-smoke)
voicekit numbers             list | buy | release | point | restore
voicekit keys                list | validate (re-runs live checks)
voicekit calls               list | show <id> | redeliver <id> (--undelivered)
voicekit recipes             list | add <name> | update-check
voicekit doctor              full preflight (--fix applies safe fixes)
voicekit upgrade             engine upgrade + recipe drift report (AI-merge guidance, never auto-overwrite)
```

### 7.3 Init wizard (fixed question set — every answer an explicit user selection)

**Q1 — "What should your agent do?"** Recipe list from the registry, each with a one-line description, plus `Start from scratch` last.
**The scratch path (fully defined):** a follow-up free-text question — *"Describe in a sentence or two what your agent should do."* The description is templated into `persona` and `prompts/system.md`, and the scaffold is a minimal but **working** single-prompt agent: greeting, system prompt seeded from the description, one example tool, one example sim test — all TODO-marked. `voicekit dev` produces a talking agent immediately even from scratch. After key setup, one explicit opt-in question (default No): *"Draft fuller starting prompts from your description using your configured LLM key? [y/N]"* — never runs unless selected.

**Q2 — "Where will people talk to it?"** **Multi-select** checkboxes: `[ ] Phone` `[ ] Website / app (browser)`. At least one required, both selectable. Phone → carrier selection + `phone` block scaffolded; Website → `web` block + embed snippet in the scaffold.

**Q3 — "Which engine?"** Neutral list, nothing pre-selected:
- `Pipecat — open-source Python pipeline framework (by Daily); phone audio via carrier media streams.`
- `LiveKit Agents — open-source agent framework on LiveKit's WebRTC/SIP infrastructure.`
Fixed footer line: *"Every voicekit command works identically with either."* User must pick.

**Q4 — Models, one axis at a time.** Three explicit selections (STT → LLM → TTS) from the provider catalog for the chosen runtime; each option shows a factual one-liner (price class, language coverage, latency class). No pre-selected set. Flag twin: `--models stt=…,llm=…,tts=…`.

Then guided keys for exactly the providers chosen (signup URL per provider, `o` opens browser, paste → live ✓/✗ with fix line), then scaffold + `Next: voicekit dev`.

### 7.4 Doctor checks (complete list)

Keys valid + provider balances/credit where APIs expose it · runtime package versions within tested range · Python version · audio deps (ffmpeg) · port availability · tunnel reachability (self-request through tunnel) · carrier webhook agreement (fetch number's live config, diff vs expected) · LiveKit project reachable + SIP trunk/dispatch existence · webhook receiver reachable + signature round-trip (against a `voicekit`-hosted echo or user endpoint with `--send-test`) · DLQ depth · clock skew (breaks HMAC) · `.env` vs `.env.example` diff · disk space (recordings/DLQ). Each ✗ prints one fix line; `--fix` applies the safe subset.

### 7.5 Guidance system

Wizard/state machine reads and writes `voicekit.jsonc`; "next step" is computed from manifest + environment (keys present? phone configured? tests passing? deployed?), so guidance stays correct mid-journey, not just at init.

### 7.6 Error catalog

Every raised error carries a stable code (`VK-TEL-021`), a plain-language cause, and a copy-paste fix; carrier/provider error codes are mapped into the catalog (e.g. Twilio 21205 → "webhook not public — is `voicekit dev --phone` running?"). Catalog is a docs page; CLI links each error to its anchor. Unmapped errors print the raw cause plus a pre-filled GitHub issue link — an unmapped error is a bug by policy.

---

## 8. Browser playground (`voicekit dev`)

- Serves a local web app; **Pipecat projects** connect via RTVI client SDKs; **LiveKit projects** via livekit-client — internal detail, identical UX.
- Uses two loopback listeners: `--port` is the public runtime/signaling listener and `--port + 1` is the admin playground/read-API listener. A tunnel may forward only the public listener; the admin listener is never a tunnel target. Non-local admin deployment requires an integrator authentication hook.
- Browser session creation requires a short-lived, one-use **voicekit** bearer token bound to the agent, audience, session, and resulting peer/call. The voicekit token is sent in the `Authorization` header, never in a URL; origin/host policy, trusted-proxy reconstruction, issuance limits, and signaling rate limits are enforced server-side. On LiveKit, successful authenticated exchange returns a separate short-lived, least-privilege room credential to the pinned official client. The client may carry that provider credential using LiveKit's native signaling protocol; it is not a voicekit session token or a provider API key.
- Surface: mic button + live conversation; streaming transcript with per-turn latency badges; event feed (tool calls with args/results/duration, state transitions, interruptions); captured `data` panel updating live; the exact webhook payload preview at call end ("what your server will receive"); latency breakdown per subsystem (STT/LLM/TTS/e2e) per turn.
- Hot reload: prompts and config apply next session; flow code restarts the runtime worker between calls with a visible "reloaded" marker.
- Also serves the local read API used by `calls show` — playground and CLI read the same store.

---

## 9. Simulated-caller testing (`voicekit test`)

Scenario file (owned code, `tests/`):

```python
from voicekit.testing import scenario

@scenario
def changes_mind():
    return dict(
        caller="Busy parent, slightly distracted. Books for Tuesday 7pm, then switches to 8pm.",
        goals=["end with a confirmed booking"],
        expect=dict(outcome="booked", data={"slot": lambda s: s.endswith("20:00")}),
        judge=["agent confirmed the FINAL time, not the first one"],   # LLM-judged, cited from transcript
        max_turns=24,
    )
```

- **Tiers:** `test` (default) = text-mode — sim-caller LLM ↔ agent LLM+flow with tools live or mocked; fast, cheap, CI-default. `--audio` = full pipeline — caller turns synthesized via TTS, pushed as audio through the real STT→LLM→TTS path; catches pronunciation, endpointing, barge-in issues. `--live` = real PSTN loopback — engine places an actual call to the agent's number with the sim caller on the line; nightly CI, pre-deploy smoke.
- **Assertions:** hard checks on `outcome`/`data`/turn-count/latency budgets + LLM-judge criteria (must cite transcript lines; judge model configurable).
- Deterministic seeds where providers allow; flake policy: a scenario failing <100% reruns 3× and reports stability %, never silently passes.
- Output: terminal table, `--report junit` for CI. **Recipes ship with their sim suites passing in both runtimes — enforced by release CI (§17).**

---

## 10. Recipe registry

- **A recipe =** `recipe.jsonc` (metadata, version, min-engine) + per-runtime `pipecat/flow.py` + `livekit/flow.py` + shared `prompts/`, `tools.py` stubs (TODO-marked integration points), `tests/` sim suite, and a README (what it does, what to customize, integration points).
- **Distribution:** shadcn-model — `voicekit recipes add <name>` copies source into the project (runtime-matching variant); recorded with version in the manifest; `recipes update-check` diffs against upstream and offers guidance (including an AI-merge prompt); **never auto-overwrites**.
- **Launch set (each: both runtimes, sim-tested, production conversation design — voicemail, barge-in, "actually, change that", human-transfer, graceful failure):**
  1. `appointment-booking` (book/reschedule/cancel; calendar-API stub)
  2. `restaurant-reservations` (party size/time/special requests; waitlist fallback)
  3. `front-desk` (answer, triage, take message, warm transfer)
  4. `lead-intake` (qualify, capture, schedule follow-up)
- Quality bar is a written checklist (edge-case coverage, latency budget, test breadth, prompt-quality review) applied to first-party and community recipes alike; community recipes live in a `community/` namespace with the same CI, minus the certification stamp until reviewed.
- Registry is a static JSON index + git repo — no service dependency; `recipes list` works offline from cache.

---

## 11. Deployment (`voicekit deploy`)

Deploy = generate artifacts → drive the platform's own CLI/API → sync secrets → cut telephony over → smoke-verify. Targets:

| Target | Artifact | Secrets | Telephony cutover | Smoke |
|---|---|---|---|---|
| `docker` (canonical) | Dockerfile + compose + healthcheck; printed run instructions | documented env | `numbers point` printed as explicit step | manual or `--smoke <url>` |
| `pipecat-cloud` | their deploy config; cloud build | synced to their secret store | automatic (adapter → hosted URL) | automatic |
| `livekit-cloud` | worker deploy via their tooling | their env config | none needed (trunk already → LiveKit) | automatic |
| `fly` / `railway` | their config + CLI driven | their secrets CLI | automatic (Pipecat-shaped) | automatic |

- **Smoke verification** (default on): one real test call post-cutover; reports answer latency, greeting match, webhook delivery; failure offers instant rollback (`numbers restore`). On the canonical Docker target, `--smoke <url> --to <E.164>` first proves the public ready/storage/admission health contract and then places the explicitly confirmed paid phone call; a web-only target requires a real browser conversation and cannot substitute an endpoint probe for media evidence.
- Zero-downtime redeploys documented per target (drain: stop accepting new calls, let active calls finish — engine exposes a drain signal handler).
- **Storage repository:** lifecycle, call-record, and outbox persistence use one repository contract and one logical schema. Docker/self-host uses SQLite WAL on one local persistent volume; same-host multi-process handover is supported, while network-volume or cross-replica SQLite is rejected. Fly and Railway use platform-managed Postgres. Backend contract tests, startup schema validation, migration locks, and expand/contract migrations ensure overlapping generations share the schema safely.
- **Cloud-worker relay:** ephemeral Pipecat Cloud and LiveKit Cloud workers use an authenticated results relay backed by the same repository contract. The durable relay must acknowledge `begin_call` before a worker accepts a call, issue a fencing token, accept an ordered idempotent update stream, acknowledge terminal persistence, recover stale calls server-side, rotate credentials, and prevent replay. Workers fail closed when the relay is unreachable. The certified companion is voicekit in results-service mode on user-owned Fly compute, managed Postgres, and object storage; `--relay-url` supports an equivalently validated user-owned relay.
- **Artifacts:** recordings use an artifact-store contract. Docker/self-host uses a protected local filesystem; Fly/Railway and cloud-relay deployments use durable object storage. The durable side owns authenticated carrier download, access control, `call.recording.ready`, and retention deletion.

---

## 12. Observability

- **Structured JSON logs** (call_id-correlated) with a human-pretty dev renderer; levels documented; no PII at info level.
- **Per-call record** (repository-backed SQLite or Postgres): config_hash, timeline, transcript, tool calls, latency series, terminal reason, webhook delivery status.
- **Latency instrumentation** per subsystem per turn (STT partial/final, LLM TTFT, TTS TTFB, mouth-to-ear e2e) — powers playground badges, test budgets, and the smoke report.
- **Prometheus endpoint** (opt-in): active calls, call rate, error rate by code, DLQ depth, latency histograms.
- Optional OTLP export (spans per call/turn/tool) — off by default, one config line to enable.

---

## 13. Security

- **Inbound:** carrier signature verification mandatory per adapter (negative-tested in certification); playground/local API bound to localhost by default; web client session exchange uses short-lived voicekit tokens and exposes no provider API keys (LiveKit's official client receives only a scoped room credential for its native signaling protocol); `web.allowed_origins` enforced.
- **Outbound:** results webhook HMAC (two-secret rotation, §6.2); HTTPS-only enforcement.
- **Secrets:** env-only (`_env`-suffixed config fields); webhook secrets use `whsec_` serialization and support current+previous env names; secrets are never serialized into config JSON, logs, call records, or images; deploy syncs to target secret stores; `doctor` flags secrets found in files.
- **PII:** `results.redact` field-level redaction pre-delivery; recording on/off per agent; transcript retention window config (`purge_after_days`); purge spans database rows, SQLite WAL, outbox/dead-letters, recordings, and backups on both repository backends and artifact stores; a documented data-map (what is stored where) for users' own compliance work.
- Dependency and container scanning in CI; a SECURITY.md with a disclosure process from day one.

---

## 14. Reliability & production hardening

- **Admission control:** `limits.max_concurrent` enforced at answer time with a correct busy behavior per carrier (reject → carrier-native busy handling; documented per adapter).
- **Graceful shutdown/drain:** SIGTERM → stop accepting, finish active calls (bounded by `max_duration_s`), flush DLQ, then exit; required for zero-downtime deploys.
- **Crash safety:** call record + results buffer flushed incrementally through the assigned repository or results relay; a crash mid-call yields one fenced `call.failed` event with partial transcript, not silence.
- **Provider outage behavior:** model `fallbacks` where configured; otherwise fast-fail with distinct terminal reasons (`stt_unavailable`, …) so operators can alert on cause.
- **Idempotency:** outbound `call` placement accepts an idempotency key; webhook events carry stable ids for receiver-side dedup.
- Chaos tests in CI: kill provider connections mid-call, drop carrier WS, timeout tools — assert terminal-webhook invariant (§6) holds in every case.

---

## 15. Versioning & release engineering

- **SemVer** on the `voicekit` package; public API = `agent.py` schema, `tool`/`results`/`testing` APIs, webhook payload, CLI commands/flags, adapter Protocol.
- **Runtime compatibility:** each release pins tested ranges of `pipecat-ai`/`livekit-agents`; a compatibility table in docs; CI runs the matrix against range edges; out-of-range installs warn loudly (not fail) with the table link.
- **Deprecation policy:** nothing public removed with less than 2 minor versions of runtime warnings + changelog + migration note.
- **Webhook payload versioning:** additive-only within a major; envelope carries no version field until v2 is ever needed (then explicit).
- Release cadence target: minor every 4–6 weeks; canary channel (`pip install voicekit --pre`) exercised by first-party recipes before stable.

---

## 16. Documentation (launch-blocking)

Quickstart per runtime (the 5-minute path, verbatim-tested in CI) · Concepts (architecture, ownership boundaries — what's yours vs the engine's) · Guides: each carrier (setup, certification notes, gotchas), each deploy target, webhook receiving (verify snippets in 3 languages), testing, upgrading · Recipe pages (one per recipe: demo audio, customization map) · Generated API reference (config schema, tool/results/testing APIs, webhook schema, error catalog) · Troubleshooting index keyed by error codes. Docs live in-repo; docs PRs required for public-surface changes (CI-enforced via API-schema snapshot diff).

---

## 17. Quality gates — the definition of "production-ready"

Ship-blocking acceptance criteria, measured in CI or scripted checks:

| Surface | Gate |
|---|---|
| First-run DX | Fresh machine → `init` → first browser conversation in **≤ 5 min**; scripted e2e proves it for both runtimes |
| Latency | Reference config (Deepgram/Claude/Cartesia): voice-to-voice **p50 ≤ 800ms, p95 ≤ 1500ms** in `--audio` tests; regressions fail CI |
| Recipes | All 4 × both runtimes: full sim suite green (text + audio tiers); `--live` green nightly |
| Telephony | Certified carriers: full checklist (§5.2) green nightly incl. real PSTN loopback; rollback proven |
| Webhook invariant | Chaos suite: every call terminates in exactly one terminal webhook or visible dead-letter — zero silent losses across all injected failures |
| CLI | Every command: interactive + flag-twin + `--json` paths tested; error catalog covers 100% of raised codes; doctor detects every setup break we can inject |
| Deploy | Each target: scripted deploy from scratch → smoke call green; drain/redeploy without dropped calls |
| Storage | Repository contract + chaos suite green on SQLite and Postgres; invalid target/backend combinations rejected; every target passes persistence preflight and a rolling-generation invariant test |
| Cloud relay | Both ephemeral cloud runtimes require acknowledged `begin_call`, fenced ordered updates, terminal acknowledgement, replay protection, and server-side stale-call recovery under failure injection |
| Docs | Quickstarts executed verbatim by CI; zero broken links; API reference generated from source of truth |
| Reliability | 24h soak at `max_concurrent` with sim callers: zero leaked calls/FDs/memory growth beyond bounds |
| Security | Signature negative-tests; secret-leak scan of logs/records/images; dependency audit clean |

**CI matrix:** {pipecat, livekit} × {py3.11, 3.12, 3.13, 3.14} × {unit, integration (carrier/provider mocks), sim-text} on every PR; integration runs on the 3.11/3.14 range edges on PRs and the full Python matrix nightly. Nightly also adds sim-audio, live PSTN loopback per certified carrier, deploy-target e2e, repository-backend equivalence, soak (weekly), and runtime-range edges.

---

## 18. Repo layout

```
voicekit/
├── src/voicekit/
│   ├── config/        # schema, validation, manifest
│   ├── telephony/     # adapter protocol + twilio/ telnyx/ vobiz/ plivo/ sip/
│   ├── results/       # contract, signing, delivery, DLQ
│   ├── runtimes/      # pipecat/ livekit/ bootstraps (thin)
│   ├── playground/    # dev server + web app (built assets vendored)
│   ├── testing/       # scenario API, sim caller, judges, tiers
│   ├── cli/           # commands, wizard, doctor, error catalog
│   └── obs/           # logging, metrics, latency instrumentation
├── recipes/           # first-party recipes (+ community/ namespace)
├── docs/
├── tests/             # engine tests incl. chaos + certification suites
└── examples/
```

License: **Apache-2.0** (patent grant; matches LiveKit precedent). Any future cloud-only code lives outside this repo from day one.

---

## 19. Build plan (every phase exits at production quality for what it contains)

Phases gate on the §17 criteria for their contents — later phases add surfaces, never finish earlier ones.

- **P1 — Engine spine + Pipecat bootstrap + Twilio (certified) + playground + CLI core** (`init/dev/doctor` with full guidance system, error catalog from day one) + `appointment-booking` recipe (Pipecat) + results contract + docker deploy. *Exit: quickstart gate, latency gate, webhook invariant, Twilio certification.*
- **P2 — LiveKit bootstrap to full parity** (SIP provisioning, playground client, same recipe on LiveKit) + `test` text+audio tiers + Telnyx certified. *Exit: parity checklist — every P1 gate green on LiveKit.*
- **P3 — Remaining recipes ×2 runtimes + Vobiz certified + Plivo/SIP beta + `--live` testing + pipecat-cloud & livekit-cloud deploy targets.*
- **P4 — Hardening to full §17:** chaos suite, soak, drain, metrics/OTLP, fly/railway, `upgrade`/drift tooling, docs completion, security review. *Exit: every gate green → 1.0.*

Estimate honestly: this is roughly 4–6 months for a small senior team (2–3 engineers) or proportionally longer solo — the gates, not the calendar, define done.

---

## 20. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Runtime API churn (both are fast-moving) | Pinned tested ranges + CI at range edges + thin bootstraps (small blast radius) |
| Daily/LiveKit ship overlapping toolchain features | Zero-wrapper design means we ride their improvements; moat = cross-runtime parity + recipes + guided DX, which framework vendors are structurally unlikely to do for each other |
| 2× surface (runtimes) slows everything | Parity checklist is CI-enforced; a feature isn't "done" until green on both — prevents silent drift toward one runtime |
| Recipe quality is content work, easy to underfund | Recipes have the same CI gates as code; launch count fixed at 4, superb, not 10 mediocre |
| Live-test flake (real providers/PSTN in CI) | Live tier is nightly not per-PR; stability % reporting; quarantine process with SLA to fix |
| Solo/maintainer sustainability (the Vocode failure mode) | Small public API, aggressive automation of quality gates, community recipe namespace to distribute content burden |

## 21. Open decisions (need Jigyansu)

1. **Name** (and therefore package/CLI/registry names) — everything above uses `voicekit` as placeholder.
2. Hosting org for repo + docs domain.
3. Judge-model default for sim tests (quality vs cost).
4. Whether Vobiz LiveKit-path is feasible (SIP support?) or ships Pipecat-path-only with a capability flag.
5. P1 reference model set (Deepgram/Claude/Cartesia assumed above).
```
