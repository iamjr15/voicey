# External verification gaps

This file tracks gates that are fully implemented but cannot be truthfully marked green without credentials, paid resources, physical hardware, cloud access, or wall-clock time.

| Gate | Status | Exact runbook command | Requirement |
|---|---|---|---|
| P1 Twilio carrier/API certification | ready-to-run, pending credentials/PSTN | Commands below | Twilio test/live credentials, funded number, public target, PSTN |
| P1 physical-handset outbound check | ready-to-run, pending credentials/human | Paid PSTN command below | Physical handset, answering person, provisioned carrier numbers |
| P1 inbound audio/transcript loopback | not-ready (P1.6/P1.10 dependency) | Added to the same harness after the runtime/recipe endpoints land | Running Pipecat pipeline, physical handset and provisioned number |
| P2 Twilio–LiveKit certification | not-ready | Added with the P2 certification harness | LiveKit project, Twilio Elastic SIP trunk, PSTN |
| P2 Telnyx certification, both paths | not-ready | Added with the P2 certification harness | Funded Telnyx and LiveKit accounts |
| P3 tier-3 PSTN loopback | not-ready | Added with the P3 live-test harness | Certified carrier accounts and PSTN |
| P3 cloud deploys | not-ready | Added with each P3 deploy target | Pipecat Cloud, LiveKit Cloud, and Fly access |
| P4 Railway deploy | not-ready | Added with the P4 Railway target | Railway access |
| P4 24-hour soak | not-ready | Added with the P4 soak harness | 24 hours of uninterrupted runner time |

Statuses change to `ready-to-run, pending credentials/time` only after the harness, configuration, and exact command exist. A row moves to the completion report as green only after the command actually passes.
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

The inbound handset/audio-transcript loopback joins this suite after the P1
Pipecat runtime and recipe endpoints exist. Mocked carrier protocol and local
codec/tone-loopback evidence is green independently; it is not represented as
live carrier evidence.
