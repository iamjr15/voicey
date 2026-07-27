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
| P1 Docker public deployment + paid smoke | ready-to-run, pending public ingress/credentials/PSTN | Commands below | Public HTTPS ingress, live Twilio credentials, owned number, paid destination |
| P2 Twilio–LiveKit certification | not-ready | Added with the P2 certification harness | LiveKit project, Twilio Elastic SIP trunk, PSTN |
| P2 Telnyx certification, both paths | not-ready | Added with the P2 certification harness | Funded Telnyx and LiveKit accounts |
| P3 tier-3 PSTN loopback | not-ready | Added with the P3 live-test harness | Certified carrier accounts and PSTN |
| P3 cloud deploys | not-ready | Added with each P3 deploy target | Pipecat Cloud, LiveKit Cloud, and Fly access |
| P4 Railway deploy | not-ready | Added with the P4 Railway target | Railway access |
| P4 24-hour soak | not-ready | Added with the P4 soak harness | 24 hours of uninterrupted runner time |

Statuses change to `ready-to-run, pending …` only after the harness,
configuration, and exact command exist. A row moves to the completion report
as green only after the command actually passes.
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

Both commands use Pipecat's installed suite runner and return 1 on any failed
scenario. The first audio run may download Kokoro/Moonshine model data. These
gates remain `pending credentials/local judge` until the exact commands truly
return 0.

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
