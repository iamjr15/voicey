# External verification gaps

This file tracks gates that are fully implemented but cannot be truthfully marked green without credentials, paid resources, physical hardware, cloud access, or wall-clock time.

| Gate | Status | Exact runbook command | Requirement |
|---|---|---|---|
| P1 Twilio carrier/API certification | ready-to-run, pending credentials/PSTN | Commands below | Twilio test/live credentials, funded number, public target, PSTN |
| P1 cloudflared public WebSocket edge | ready-to-run, pending edge DNS/network | Command below | `cloudflared`, outbound Cloudflare access, generated quick-tunnel DNS |
| P1 physical-handset outbound check | ready-to-run, pending credentials/human | Paid PSTN command below | Physical handset, answering person, provisioned carrier numbers |
| P1 inbound audio/transcript loopback | not-ready (P1.10 dependency) | Added to the same harness after the recipe endpoint lands | Running Pipecat pipeline, physical handset and provisioned number |
| P1 full guided-wizard usability | ready-to-run, pending human/credentials | Command below | Interactive terminal, a human, provider keys |
| P1 doctor on deliberately broken project | ready-to-run, pending human observation | Commands below | Interactive terminal; disposable fixture only |
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

The inbound Pipecat runtime path now exists; the handset/audio-transcript
loopback joins this suite with the P1.10 recipe endpoint and Evals harness.
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
