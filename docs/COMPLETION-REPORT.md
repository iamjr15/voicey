# Completion report

Implementation of the P0–P4 build plan is complete. Every credential-free,
locally runnable phase gate is green. The free-tier Railway companion and both
cloud runtimes passed real web/model/audio certification. The product is not
represented as fully released or fully externally certified: paid provider
media, live PSTN, human audio/handset checks, active-call drain, managed-object
compatibility, Fly, the full 24-hour soak, and public publishing remain pending
under the repository's reality boundary.

## Phase gate summary

| Phase | Local automation | External status | Reproduce |
|---|---|---|---|
| P0 — pins, repository, security baseline, dual walking skeleton | green | green | `uv run pytest -m integration --no-cov tests/integration/test_p0_walking_skeleton.py` |
| P1 — Pipecat engine, Twilio, CLI, playground, recipe, Docker | green | pending-live / pending-human | Local: `uv run python tests/verification/run_p1_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl`; credentialed: `uv run python tests/verification/run_p1_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl --require-live --latency-project "$VOICEY_EVAL_PROJECT"`; manual commands are in [GAPS](GAPS.md) |
| P2 — LiveKit parity, SIP, Telnyx, unified testing | green | pending-live / pending-human | Local: `uv run python tests/verification/run_p2_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl`; carrier: `uv run pytest -m live --no-cov tests/live/test_twilio_livekit_live.py tests/live/test_telnyx_live.py tests/live/test_telnyx_livekit_live.py`; microphone and handset checklists are in [GAPS](GAPS.md) |
| P3 — recipes, Vobiz, Plivo/SIP, tier-3 PSTN, cloud relay/deploy, warm transfer | green | pending-live / pending-human | Local: `VOICEY_TEST_POSTGRES_DSN=postgresql://... uv run python tests/verification/run_p3_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl`; carrier: `uv run pytest -m live --no-cov tests/live/test_vobiz_live.py tests/live/test_vobiz_livekit_live.py tests/live/test_plivo_live.py tests/live/test_plivo_livekit_live.py tests/live/test_generic_sip_live.py`; paid cloud/PSTN/handset commands are in [GAPS](GAPS.md) |
| P4 — hardening, observability, Railway, upgrade, release, docs, security | green | pending-time / pending-live / pending-human | Local: `VOICEY_TEST_POSTGRES_DSN=postgresql://... uv run python tests/verification/run_p4_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl`; soak: `uv run python tests/verification/p4_soak.py --duration-s 86400 --max-concurrent 8 --runtime both --report .voicey/verification/p4-24h-soak-report.json`; cloud and active-call drain commands are in [GAPS](GAPS.md) |

The final aggregate report is
`.voicey/verification/p4-gate-report.json`. Its
`local_automated_status` is `green`, its overall status is `pending-live`, and
all four pending rows name their unpromoted evidence class.

## Final local evidence

- Full Python suite: 1,127 passed, 41 truthful skips, 90.42% branch coverage on
  Python 3.11.
- Current post-certification regression: 1,177 passed, 41 truthful skips, and
  90.00% coverage; Ruff, formatting, strict Pyright, 11 frontend tests,
  frontend type checking/build, and Python/npm dependency audits are green.
- Reference-provider text certification: fresh appointment and P3 recipe
  projects on both runtimes used Claude Sonnet 5, native Anthropic judges,
  production typed tools, and durable result assertions. Appointment passed
  7+7 cases; Restaurant 5+5, Front Desk 6+6, and Lead Intake 6+6 passed too.
  All 48 cases passed on their first attempt. No Ollama request contributed to
  this evidence.
- Live no-call account/control-plane evidence: Twilio test credentials and live
  owned-number readiness passed; Twilio↔LiveKit and Vobiz↔LiveKit each passed
  provision, exact reuse, and reverse rollback. Vobiz live account readiness
  passed, prior routes were restored, and provider inspection found zero
  temporary resources. A fresh Twilio console inventory confirmed the sole
  account friendly name is `prod-voice`; neither `settle` nor `nudgely`
  remains. No unrelated API key was renamed or rotated, and no paid call was
  placed.
- Free-tier cloud evidence: Railway deployed a two-replica Singapore companion,
  singleton managed Postgres/volume, private bucket, and domain. Migration,
  object, fencing/rolling-generation, liveness, and five signed-readiness probes
  at 0.195–0.248 seconds passed. Pipecat Cloud and LiveKit Cloud in `ap-south`
  each passed native session startup, graceful `completed` terminal, explicit
  relay migration, and a Deepgram→Gemini→Cartesia model/audio probe through
  that relay. Pipecat captured 41,234 voiced samples; LiveKit captured 21,519.
  No Ollama request contributed to this evidence. Ownership-scoped teardown
  then removed both agents, Pipecat's disposable credentials, all Railway
  resources, and temporary Railway SSH access; the provider-retained
  soft-deleted project has zero services/buckets.
- Aggregate P3 regression: all eight local groups green, including SQLite and
  disposable PostgreSQL 17.
- Bounded hardening run: 864 calls started and terminalized across Pipecat and
  LiveKit in 5.038 seconds; peak active 16; zero active or file-descriptor leak.
  This is not the 24-hour gate.
- Observability wire test: two OTLP/HTTP protobuf requests, 1,995 bytes, both
  runtime Prometheus surfaces green, protected-payload scan green.
- Release canary: a fresh source-free install compiled and instantiated all
  four first-party recipes on both native runtimes.
- Documentation: both verbatim fresh-wheel quickstarts green in 43.170 seconds;
  native flow/Agent and typed-tool execution, provider-mocked browser/media
  connection, terminal result, and Standard Webhooks verification all green.
- Security: 18 signature-negative tests and 35 log/record/deploy secret-boundary
  tests green; 491 repository files scanned; Python and npm audits clean; wheel
  and sdist unpacked and secret-scanned; canonical Python 3.14 container built,
  started read-only/non-root, health-checked, SIGTERM-drained with exit zero,
  and scanned with zero fixed high/critical vulnerability or secret finding.
- Static/release gates: Ruff, formatting, strict Pyright, Actionlint, generated
  API reference, public snapshots, demo-audio validation, package build, and
  pre-commit checks are green.

## Spec §17 gate status

| Surface | Status | Evidence or exact pending command |
|---|---|---|
| First-run DX | local green; credentialed conversation pending-human | Both fresh-wheel docs quickstarts complete inside five minutes. Run the runtime-specific real microphone commands in [GAPS](GAPS.md). |
| Latency | pending-live | `uv run python tests/verification/p1_latency_gate.py --project "$VOICEY_EVAL_PROJECT"` |
| Recipes | all four recipe text suites green on both runtimes; provider audio/JUnit and physical transfer pending-live | Appointment and the 17 P3 scenarios passed every Pipecat and LiveKit case first attempt with the Anthropic API override. Run the remaining audio/JUnit and human warm-transfer commands in [GAPS](GAPS.md). |
| Telephony | local suites plus Twilio/Vobiz account and no-call LiveKit control planes green; paid PSTN pending-live | `uv run pytest -m live --no-cov tests/live`; only actually executed account/control-plane rows are promoted. Run paid media, recording, and handset commands from [GAPS](GAPS.md). |
| Webhook invariant | green | P1/P3/P4 aggregate chaos covers transaction rollback, provider/carrier/tool failure, actual SIGKILL, dual sweepers, and stale fencing. |
| CLI | automated contract green; human usability pending-human | Run the guided-wizard and deliberately broken-machine doctor procedures in [GAPS](GAPS.md). |
| Deploy | local invariants and Railway/Pipecat/LiveKit web smokes green; paid/drain/Fly pending-live | The Railway companion and both cloud workers passed real platform plus model/audio smokes. Run each target's remaining paid smoke, active-call replacement, and rollback sequence in [GAPS](GAPS.md). |
| Storage | green locally on SQLite and PostgreSQL 17; managed bucket pending-live | `VOICEY_LIVE_OBJECT_ACK=I_ACKNOWLEDGE_OBJECT_STORE_MUTATION uv run pytest -m live --no-cov tests/live/test_s3_artifacts_live.py` |
| Cloud relay | Railway companion and both immutable cloud workers green; paid PSTN/Fly pending-live | Signed relay admission, migration, platform sessions, durable completed terminals, and full model/audio probes passed on Railway, Pipecat Cloud, and LiveKit Cloud. Fly and paid carrier paths remain in [GAPS](GAPS.md). |
| Docs | green | `uv run python tests/verification/run_p4_docs_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl` |
| Reliability | bounded chaos/soak green; full soak pending-time | `uv run python tests/verification/p4_soak.py --duration-s 86400 --max-concurrent 8 --runtime both --report .voicey/verification/p4-24h-soak-report.json` |
| Security | green | `uv run python tests/verification/run_p4_security_gate.py --wheel dist/voicey-0.0.0.dev0-py3-none-any.whl` |

## Decisions taken

- The public name is Voicey. The unscoped npm name is reserved as
  `voicey@0.0.1`; the Python distribution is not published yet.
- The reference stack is Deepgram Nova-3, Anthropic Claude, and Cartesia Sonic
  3.5.
- The simulator judge defaults to local Ollama with an explicit cloud override.
  The reference text certification selected the native Anthropic override;
  the 2026-08-23 cloud model/audio evidence used Gemini. No current live
  certification request used Ollama.
- Pipecat is pinned at 1.6.0 and imports Flows from `pipecat.flows`; no
  standalone Flows package, custom flow DSL, or MCP product surface exists.
- LiveKit Agents is pinned at 1.6.7 and conversation behavior remains native
  LiveKit Agent workflows.
- The Vobiz LiveKit feasibility spike was positive, so both Pipecat and LiveKit
  paths ship behind executable capability evidence.
- Docker uses SQLite and local artifacts; Fly/Railway companions use Postgres
  and object storage; ephemeral cloud workers use the authenticated,
  user-owned results relay.
- Generated reference pages, illustrative recipe audio, and actual
  container-image security evidence follow the boundaries recorded in
  [decisions.md](decisions.md).

The append-only rationale, supersessions, installed-symbol observations, and
provider-specific safety boundaries are in [decisions.md](decisions.md).

## Gaps and human-only remainder

[GAPS.md](GAPS.md) contains 26 ready-to-run or human-only rows: 10 P1, 5 P2, 7
P3, and 4 P4. Each row identifies its missing credential, paid resource,
physical input, external route, or wall-clock requirement and gives the exact
command/checklist. No skipped live test is counted as green.

The remaining human-owned actions are:

1. Run the full 24-hour soak only if release policy requires it; the user
   explicitly declined that wall-clock exercise for the current validation.
2. Execute the remaining provider-audio/JUnit, paid carrier, managed-object,
   microphone, guided-wizard, doctor-usability, active-call drain, Fly, and
   physical-handset procedures when their accounts, balance, or human input
   exist.
3. Review the Voicey wheel/sdist and canary evidence.
4. Create any public repository/package/domain resources and publish manually.

Until those items pass, the accurate release state is: implementation complete,
local automation and free-tier cloud web/model/audio certification green;
paid/human/time certification and public release pending.
