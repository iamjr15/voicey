# External verification gaps

This file tracks gates that are fully implemented but cannot be truthfully marked green without credentials, paid resources, physical hardware, cloud access, or wall-clock time.

| Gate | Status | Exact runbook command | Requirement |
|---|---|---|---|
| P1 Twilio carrier/API certification | ready-to-run, pending credentials/PSTN | Commands below | Twilio test/live credentials, funded number, public target, PSTN |
| P1 cloudflared public WebSocket edge | ready-to-run, pending edge DNS/network | Command below | `cloudflared`, outbound Cloudflare access, generated quick-tunnel DNS |
| P1 physical-handset outbound check | ready-to-run, pending credentials/human | Paid PSTN command below | Physical handset, answering person, provisioned carrier numbers |
| P1 inbound audio/transcript loopback | ready-to-run, pending credentials/human | Commands below | Running appointment recipe, physical handset, and provisioned number |
| P1 full guided-wizard usability | ready-to-run, pending human/credentials | Command below | Interactive terminal, a human, provider keys |
| P1 doctor on deliberately broken project | ready-to-run, pending human observation | Commands below | Interactive terminal; disposable fixture only |
| P1 playground real microphone/provider call | ready-to-run, pending human/credentials | Command below | Valid reference-provider keys, browser microphone grant, a person speaking |
| P1 appointment text Evals | ready-to-run, pending credentials/local judge | Commands below | Deepgram, Anthropic, Cartesia keys and local Ollama `gemma2:9b` |
| P1 appointment audio Evals | ready-to-run, pending credentials/model downloads | Commands below | Reference-provider keys plus one-time Kokoro/Moonshine downloads |
| P1 reference audio latency | ready-to-run, pending credentials | Command below | Deepgram, Anthropic, and Cartesia keys; p50 ≤ 800 ms and p95 ≤ 1500 ms |
| P1 Docker public deployment + paid smoke | ready-to-run, pending public ingress/credentials/PSTN | Commands below | Public HTTPS ingress, live Twilio credentials, owned number, paid destination |
| P2 Twilio–LiveKit automated provisioning | ready-to-run, pending credentials/account mutation | Commands below | LiveKit project, owned Twilio number, Elastic SIP domain, explicit mutation acknowledgement |
| P2 Twilio–LiveKit PSTN certification | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded LiveKit/Twilio accounts, deployed agent, two physical endpoints |
| P2 LiveKit playground real microphone/provider call | ready-to-run, pending credentials/human | Command below | LiveKit project, reference-provider keys, browser microphone grant, a person speaking |
| P2 appointment LiveKit conversation | ready-to-run, pending credentials/human | Command and checklist below | LiveKit project, reference-provider keys, browser microphone grant, a person exercising all handoffs |
| P2 unified appointment text suite, both runtimes | ready-to-run, pending credentials/local judge | Commands below | Deepgram, Anthropic, Cartesia, local Ollama; both generated projects |
| P2 unified appointment audio suite, both runtimes | ready-to-run, pending credentials/model downloads | Commands below | Reference-provider keys, Ollama, Kokoro/Moonshine downloads; both generated projects |
| P2 Telnyx certification, both paths | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded Telnyx/LiveKit accounts, both carrier paths, public target, PSTN |
| P3 Vobiz certification, both paths | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded Vobiz/LiveKit accounts, Vobiz SIP credential, public Pipecat target, PSTN |
| P3 Plivo Beta certification, both paths | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded Plivo/LiveKit accounts, public Pipecat target, Zentrunk credentials, PSTN |
| P3 generic SIP Beta loopback | ready-to-run, pending external route/PSTN/human | Commands and checklist below | LiveKit project, operator-managed PBX/carrier trunk, physical endpoints |
| P3 tier-3 PSTN loopback | ready-to-run, pending credentials/PSTN | Commands below | Funded Twilio or LiveKit SIP path, deployed target agent, reference/judge keys, ngrok for Pipecat, and paid PSTN |
| P3 cloud deploys | not-ready | Added with each P3 deploy target | Pipecat Cloud, LiveKit Cloud, and Fly access |
| P4 Railway deploy | not-ready | Added with the P4 Railway target | Railway access |
| P4 24-hour soak | not-ready | Added with the P4 soak harness | 24 hours of uninterrupted runner time |

Statuses change to `ready-to-run, pending …` only after the harness,
configuration, and exact command exist. A row moves to the completion report
as green only after the command actually passes.

## P3 tier-3 paid PSTN loopback

The local gate validates both bounded fixtures, native caller construction,
8 kHz Twilio transport, signed callback admission, LiveKit room/SIP request
shape, transcript/evidence reporting, cleanup, preflight ordering, and the
nightly workflow guard. It does not prove a real call.

For the Pipecat caller path, deploy the target agent behind the destination
number, export funded Twilio credentials, a distinct owned caller number,
ngrok, reference-provider, and judge credentials, then run the one-case
fixture:

```bash
export VOICEKIT_LIVE_PSTN_ACK='I_ACKNOWLEDGE_PAID_PSTN'
export VOICEKIT_LIVE_PSTN_MAX_CALLS=4
export VOICEKIT_LIVE_TARGET_NUMBER='+14155550123'
export VOICEKIT_LIVE_TWILIO_FROM='+14155550124'
export TWILIO_ACCOUNT_SID='AC…'
export TWILIO_AUTH_TOKEN='…'
export NGROK_AUTHTOKEN='…'
export DEEPGRAM_API_KEY='…'
export ANTHROPIC_API_KEY='…'
export CARTESIA_API_KEY='…'
export OPENAI_API_KEY='…'
(cd tests/fixtures/live-pstn-pipecat && \
  ../../../.venv/bin/voicekit test --live --report junit)
```

For the LiveKit path, configure an outbound SIP trunk that can reach the
target destination and run:

```bash
export VOICEKIT_LIVE_PSTN_ACK='I_ACKNOWLEDGE_PAID_PSTN'
export VOICEKIT_LIVE_PSTN_MAX_CALLS=4
export VOICEKIT_LIVE_TARGET_NUMBER='+14155550123'
export VOICEKIT_LIVEKIT_OUTBOUND_TRUNK_ID='ST_…'
export LIVEKIT_URL='wss://project.livekit.cloud'
export LIVEKIT_API_KEY='…'
export LIVEKIT_API_SECRET='…'
export DEEPGRAM_API_KEY='…'
export ANTHROPIC_API_KEY='…'
export CARTESIA_API_KEY='…'
export OPENAI_API_KEY='…'
(cd tests/fixtures/live-pstn-livekit && \
  ../../../.venv/bin/voicekit test --live --report junit)
```

Each result must be a first-attempt pass, carrier status `completed`, contain
both caller and target-agent transcript lines, and write
`.voicekit/test-results.xml` with `evidence.provider`,
`evidence.provider_call_id`, `evidence.runtime_call_id`,
`evidence.path`, and `evidence.terminal_status`. Inspect the carrier account to
confirm no more than four calls were placed by either job.

Nightly automation is `.github/workflows/live-pstn.yml`. Configure the
protected `paid-pstn` environment with the named secrets, set repository
variable `VOICEKIT_LIVE_PSTN_ENABLED=true`, and set repository variable
`VOICEKIT_LIVE_PSTN_ACK=I_ACKNOWLEDGE_PAID_PSTN`. The workflow uses
non-cancelling concurrency so an overlapping schedule cannot abandon a paid
call. The process environment has none of the required carrier, LiveKit,
Anthropic, OpenAI judge, target, or caller variables. The ignored predecessor
backup contains only Deepgram and Cartesia among the needed values. The
workspace therefore has not executed either paid call, and neither path is
represented as green.

## P2 unified scenario suite on both runtimes

Normal CI validates the public scenario API, deterministic profiles, cited
judge contract, four-attempt stability reporting, JSON/JUnit output, installed
Pipecat scenario/manifest parsing, LiveKit native assertion plans, and the
LiveKit PCM bridge. It compiles all seven appointment cases for both runtimes.
Those local checks do not prove the reference-provider conversations.

Export `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, and `CARTESIA_API_KEY`, then
install and start the documented local judge:

```bash
ollama pull gemma2:9b
ollama serve
```

In another terminal from the repository root, create both disposable projects:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_TEST_PARENT="$(mktemp -d)"
VOICEKIT_TEST_PIPECAT="$VOICEKIT_TEST_PARENT/appointment-pipecat"
VOICEKIT_TEST_LIVEKIT="$VOICEKIT_TEST_PARENT/appointment-livekit"
uv run voicekit init "$VOICEKIT_TEST_PIPECAT" \
  --name appointment-pipecat \
  --recipe appointment-booking \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
uv run voicekit init "$VOICEKIT_TEST_LIVEKIT" \
  --name appointment-livekit \
  --recipe appointment-booking \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

Run text, audio, and JUnit output on each native runtime:

```bash
(cd "$VOICEKIT_TEST_PIPECAT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test --audio && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test --report junit)
(cd "$VOICEKIT_TEST_LIVEKIT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test --audio && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test --report junit)
```

Each command must return zero, each runtime must report all seven cases at
100% stability, and both JUnit files must contain zero failures. The first
audio run may download Kokoro and Moonshine. As of 2026-07-27 this environment
has none of the three reference-provider variables and has no `ollama`
executable, so no provider-backed result is represented as green.

## P2 LiveKit playground microphone/provider gate

The local suite proves one-use voicekit token exchange, durable reservation,
room-token scope, replay rejection, exchange-failure terminalization, native
client state/audio/transcript wiring, the three-process supervisor, and native
scratch import. Desktop and 390×844 browser automation completed the exchange
against the credentialless fixture. The SDK then reported its real provider
connection failure; no media success was inferred.

With valid reference-provider and LiveKit project credentials exported, create
and run a disposable project:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_LIVEKIT_PARENT="$(mktemp -d)"
VOICEKIT_LIVEKIT_PROJECT="$VOICEKIT_LIVEKIT_PARENT/livekit-browser"
uv run voicekit init "$VOICEKIT_LIVEKIT_PROJECT" \
  --name livekit-browser \
  --recipe scratch \
  --description "Greet the caller and answer concise product questions." \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
(cd "$VOICEKIT_LIVEKIT_PROJECT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" doctor && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" dev --port 7860)
```

Required process variables are `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`,
`CARTESIA_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and
`LIVEKIT_API_SECRET`. Open `http://127.0.0.1:7861`, grant microphone access,
speak at least two turns, end the session, and confirm remote audio, streaming
and durable transcript, latency/event panels, and exactly one terminal event.
In browser network inspection, confirm the voicekit bearer appears only in the
`Authorization` header of `/api/livekit/token`; the separate scoped provider
room credential may appear only inside the official LiveKit client's native
signaling exchange. This gate remains pending until the credentialed media call
and human speech genuinely pass.

## P2 appointment LiveKit conversation gate

Normal CI loads the copied recipe through the production native-flow loader,
invokes each Agent-return handoff, verifies shared-tool preservation, and
exercises the installed `GetNameTask`/`GetEmailTask` boundary with deterministic
results. It cannot prove the real speech-driven task conversations without a
LiveKit project, reference-provider credentials, microphone access, and a human.

With those values exported, create and run the actual recipe:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_RECIPE_PARENT="$(mktemp -d)"
VOICEKIT_RECIPE_PROJECT="$VOICEKIT_RECIPE_PARENT/livekit-appointments"
uv run voicekit init "$VOICEKIT_RECIPE_PROJECT" \
  --name livekit-appointments \
  --recipe appointment-booking \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
(cd "$VOICEKIT_RECIPE_PROJECT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" doctor && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" dev --port 7860)
```

Open `http://127.0.0.1:7861`, grant microphone access, and verify:

1. Booking asks for and confirms name and email before calendar search, accepts
   an “actually” correction, confirms all final fields, then reports success
   only after `book_appointment`.
2. Rescheduling and cancellation each confirm email, require an `APT-`
   reference, call `find_appointment`, and request explicit mutation
   confirmation.
3. A changed intent returns to intake without losing shared calendar tools.
4. A calendar failure states that no change occurred and offers one safe retry
   followed by human escalation when `VOICEKIT_TRANSFER_NUMBER` is configured.
5. Ending the session produces exactly one terminal event with the expected
   appointment outcome and native tool observations.

The environment still lacks LiveKit and Anthropic credentials, so this command
has not been represented as green.

## P2 Twilio–LiveKit SIP certification

The local suite covers current SDK request shapes, the required asymmetric
authentication boundary, TLS signaling, encrypted-media policy, idempotent
provisioning, reverse rollback, ambiguous-outcome fencing, durable outbound
intent mapping, native DTMF and transfer tools, recording policy, close-reason
mapping, incremental observations, and actual `SIGKILL` recovery:

```bash
uv run pytest --no-cov \
  tests/unit/test_livekit_runtime.py \
  tests/integration/test_livekit_sigkill.py \
  tests/certification/test_twilio_livekit_sip.py
```

For the live no-call provisioning gate, export the LiveKit project credentials,
Twilio credentials, an owned number, the LiveKit SIP URI, a unique Twilio SIP
domain, the configured LiveKit agent name, and randomly generated SIP
credentials. The test provisions twice, verifies reuse, then rolls back:

```bash
VOICEKIT_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
  uv run pytest -m live --no-cov \
  tests/live/test_twilio_livekit_live.py::test_live_twilio_livekit_provision_reuse_and_rollback
```

Required variables: `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`VOICEKIT_TWILIO_LIVE_FROM`, `VOICEKIT_LIVEKIT_AGENT_NAME`,
`VOICEKIT_LIVEKIT_SIP_URI`, `VOICEKIT_TWILIO_SIP_DOMAIN`,
`VOICEKIT_TWILIO_SIP_USERNAME`, and `VOICEKIT_TWILIO_SIP_PASSWORD`.

For the paid outbound gate, retain the provisioned outbound trunk, set its id
as `LIVEKIT_SIP_OUTBOUND_TRUNK`, identify the certification room and
destination, then explicitly acknowledge charges:

```bash
VOICEKIT_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
  uv run pytest -m live --no-cov \
  tests/live/test_twilio_livekit_live.py::test_live_twilio_livekit_paid_outbound_and_sip_status_mapping
```

After either live call completes, copy LiveKit's documented
`sip.twilio.callSid` participant attribute into
`VOICEKIT_TWILIO_LIVE_CALL_SID` and verify that Core Recordings exposes exactly
one completed `Trunking` recording:

```bash
uv run pytest -m live --no-cov \
  tests/live/test_twilio_livekit_live.py::test_live_twilio_livekit_completed_trunk_recording_correlation
```

The full physical certification uses a deployed appointment agent with
`behavior.dtmf=true`, `phone.record=true`, and a configured transfer
destination. Run one inbound call and one outbound call. During the calls:

1. verify greeting and two-way audio;
2. send and receive `12#` and confirm the durable
   `runtime.dtmf_received` timeline;
3. hang up once from the caller and once from the agent, then confirm
   `caller_hangup` and `agent_hangup`;
4. complete one cold transfer and one native warm transfer;
5. verify Twilio created a dual-channel recording and voicekit emitted the
   stable recording reference plus `call.recording.ready`;
6. verify exactly one terminal event and one acknowledged or visibly
   dead-lettered delivery for every call;
7. roll back the provisioning token and confirm the original number route.

Inspect the durable evidence after each call:

```bash
voicekit calls list
voicekit calls show <call-id>
```

This gate is not green until both guarded pytest commands and the physical
checklist genuinely pass. This workspace has neither the account variables nor
the required PSTN endpoints, so the tests skip and the handset checklist
remains pending.

## P2 Telnyx dual-path certification

The offline certification validates signed Call Control and TeXML callbacks,
one-use WebSocket admission, native PCMU/8 kHz bidirectional streaming, DTMF,
hangup mapping, recording ingestion, asynchronous number orders, durable
intent reconciliation, conflict-safe route restoration, current LiveKit/Telnyx
request shapes, idempotent reuse, and reverse rollback:

```bash
uv run pytest --no-cov \
  tests/certification/test_telnyx_adapter.py \
  tests/certification/test_telnyx_media.py \
  tests/certification/test_telnyx_livekit_sip.py
```

The safe read-only account/owned-number check needs `TELNYX_API_KEY`,
`TELNYX_PUBLIC_KEY`, `TELNYX_CONNECTION_ID`, and
`VOICEKIT_TELNYX_LIVE_FROM`:

```bash
uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_account_and_owned_number_are_ready
```

With the Pipecat agent running at `VOICEKIT_LIVE_PUBLIC_BASE`, this guarded
command temporarily points the number to the configured Voice API connection,
checks the FULL-durability route record, and restores the exact snapshot:

```bash
VOICEKIT_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_route_point_and_crash_safe_restore
```

The paid Call Control gate additionally needs
`VOICEKIT_TELNYX_LIVE_TO`, `VOICEKIT_TELNYX_TRANSFER_TO`, a person answering
the endpoint, and destination permissions. It starts dual-channel recording,
sends `12#`, cold-transfers, and guarantees a final hangup:

```bash
VOICEKIT_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_paid_pstn_dtmf_recording_and_cold_transfer
```

After the verified runtime captures `call.recording.saved`, export its `mp3`
or `wav` URL as `VOICEKIT_TELNYX_LIVE_RECORDING_URL` and prove that engine-owned
artifact ingestion succeeds:

```bash
uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_signed_recording_url_ingests_to_engine_storage
```

For the no-call LiveKit path, also export `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `VOICEKIT_LIVEKIT_AGENT_NAME`,
`VOICEKIT_LIVEKIT_SIP_URI`, `VOICEKIT_TELNYX_SIP_USERNAME`, and
`VOICEKIT_TELNYX_SIP_PASSWORD`. The test provisions both control planes
twice, proves reuse, then rolls back:

```bash
VOICEKIT_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_livekit_live.py::test_live_telnyx_livekit_provision_reuse_and_rollback
```

Retain a provisioned outbound trunk as `LIVEKIT_SIP_OUTBOUND_TRUNK`, set
`VOICEKIT_LIVEKIT_CERT_ROOM`, and run the paid LiveKit SIP call:

```bash
VOICEKIT_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_livekit_live.py::test_live_telnyx_livekit_paid_outbound_and_sip_status_mapping
```

The physical checklist requires one inbound and one outbound call on each
path:

1. verify greeting and two-way audio;
2. send and receive `12#`, then inspect the durable DTMF observation;
3. hang up once from each side and confirm caller/agent/provider terminal
   mapping;
4. complete a cold transfer on both paths and a native warm transfer on
   LiveKit;
5. confirm the stable pending recording reference and
   `call.recording.ready`;
6. confirm exactly one terminal event and an acknowledged or visibly
   dead-lettered delivery;
7. interrupt one temporary route/provisioning run and prove the saved snapshot
   restores without overwriting concurrent carrier changes.

This workspace has no Telnyx or LiveKit variables. All six live tests therefore
skip, and no account, route, paid-call, recording, or handset result is marked
green.

## P1 cloudflared public WebSocket edge

The harness starts a loopback FastAPI listener, installs an ephemeral
challenge-only WebSocket endpoint, creates a cloudflared quick tunnel, verifies
the exact challenge through the public `wss://` route, and tears down both
processes:

```bash
VOICEKIT_LIVE_TUNNEL_CONFIRM=I_ACKNOWLEDGE_PUBLIC_TUNNEL \
  uv run pytest -m live --no-cov \
  tests/live/test_tunnel_live.py::test_cloudflared_quick_tunnel_websocket_round_trip
```

On 2026-07-27, cloudflared 2026.3.0 connected and emitted a
`trycloudflare.com` URL on three attempts, but the generated hostname remained
unresolvable for the full 60-second probe deadline (`gaierror`). The harness
failed and cleaned up every child process, so this edge gate remains pending.

## P1 Twilio credential and PSTN gates

- **Twilio no-charge test-credential Calls API:** ready-to-run, pending
  `TWILIO_TEST_ACCOUNT_SID` and `TWILIO_TEST_AUTH_TOKEN`.

  ```bash
  uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_test_credentials_accept_outbound_contract_without_charge
  ```

- **Twilio live account/owned-number readiness:** ready-to-run, pending live
  credentials and `VOICEKIT_TWILIO_LIVE_FROM`.

  ```bash
  uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_live_account_and_owned_number_are_ready
  ```

- **Twilio route mutation + crash-safe restore:** ready-to-run, pending a
  reachable `VOICEKIT_LIVE_PUBLIC_BASE`, owned number, live credentials, and
  explicit `VOICEKIT_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION`.

  ```bash
  VOICEKIT_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
    uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_live_route_point_and_crash_safe_restore
  ```

- **Twilio paid PSTN/physical-handset, outbound DTMF, dual-channel recording,
  and cold transfer:** ready-to-run, pending the running public Pipecat target,
  live from/to/transfer numbers, credentials, a person answering the handset,
  and explicit `VOICEKIT_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES`.

  ```bash
  VOICEKIT_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
    uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_live_paid_pstn_dtmf_recording_and_cold_transfer
  ```

The inbound Pipecat runtime path and appointment recipe now exist. Run a
credentialed recipe project through `voicekit dev --phone`, call its owned
number from a physical handset, book and then change an appointment, and inspect
the durable transcript:

```bash
VOICEKIT_TRANSFER_NUMBER=+14155550199 voicekit dev --phone
voicekit calls list
voicekit calls show <call-id>
```

This remains pending until a person completes the inbound call; the local Evals
below do not count as physical-handset evidence.
Mocked carrier protocol and local codec/tone-loopback evidence is green
independently; it is not represented as live carrier evidence.

## P1 CLI manual gates

The full wizard must be assessed by a person because absence of coercive
wording, initial selection, and confusing transitions is a usability claim.
Create a disposable target and complete the flow without answer flags:

```bash
VOICEKIT_MANUAL_PROJECT="$(mktemp -d)/human-wizard"
uv run voicekit init "$VOICEKIT_MANUAL_PROJECT"
```

Verify every choice starts unselected, scratch is last, channel multi-select
requires an explicit selection, each pasted key is validated, and the final
`Next:` command starts the generated project. Interrupt once before completion,
then run:

```bash
uv run voicekit init "$VOICEKIT_MANUAL_PROJECT" --resume
```

The broken-machine doctor gate uses a disposable project rather than damaging
the host. From the repository root:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_BROKEN_PROJECT="$(mktemp -d)"
uv run python tests/manual/prepare_broken_doctor.py "$VOICEKIT_BROKEN_PROJECT"
(cd "$VOICEKIT_BROKEN_PROJECT" && "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" doctor)
```

The run is expected to exit non-zero and visibly diagnose missing provider and
carrier credentials, missing webhook secret, `.env.example` drift, unreachable
signed receiver, and route/account checks it cannot safely perform. Then run
the safe subset and verify that secrets are not printed:

```bash
(cd "$VOICEKIT_BROKEN_PROJECT" && "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" doctor --fix)
```

These remain `pending human` until a person records the observed outcome.

## P1 playground microphone/provider gate

The automated suite proves token scope/replay/rate limits, two-listener
isolation, WebRTC signaling authorization, durable reads, hot reload, the
embedded-wheel build, and frontend accessibility. Desktop and mobile visual
review also ran through the required browser automation workflow. A real
microphone/provider conversation still requires a human permission grant and
speech. With the three reference provider credentials already present in the
environment, run from the repository root:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_PLAYGROUND_PARENT="$(mktemp -d)"
VOICEKIT_PLAYGROUND_PROJECT="$VOICEKIT_PLAYGROUND_PARENT/browser-call"
uv run voicekit init "$VOICEKIT_PLAYGROUND_PROJECT" \
  --name browser-call \
  --recipe scratch \
  --description "Greet the caller and help them schedule an appointment." \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
(cd "$VOICEKIT_PLAYGROUND_PROJECT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" dev --port 7860)
```

Open `http://127.0.0.1:7861`, grant microphone access, speak at least two turns,
end the session, and verify transcript, latency, events, and the terminal-event
preview. Also confirm browser network URLs contain no bearer token. This gate
remains pending until that physical input and credentialed provider path run
green.

## P1 appointment recipe Evals

The complete native Pipecat text and audio manifests are packaged with every
appointment project. This environment currently has no `ANTHROPIC_API_KEY` and
no `ollama` executable, so neither credentialed suite is claimed green.

After injecting the reference-provider credentials into the process, create a
disposable recipe project from the repository root:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_EVAL_PARENT="$(mktemp -d)"
VOICEKIT_EVAL_PROJECT="$VOICEKIT_EVAL_PARENT/appointment-evals"
uv run voicekit init "$VOICEKIT_EVAL_PROJECT" \
  --name appointment-evals \
  --recipe appointment-booking \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
ollama pull gemma2:9b
```

Run the fast behavior suite:

```bash
(cd "$VOICEKIT_EVAL_PROJECT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/pipecat" eval suite evals/text-suite.yaml)
```

Then run the real STT→LLM→TTS audio path and retain recordings for manual
review:

```bash
(cd "$VOICEKIT_EVAL_PROJECT" && \
  "$VOICEKIT_REPO_ROOT/.venv/bin/pipecat" eval suite evals/audio-suite.yaml -a)
```

Run the dedicated 20-turn reference-stack latency gate. It reads the production
observer's persisted end-to-end samples, requires 20 distinct measured turns,
and enforces both percentile budgets:

```bash
"$VOICEKIT_REPO_ROOT/.venv/bin/python" \
  "$VOICEKIT_REPO_ROOT/tests/verification/p1_latency_gate.py" \
  --project "$VOICEKIT_EVAL_PROJECT"
```

The suite commands use Pipecat's installed runner and return 1 on any failed
scenario. The first audio run may download Kokoro/Moonshine model data. The
latency wrapper returns 2 when credentials are missing and 1 for a suite,
sample-count, model, or percentile failure. These gates remain pending until
the exact commands truly return 0. The local untracked backup has Deepgram and
Cartesia values but no Anthropic value, so the latency command was not run and
is not green.

## P1 Docker public deployment and paid smoke

The canonical image's local build, non-root/read-only start, storage preflight,
ready health contract, SIGTERM drain, zero exit, and fixed high/critical
vulnerability scan are automated. Those checks do not prove a public TLS edge
or a real carrier call.

Build the same unpublished engine wheel and generate artifacts in the target
agent project:

```bash
VOICEKIT_REPO_ROOT="$PWD"
uv build --wheel --out-dir dist
cd /path/to/agent-project
"$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" deploy docker \
  --engine-wheel \
  "$VOICEKIT_REPO_ROOT/dist/voicekit-0.0.0.dev0-py3-none-any.whl" \
  --skip-smoke
VOICEKIT_PUBLIC_BASE=https://voice.example.com \
  docker compose -f compose.voicekit.yaml up -d --build
curl --fail https://voice.example.com/health
```

After configuring the public reverse proxy and injecting the live provider,
webhook, integrator, and Twilio variables through the protected runtime
environment, point the owned number and place the explicitly confirmed paid
smoke:

```bash
"$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" numbers point +14155550123 \
  --url https://voice.example.com \
  --yes
"$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" deploy docker \
  --smoke https://voice.example.com \
  --to +15551234567 \
  --engine-wheel \
  "$VOICEKIT_REPO_ROOT/dist/voicekit-0.0.0.dev0-py3-none-any.whl" \
  --yes
```

Verify answer latency, greeting, speech in both directions, and acknowledged
results-webhook delivery, then stop the old generation and observe a
`container_drained` log with exit code zero. If the smoke fails, immediately
run the `voicekit numbers restore <rollback-token> --yes` command printed by
the cutover. This gate remains pending because no public target or live Twilio
credentials are available in the current environment.

## P3 Vobiz certification on Pipecat and LiveKit

Offline certification covers Vobiz Voice API request shapes, VobizXML,
V3/V2 callback signatures and nonce replay, PCMU/8 kHz serialization, one-use
media admission, provider-authoritative terminalization, route/intent fencing,
recording ingestion, the documented LiveKit UDP topology, deterministic
resource reuse, drift rejection, reverse rollback, and ambiguous writes. Those
tests do not prove the current account control plane or a PSTN conversation.

Export the Vobiz account, owned-number, public deployment, and paid-call
values:

```bash
export VOBIZ_AUTH_ID='MA_...'
export VOBIZ_AUTH_TOKEN='...'
export VOICEKIT_VOBIZ_LIVE_FROM='+9180...'
export VOICEKIT_VOBIZ_LIVE_TO='+91...'
export VOICEKIT_VOBIZ_TRANSFER_TO='+91...'
export VOICEKIT_LIVE_PUBLIC_BASE='https://voice.example.com'
export VOICEKIT_VOBIZ_LIVE_RECORDING_URL='https://provider-recording-url-from-a-verified-callback'
export VOICEKIT_LIVE_ROUTE_CONFIRM='I_ACKNOWLEDGE_ROUTE_MUTATION'
export VOICEKIT_LIVE_CONFIRM='I_ACKNOWLEDGE_PSTN_CHARGES'
```

Run the Pipecat/Voice API account, route restore, paid AMD/DTMF/recording/cold
transfer, and artifact-ingestion gates:

```bash
uv run pytest -q --no-cov -m live tests/live/test_vobiz_live.py
```

The route test restores in `finally`; preserve its rollback token and carrier
audit log. The recording URL must come from a callback whose HMAC was verified
by the deployed host. Never paste an unrelated URL merely to satisfy the
download test.

For the LiveKit path, create a Vobiz SIP credential and export the exact
existing values plus a deployed native agent and certification room:

```bash
export LIVEKIT_URL='wss://project.livekit.cloud'
export LIVEKIT_API_KEY='...'
export LIVEKIT_API_SECRET='...'
export VOICEKIT_LIVEKIT_AGENT_NAME='appointment-booking'
export VOICEKIT_LIVEKIT_SIP_URI='sip:project-id.sip.livekit.cloud'
export VOICEKIT_VOBIZ_SIP_CREDENTIAL_ID='...'
export VOICEKIT_VOBIZ_SIP_USERNAME='...'
export VOICEKIT_VOBIZ_SIP_PASSWORD='...'
export LIVEKIT_SIP_OUTBOUND_TRUNK='ST_...'
export VOICEKIT_LIVEKIT_CERT_ROOM='voicekit-vobiz-cert'
```

Run provision→idempotent reuse→reverse rollback and a paid outbound SIP call:

```bash
uv run pytest -q --no-cov -m live tests/live/test_vobiz_livekit_live.py
```

Then perform the physical both-path checklist:

1. Call the Vobiz number from a physical handset through the Pipecat route;
   verify the greeting, two-way speech, interruption clear, incoming DTMF,
   outbound DTMF, normal caller hangup, and exactly one acknowledged terminal
   result.
2. Place the guarded Pipecat outbound call; verify async AMD, the configured
   digit sequence, cold transfer to the second physical endpoint, recording
   callback, authenticated engine artifact, and route restoration after
   stopping `voicekit dev --phone`.
3. Re-provision the LiveKit route twice; confirm the second operation creates
   zero resources. Call inbound and outbound through Vobiz SIP, verify both
   speech directions and terminal mapping, then roll back and confirm the
   prior number binding is restored.
4. Inspect Vobiz and LiveKit: transport must be UDP/5060, inbound LiveKit
   addresses exactly `13.233.44.61/32`, and no test must claim TLS/SRTP.
5. Retain call ids, result event ids, recording ids, provisioning operation
   ids, timestamps, and the zero-exit test output without retaining secrets or
   raw caller PII.

This row stays pending until both commands return zero and the physical
checklist is recorded. A passing offline suite or provider dashboard screenshot
is not a substitute.

## P3 Plivo Beta certification on Pipecat and LiveKit

Offline certification covers installed-SDK V3 signature canonicalization,
nonce replay rejection, Plivo XML, PCMU/8 kHz bidirectional media, interruption
clear, provider-authoritative terminalization, route and outbound-intent
fencing, bounded recording ingestion, current Zentrunk request shapes,
deterministic adoption, drift rejection, ambiguity fencing, and reverse
rollback. It does not prove a funded Plivo account, provider region, or PSTN
conversation.

Export the Plivo account, owned number, public deployment, and paid-call
values:

```bash
export PLIVO_AUTH_ID='MA...'
export PLIVO_AUTH_TOKEN='...'
export VOICEKIT_PLIVO_LIVE_FROM='+1415...'
export VOICEKIT_PLIVO_LIVE_TO='+1415...'
export VOICEKIT_PLIVO_TRANSFER_TO='+1415...'
export VOICEKIT_LIVE_PUBLIC_BASE='https://voice.example.com'
export VOICEKIT_PLIVO_LIVE_RECORDING_URL='https://provider-url-from-a-verified-callback'
export VOICEKIT_LIVE_ROUTE_CONFIRM='I_ACKNOWLEDGE_ROUTE_MUTATION'
export VOICEKIT_LIVE_CONFIRM='I_ACKNOWLEDGE_PSTN_CHARGES'
```

Run the account/ownership, temporary-route rollback, paid AMD/DTMF/cold
transfer, and recording-ingestion gates:

```bash
uv run pytest -q --no-cov -m live tests/live/test_plivo_live.py
```

The route test restores in `finally`. The recording URL must come from a
callback whose V3 signature the deployed host verified; an arbitrary Plivo or
third-party URL is invalid evidence.

For the LiveKit path, export the project, Zentrunk credential, deployed native
agent, and certification room:

```bash
export LIVEKIT_URL='wss://project.livekit.cloud'
export LIVEKIT_API_KEY='...'
export LIVEKIT_API_SECRET='...'
export VOICEKIT_LIVEKIT_AGENT_NAME='appointment-booking'
export VOICEKIT_LIVEKIT_SIP_URI='sip:project-id.sip.livekit.cloud'
export VOICEKIT_PLIVO_SIP_USERNAME='voicekituser'
export VOICEKIT_PLIVO_SIP_PASSWORD='strong-special-value' # pragma: allowlist secret
export LIVEKIT_SIP_OUTBOUND_TRUNK='ST_...'
export VOICEKIT_LIVEKIT_CERT_ROOM='voicekit-plivo-cert'
```

Run provision→reuse→reverse rollback and the paid outbound SIP call:

```bash
uv run pytest -q --no-cov -m live tests/live/test_plivo_livekit_live.py
```

Then perform the physical both-path checklist:

1. Call the Plivo number through the Pipecat route; verify the greeting,
   two-way speech, interruption clear, incoming/outgoing DTMF, caller and agent
   hangup, and exactly one acknowledged terminal result.
2. Place the guarded Pipecat outbound call; verify async AMD, configured
   digits, cold transfer to a second physical endpoint, signed recording
   callback, authenticated engine artifact, and route restoration.
3. Provision the LiveKit route twice and confirm the second operation creates
   zero resources. Exercise inbound and outbound speech, DTMF, hangup, result
   delivery, and reverse rollback.
4. Inspect the interconnect: inbound Plivo origination is
   `<livekit-sip-host>;transport=tcp`; the LiveKit inbound trunk does not claim
   encrypted media; outbound Plivo `secure=true` agrees with LiveKit TLS and
   required media encryption.
5. For India destinations, pin the LiveKit region required by the provider
   guide and record it with the latency evidence.
6. Retain call, stream, recording, event, and provisioning ids plus timestamps
   and zero-exit output, without retaining credentials or raw caller PII.

Plivo stays Beta after these rows pass. Promotion to Certified is a separate
spec decision and requires nightly evidence, not just a dashboard screenshot.

## P3 generic SIP Beta loopback

The local suite proves only the LiveKit side: exact transport/media mapping,
optional CIDR restrictions, deterministic adoption, drift rejection,
write-ahead provisioning records, reverse rollback, and ambiguous-write
fencing. The external PBX/carrier route is deliberately operator-owned.

Export the LiveKit project and the exact external trunk values:

```bash
export LIVEKIT_URL='wss://project.livekit.cloud'
export LIVEKIT_API_KEY='...'
export LIVEKIT_API_SECRET='...'
export VOICEKIT_LIVEKIT_AGENT_NAME='appointment-booking'
export VOICEKIT_SIP_LIVE_FROM='+1415...'
export VOICEKIT_SIP_LIVE_TO='+1415...'
export VOICEKIT_SIP_ADDRESS='trunk.provider.example:5061'
export VOICEKIT_SIP_USERNAME='voicekit'
export VOICEKIT_SIP_PASSWORD='...'
export VOICEKIT_SIP_TRANSPORT='tls'
export VOICEKIT_SIP_MEDIA_ENCRYPTION='require'
export VOICEKIT_SIP_ALLOWED_ADDRESSES='203.0.113.0/24'
export LIVEKIT_SIP_OUTBOUND_TRUNK='ST_...'
export VOICEKIT_LIVEKIT_CERT_ROOM='voicekit-generic-sip-cert'
export VOICEKIT_LIVE_ROUTE_CONFIRM='I_ACKNOWLEDGE_ROUTE_MUTATION'
export VOICEKIT_LIVE_CONFIRM='I_ACKNOWLEDGE_PSTN_CHARGES'
```

Run the guarded LiveKit provision/reuse/rollback and paid loopback:

```bash
uv run pytest -q --no-cov -m live tests/live/test_generic_sip_live.py
```

Operator checklist:

1. Point the external trunk's inbound destination at the LiveKit SIP endpoint
   and configure its reverse route to `VOICEKIT_SIP_ADDRESS`.
2. Confirm username/password, source CIDRs, signaling transport, and media
   policy match exactly on both systems. Never use TLS with disabled media
   encryption.
3. Call inbound and outbound between physical endpoints; verify two-way audio,
   interruption, DTMF, both hangup directions, one terminal event, and the
   expected caller ID.
4. Run provisioning twice and verify zero new resources on the second pass.
   Roll back and confirm every voicekit-created LiveKit resource is removed.
5. Restore the external route manually and retain its audit evidence because
   voicekit does not own that control plane.

This row remains pending until the command and every external-route check pass.
It does not turn generic SIP into a Certified carrier.

## P3 first-party recipe provider conversations

All three P3 recipe sources, deterministic integrations, native entrypoints,
and 17 shared scenarios compile locally on both runtimes. Real STT→LLM→TTS
execution needs the locked reference-provider credentials and local Ollama.
From the repository root, run each recipe/runtime pair in a disposable project:

```bash
VOICEKIT_REPO_ROOT="$PWD"
VOICEKIT_RECIPE_PARENT="$(mktemp -d)"
for VOICEKIT_RECIPE in restaurant-reservations front-desk lead-intake; do
  for VOICEKIT_RUNTIME in pipecat livekit; do
    VOICEKIT_PROJECT="$VOICEKIT_RECIPE_PARENT/$VOICEKIT_RECIPE-$VOICEKIT_RUNTIME"
    uv run voicekit init "$VOICEKIT_PROJECT" \
      --name "$VOICEKIT_RECIPE-$VOICEKIT_RUNTIME" \
      --recipe "$VOICEKIT_RECIPE" \
      --channels web \
      --runtime "$VOICEKIT_RUNTIME" \
      --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
      --no-draft-prompts \
      --yes
    (
      cd "$VOICEKIT_PROJECT"
      "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test
      "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test --audio
      "$VOICEKIT_REPO_ROOT/.venv/bin/voicekit" test --report junit
    )
  done
done
```

For `front-desk`, repeat the live phone conversation after P3 warm-transfer
provisioning and verify that the human hears the private briefing before the
caller joins. For restaurant reservations, verify an unavailable large party
becomes waitlisted rather than confirmed. For lead intake, decline retention
consent and confirm no lead is persisted. These gates remain pending until all
commands return zero and those behaviors are observed; local compilation is not
promoted as provider-conversation evidence.
