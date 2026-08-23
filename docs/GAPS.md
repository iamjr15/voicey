# External verification gaps

This file tracks gates that are fully implemented but cannot be truthfully marked green without credentials, paid resources, physical hardware, cloud access, or wall-clock time.

| Gate | Status | Exact runbook command | Requirement |
|---|---|---|---|
| P1 Twilio carrier/API certification | account/API green; route/PSTN pending | Commands below | Public target, paid destination, route acknowledgement, physical endpoint |
| P1 cloudflared public WebSocket edge | ready-to-run, pending edge DNS/network | Command below | `cloudflared`, outbound Cloudflare access, generated quick-tunnel DNS |
| P1 physical-handset outbound check | ready-to-run, pending credentials/human | Paid PSTN command below | Physical handset, answering person, provisioned carrier numbers |
| P1 inbound audio/transcript loopback | ready-to-run, pending credentials/human | Commands below | Running appointment recipe, physical handset, and provisioned number |
| P1 full guided-wizard usability | ready-to-run, pending human/credentials | Command below | Interactive terminal, a human, provider keys |
| P1 doctor on deliberately broken project | ready-to-run, pending human observation | Commands below | Interactive terminal; disposable fixture only |
| P1 playground real microphone/provider call | ready-to-run, pending human/credentials | Command below | Valid reference-provider keys, browser microphone grant, a person speaking |
| P1 appointment audio Evals | ready-to-run, pending model downloads/audio execution | Commands below | Reference-provider keys, Anthropic API judge config, and one-time Kokoro/Moonshine downloads |
| P1 reference audio latency | ready-to-run, pending credentials | Command below | Deepgram, Anthropic, and Cartesia keys; p50 ≤ 800 ms and p95 ≤ 1500 ms |
| P1 Docker public deployment + paid smoke | ready-to-run, pending public ingress/credentials/PSTN | Commands below | Public HTTPS ingress, live Twilio credentials, owned number, paid destination |
| P2 Twilio–LiveKit PSTN certification | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded LiveKit/Twilio accounts, deployed agent, two physical endpoints |
| P2 LiveKit playground real microphone/provider call | ready-to-run, pending credentials/human | Command below | LiveKit project, reference-provider keys, browser microphone grant, a person speaking |
| P2 appointment LiveKit conversation | ready-to-run, pending credentials/human | Command and checklist below | LiveKit project, reference-provider keys, browser microphone grant, a person exercising all handoffs |
| P2 unified appointment audio suite, both runtimes | ready-to-run, pending model downloads/audio execution | Commands below | Reference-provider keys, Anthropic API judge config, Kokoro/Moonshine downloads, and both generated projects |
| P2 Telnyx certification, both paths | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded Telnyx/LiveKit accounts, both carrier paths, public target, PSTN |
| P3 Vobiz certification, both paths | account/control-plane green; PSTN/human pending | Commands and checklist below | Public Pipecat target, paid destination, recording callback, physical endpoints |
| P3 Plivo Beta certification, both paths | ready-to-run, pending credentials/PSTN/human | Commands and checklist below | Funded Plivo/LiveKit accounts, public Pipecat target, Zentrunk credentials, PSTN |
| P3 generic SIP Beta loopback | ready-to-run, pending external route/PSTN/human | Commands and checklist below | LiveKit project, operator-managed PBX/carrier trunk, physical endpoints |
| P3 tier-3 PSTN loopback | ready-to-run, pending credentials/PSTN | Commands below | Funded Twilio or LiveKit SIP path, deployed target agent, reference/judge keys, ngrok for Pipecat, and paid PSTN |
| P3 managed object-store compatibility | ready-to-run, pending credentials | Command below | Private S3-compatible bucket with create/read/delete permission |
| P3 Fly results companion | ready-to-run, pending credentials/paid cloud | Commands below | Authenticated Fly CLI, organization billing, Managed Postgres, and Tigris |
| P3 cloud-worker deploys | ready-to-run, pending credentials/paid cloud | Commands below | Authenticated Pipecat Cloud and LiveKit Cloud projects, deployed companion, registry, provider credentials, and paid PSTN |
| P4 Railway deploy | ready-to-run, pending credentials/paid cloud | Commands below | Authenticated billed Railway workspace, managed Postgres, private bucket, and paired cloud worker for paid media evidence |
| P4 24-hour soak | ready-to-run, pending 24 hours | Command below | 24 hours of uninterrupted self-hosted runner time |
| P4 live rolling drain on every target | ready-to-run, pending credentials/paid calls | P4.1 procedure below, using each target's exact deploy command above | Public Docker ingress plus authenticated Fly, Pipecat Cloud, LiveKit Cloud, and Railway targets with active paid calls |
| P4 public canary/stable publication | human-only, pending final name/review | Run private `Prepare release artifacts`, then execute `RENAME.md`; no public-upload command is automated | Final name, package-index ownership, human artifact review and explicit publish approval |

Statuses change to `ready-to-run, pending …` only after the harness,
configuration, and exact command exist. A row moves to the completion report
as green only after the command actually passes.

## P3 managed object-store compatibility

Local tests exercise the exact S3 client contract without external
credentials. To certify the selected private bucket, grant the test identity
read/write/delete access only below a disposable prefix and run:

The AWS CLI is authenticated as of 2026-08-03. No dedicated private disposable
bucket/prefix was selected, so no object mutation was attempted and this gate
remains pending.

```bash
export VOICEY_LIVE_OBJECT_ACK='I_ACKNOWLEDGE_OBJECT_STORE_MUTATION'
export VOICEY_OBJECT_BUCKET='private-voicey-artifacts'
export VOICEY_OBJECT_PREFIX='voicey-certification'
export VOICEY_OBJECT_ENDPOINT='https://fly.storage.tigris.dev'
export AWS_REGION='auto'
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'
uv run pytest -q --no-cov -m live tests/live/test_s3_artifacts_live.py
```

For AWS S3, omit `VOICEY_OBJECT_ENDPOINT` and use the bucket's real region.
For a loopback MinIO test only, set
`VOICEY_OBJECT_FORCE_PATH_STYLE=true`. A pass means the bucket was reachable,
one checksummed probe was read back byte-for-byte, and that probe was deleted.
It does not promote the Fly companion or either cloud deployment.

## P3 Fly results companion

Local tests prove command selection, explicit adoption, owner-only checkpoints,
secret rotation continuity, reverse rollback ownership, generated topology,
platform/signed smoke behavior, and the service's real Postgres preflight. The
Fly CLI is installed but `fly auth whoami` is unauthenticated as of 2026-08-03,
so no external resource or paid service is represented as green.

Install and authenticate the Fly CLI, choose a disposable web-only voicey
agent project, and run the complete gate from this repository:

```bash
export VOICEY_REPO_ROOT="$PWD"
export VOICEY_AGENT_PROJECT='/absolute/path/to/web-only-agent-project'
export VOICEY_FLY_APP='voicey-results-cert'
export VOICEY_FLY_ORG='exact-org-slug'
export VOICEY_FLY_REGION='iad'
export VOICEY_FLY_PG='voicey-results-cert-pg'
export VOICEY_FLY_BUCKET='voicey-results-cert-objects'
fly auth whoami
uv build --out-dir dist
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy fly \
    --app "$VOICEY_FLY_APP" \
    --org "$VOICEY_FLY_ORG" \
    --region "$VOICEY_FLY_REGION" \
    --postgres-name "$VOICEY_FLY_PG" \
    --postgres-plan Basic \
    --postgres-volume-gb 10 \
    --bucket "$VOICEY_FLY_BUCKET" \
    --engine-wheel \
      "$VOICEY_REPO_ROOT/dist/voicey-0.0.0.dev0-py3-none-any.whl" \
    --yes \
    --json
)
```

The JSON must show `deployed=true`, `smoke_green=true`, at least one passing
platform check, liveness, and signed readiness. Fly logs must show the
checksummed migrations, S3 round trip, and rollback-only rolling-generation
probe before server admission. Confirm the bucket is private and that the
resource ledger contains identifiers/fingerprints but none of the values in
`.env`.

Rerun the identical deploy command and verify no new app, cluster, or bucket
is created. Then exercise current/previous credential cutover:

```bash
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy fly \
    --app "$VOICEY_FLY_APP" \
    --org "$VOICEY_FLY_ORG" \
    --region "$VOICEY_FLY_REGION" \
    --postgres-name "$VOICEY_FLY_PG" \
    --postgres-plan Basic \
    --postgres-volume-gb 10 \
    --bucket "$VOICEY_FLY_BUCKET" \
    --rotate-credentials \
    --engine-wheel \
      "$VOICEY_REPO_ROOT/dist/voicey-0.0.0.dev0-py3-none-any.whl" \
    --yes
)
```

Retain redacted app, MPG, bucket, release, Machine, health-check, and signed
readiness evidence. To remove the disposable resources after evidence capture,
run the exact same resource flags with `--rollback-created --yes`; verify
Tigris, MPG, and app are deleted in that order. Do not run rollback against an
adopted or production resource set.

## P3 Pipecat Cloud and LiveKit Cloud workers

The local suite proves secret-free images, native runtime entrypoints,
worker-secret filtering, signed relay preflight, ownership/adoption drift
fences, hosted carrier answers, resumable deploys, created-only/version
rollback, platform session/room smoke, paid phone-smoke orchestration, and
durable begin/terminal invariants. It does not prove either paid cloud control
plane. This machine has `pipecat-ai-cli==1.3.0` with
`pipecatcloud==1.1.0`, and `lk==2.16.2`; both CLIs can
read their authenticated organizations/projects as of 2026-08-03. Deployment
still cannot start without a signed results companion, a selected immutable
registry image, and the paid smoke destination. Docker is installed but its
daemon is stopped. No platform resource or call is represented as green.

First complete the Fly companion gate above and retain its public base and
generated `VOICEY_RELAY_CREDENTIAL` in the agent project's ignored,
owner-only `.env`. From this repository, build the unpublished engine wheel:

```bash
export VOICEY_REPO_ROOT="$PWD"
export VOICEY_AGENT_PROJECT='/absolute/path/to/agent-project'
export VOICEY_RELAY_URL='https://voicey-results-cert.fly.dev'
export VOICEY_ENGINE_WHEEL="$PWD/dist/voicey-0.0.0.dev0-py3-none-any.whl"
uv build --out-dir dist
```

For Pipecat Cloud, choose an immutable registry tag and exact account values,
authenticate, verify the current region list, then prepare/build/push without
platform mutation:

```bash
export VOICEY_PCC_AGENT='voicey-cloud-cert'
export VOICEY_PCC_ORG='exact-pipecat-org'
export VOICEY_PCC_REGION='us-west'
export VOICEY_PCC_SECRET_SET='voicey-cloud-cert-secrets' # pragma: allowlist secret
export VOICEY_PCC_IMAGE='registry.example.com/voicey/cloud-cert:git-sha'
pipecat cloud auth login
pipecat cloud auth whoami
pipecat cloud regions list
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy pipecat-cloud \
    --agent "$VOICEY_PCC_AGENT" \
    --org "$VOICEY_PCC_ORG" \
    --region "$VOICEY_PCC_REGION" \
    --secret-set "$VOICEY_PCC_SECRET_SET" \
    --image "$VOICEY_PCC_IMAGE" \
    --min-agents 1 \
    --max-agents 4 \
    --profile agent-1x \
    --relay-url "$VOICEY_RELAY_URL" \
    --engine-wheel "$VOICEY_ENGINE_WHEEL" \
    --prepare-only
  docker build \
    --platform linux/arm64 \
    -t "$VOICEY_PCC_IMAGE" \
    .voicey/deploy/pipecat-cloud/context
  docker push "$VOICEY_PCC_IMAGE"
)
```

Use a disposable Twilio phone project for the full automatic cutover and paid
smoke, with its owned `phone_number` in `voicey.jsonc`:

```bash
export VOICEY_CLOUD_SMOKE_TO='+14155550199'
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy pipecat-cloud \
    --agent "$VOICEY_PCC_AGENT" \
    --org "$VOICEY_PCC_ORG" \
    --region "$VOICEY_PCC_REGION" \
    --secret-set "$VOICEY_PCC_SECRET_SET" \
    --image "$VOICEY_PCC_IMAGE" \
    --min-agents 1 \
    --max-agents 4 \
    --profile agent-1x \
    --relay-url "$VOICEY_RELAY_URL" \
    --engine-wheel "$VOICEY_ENGINE_WHEEL" \
    --smoke-to "$VOICEY_CLOUD_SMOKE_TO" \
    --yes \
    --json
)
```

The JSON must report ready platform/relay/session smoke, a terminal
`smoke_call_id`, and no credential value. The companion must contain both the
platform-session begin/terminal record and paid-call terminal result with
delivered webhook status. Repeat the identical command and verify it resumes
without creating a second agent. For Telnyx, first configure its TeXML
Application to the exact companion URL printed by the command and add
`--telnyx-texml-ready`.

After evidence capture, restore the carrier route and delete only the
voicey-created disposable agent:

```bash
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy pipecat-cloud \
    --agent "$VOICEY_PCC_AGENT" \
    --org "$VOICEY_PCC_ORG" \
    --region "$VOICEY_PCC_REGION" \
    --secret-set "$VOICEY_PCC_SECRET_SET" \
    --image "$VOICEY_PCC_IMAGE" \
    --min-agents 1 \
    --max-agents 4 \
    --profile agent-1x \
    --relay-url "$VOICEY_RELAY_URL" \
    --rollback-created \
    --yes \
    --json
)
```

For LiveKit Cloud, use a LiveKit-runtime project with its carrier SIP
provisioning already complete. Authenticate, verify the exact project, and set
the outbound trunk used only for the paid smoke:

```bash
export VOICEY_LK_AGENT='voicey-cloud-cert'
export VOICEY_LK_PROJECT='exact-livekit-project'
export VOICEY_LK_REGION='us-west'
export VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID='ST_...'
lk cloud auth
lk project list --json
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy livekit-cloud \
    --agent "$VOICEY_LK_AGENT" \
    --project "$VOICEY_LK_PROJECT" \
    --region "$VOICEY_LK_REGION" \
    --relay-url "$VOICEY_RELAY_URL" \
    --engine-wheel "$VOICEY_ENGINE_WHEEL" \
    --smoke-to "$VOICEY_CLOUD_SMOKE_TO" \
    --yes \
    --json
)
```

The JSON must report ready platform/relay/session smoke. Retain the room,
dispatch, SIP participant, call, agent, version, and terminal relay ids without
credentials or raw caller PII. Rerun with the same inputs and confirm it
deploys one new version while retaining the previous version for rollback.
Then run:

```bash
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy livekit-cloud \
    --agent "$VOICEY_LK_AGENT" \
    --project "$VOICEY_LK_PROJECT" \
    --region "$VOICEY_LK_REGION" \
    --relay-url "$VOICEY_RELAY_URL" \
    --rollback \
    --yes \
    --json
)
```

Verify that a second version rolls back to the exact checkpointed previous
version; a disposable first version created by voicey is deleted instead.
Neither command may delete an adopted agent. Both cloud rows remain
pending-live until these commands and one real browser-media conversation per
web runtime pass.

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
export VOICEY_LIVE_PSTN_ACK='I_ACKNOWLEDGE_PAID_PSTN'
export VOICEY_LIVE_PSTN_MAX_CALLS=4
export VOICEY_LIVE_TARGET_NUMBER='+14155550123'
export VOICEY_LIVE_TWILIO_FROM='+14155550124'
export TWILIO_ACCOUNT_SID='AC…'
export TWILIO_AUTH_TOKEN='…'
export NGROK_AUTHTOKEN='…'
export DEEPGRAM_API_KEY='…'
export ANTHROPIC_API_KEY='…'
export CARTESIA_API_KEY='…'
export OPENAI_API_KEY='…'
(cd tests/fixtures/live-pstn-pipecat && \
  ../../../.venv/bin/voicey test --live --report junit)
```

For the LiveKit path, configure an outbound SIP trunk that can reach the
target destination and run:

```bash
export VOICEY_LIVE_PSTN_ACK='I_ACKNOWLEDGE_PAID_PSTN'
export VOICEY_LIVE_PSTN_MAX_CALLS=4
export VOICEY_LIVE_TARGET_NUMBER='+14155550123'
export VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID='ST_…'
export LIVEKIT_URL='wss://project.livekit.cloud'
export LIVEKIT_API_KEY='…'
export LIVEKIT_API_SECRET='…'
export DEEPGRAM_API_KEY='…'
export ANTHROPIC_API_KEY='…'
export CARTESIA_API_KEY='…'
export OPENAI_API_KEY='…'
(cd tests/fixtures/live-pstn-livekit && \
  ../../../.venv/bin/voicey test --live --report junit)
```

Each result must be a first-attempt pass, carrier status `completed`, contain
both caller and target-agent transcript lines, and write
`.voicey/test-results.xml` with `evidence.provider`,
`evidence.provider_call_id`, `evidence.runtime_call_id`,
`evidence.path`, and `evidence.terminal_status`. Inspect the carrier account to
confirm no more than four calls were placed by either job.

Nightly automation is `.github/workflows/live-pstn.yml`. Configure the
protected `paid-pstn` environment with the named secrets, set repository
variable `VOICEY_LIVE_PSTN_ENABLED=true`, and set repository variable
`VOICEY_LIVE_PSTN_ACK=I_ACKNOWLEDGE_PAID_PSTN`. The workflow uses
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
On 2026-08-03, fresh generated projects ran all seven text cases on both
runtimes with Deepgram Nova-3, Claude Sonnet 5, Cartesia Sonic 3.5, and native
Anthropic judges. Each runtime passed every case on its first attempt. Ollama
was not used. The text provider gate is green; only the real PCM audio tier in
this section remains pending.

For that remaining gate, export `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, and
`CARTESIA_API_KEY` and use the Anthropic `tests/voicey-test.jsonc` configuration
shown verbatim in `docs/testing.md`.

In another terminal from the repository root, create both disposable projects:

```bash
VOICEY_REPO_ROOT="$PWD"
VOICEY_TEST_PARENT="$(mktemp -d)"
VOICEY_TEST_PIPECAT="$VOICEY_TEST_PARENT/appointment-pipecat"
VOICEY_TEST_LIVEKIT="$VOICEY_TEST_PARENT/appointment-livekit"
uv run voicey init "$VOICEY_TEST_PIPECAT" \
  --name appointment-pipecat \
  --recipe appointment-booking \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
uv run voicey init "$VOICEY_TEST_LIVEKIT" \
  --name appointment-livekit \
  --recipe appointment-booking \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

To reproduce the completed text evidence, run `voicey test --report json` in
each project. Run the remaining audio gate and retain JUnit output with:

```bash
(cd "$VOICEY_TEST_PIPECAT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --audio && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --audio --report junit)
(cd "$VOICEY_TEST_LIVEKIT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --audio && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --audio --report junit)
```

Each audio command must return zero, each runtime must report all seven cases
at 100% stability, and both JUnit files must contain zero failures. The first
audio run may download Kokoro and Moonshine. No audio command has completed
green yet, so the audio row remains in the gap table.

## P2 LiveKit playground microphone/provider gate

The local suite proves one-use voicey token exchange, durable reservation,
room-token scope, replay rejection, exchange-failure terminalization, native
client state/audio/transcript wiring, the three-process supervisor, and native
scratch import. Desktop and 390×844 browser automation completed the exchange
against the credentialless fixture. The SDK then reported its real provider
connection failure; no media success was inferred.

With valid reference-provider and LiveKit project credentials exported, create
and run a disposable project:

```bash
VOICEY_REPO_ROOT="$PWD"
VOICEY_LIVEKIT_PARENT="$(mktemp -d)"
VOICEY_LIVEKIT_PROJECT="$VOICEY_LIVEKIT_PARENT/livekit-browser"
uv run voicey init "$VOICEY_LIVEKIT_PROJECT" \
  --name livekit-browser \
  --recipe scratch \
  --description "Greet the caller and answer concise product questions." \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
(cd "$VOICEY_LIVEKIT_PROJECT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" doctor && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" dev --port 7860)
```

Required process variables are `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`,
`CARTESIA_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and
`LIVEKIT_API_SECRET`. Open `http://127.0.0.1:7861`, grant microphone access,
speak at least two turns, end the session, and confirm remote audio, streaming
and durable transcript, latency/event panels, and exactly one terminal event.
In browser network inspection, confirm the voicey bearer appears only in the
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
VOICEY_REPO_ROOT="$PWD"
VOICEY_RECIPE_PARENT="$(mktemp -d)"
VOICEY_RECIPE_PROJECT="$VOICEY_RECIPE_PARENT/livekit-appointments"
uv run voicey init "$VOICEY_RECIPE_PROJECT" \
  --name livekit-appointments \
  --recipe appointment-booking \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
(cd "$VOICEY_RECIPE_PROJECT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" doctor && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" dev --port 7860)
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
   followed by human escalation when `VOICEY_TRANSFER_NUMBER` is configured.
5. Ending the session produces exactly one terminal event with the expected
   appointment outcome and native tool observations.

LiveKit and Anthropic credentials are available. This command is still not
green because no human granted browser microphone access and completed the
spoken workflow.

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
VOICEY_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
  uv run pytest -m live --no-cov \
  tests/live/test_twilio_livekit_live.py::test_live_twilio_livekit_provision_reuse_and_rollback
```

Required variables: `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`VOICEY_TWILIO_LIVE_FROM`, `VOICEY_LIVEKIT_AGENT_NAME`,
`VOICEY_LIVEKIT_SIP_URI`, `VOICEY_TWILIO_SIP_DOMAIN`,
`VOICEY_TWILIO_SIP_USERNAME`, and `VOICEY_TWILIO_SIP_PASSWORD`.

This no-call gate passed on 2026-08-03 against the live Twilio and LiveKit
control planes. The first generated password exposed Twilio error `21240`; the
product now rejects passwords before mutation unless they have at least 12
characters plus lowercase, uppercase, and a digit. A compliant rerun passed
provision, exact reuse, and reverse rollback. Provider API reads and both
consoles showed zero temporary trunks, credential lists, dispatch rules, or
number bindings afterward. This does not promote a call, recording, or handset
gate.

For the paid outbound gate, retain the provisioned outbound trunk, set its id
as `LIVEKIT_SIP_OUTBOUND_TRUNK`, identify the certification room and
destination, then explicitly acknowledge charges:

```bash
VOICEY_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
  uv run pytest -m live --no-cov \
  tests/live/test_twilio_livekit_live.py::test_live_twilio_livekit_paid_outbound_and_sip_status_mapping
```

After either live call completes, copy LiveKit's documented
`sip.twilio.callSid` participant attribute into
`VOICEY_TWILIO_LIVE_CALL_SID` and verify that Core Recordings exposes exactly
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
5. verify Twilio created a dual-channel recording and voicey emitted the
   stable recording reference plus `call.recording.ready`;
6. verify exactly one terminal event and one acknowledged or visibly
   dead-lettered delivery for every call;
7. roll back the provisioning token and confirm the original number route.

Inspect the durable evidence after each call:

```bash
voicey calls list
voicey calls show <call-id>
```

The no-call provisioning command is green. The paid outbound command, completed
recording-correlation command, and physical checklist are not green until they
genuinely pass. The account variables exist, but no deployed certification
agent, approved paid destination/answering endpoint, completed call SID, or
physical handset evidence is available.

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
`VOICEY_TELNYX_LIVE_FROM`:

```bash
uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_account_and_owned_number_are_ready
```

With the Pipecat agent running at `VOICEY_LIVE_PUBLIC_BASE`, this guarded
command temporarily points the number to the configured Voice API connection,
checks the FULL-durability route record, and restores the exact snapshot:

```bash
VOICEY_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_route_point_and_crash_safe_restore
```

The paid Call Control gate additionally needs
`VOICEY_TELNYX_LIVE_TO`, `VOICEY_TELNYX_TRANSFER_TO`, a person answering
the endpoint, and destination permissions. It starts dual-channel recording,
sends `12#`, cold-transfers, and guarantees a final hangup:

```bash
VOICEY_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_paid_pstn_dtmf_recording_and_cold_transfer
```

After the verified runtime captures `call.recording.saved`, export its `mp3`
or `wav` URL as `VOICEY_TELNYX_LIVE_RECORDING_URL` and prove that engine-owned
artifact ingestion succeeds:

```bash
uv run pytest -m live --no-cov \
  tests/live/test_telnyx_live.py::test_telnyx_live_signed_recording_url_ingests_to_engine_storage
```

For the no-call LiveKit path, also export `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `VOICEY_LIVEKIT_AGENT_NAME`,
`VOICEY_LIVEKIT_SIP_URI`, `VOICEY_TELNYX_SIP_USERNAME`, and
`VOICEY_TELNYX_SIP_PASSWORD`. The test provisions both control planes
twice, proves reuse, then rolls back:

```bash
VOICEY_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
  uv run pytest -m live --no-cov \
  tests/live/test_telnyx_livekit_live.py::test_live_telnyx_livekit_provision_reuse_and_rollback
```

Retain a provisioned outbound trunk as `LIVEKIT_SIP_OUTBOUND_TRUNK`, set
`VOICEY_LIVEKIT_CERT_ROOM`, and run the paid LiveKit SIP call:

```bash
VOICEY_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
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

This workspace has no Telnyx credentials or provisioned Telnyx route. LiveKit
is authenticated, but all six carrier tests still skip or remain unrun, and no
Telnyx account, route, paid-call, recording, or handset result is marked green.

## P1 cloudflared public WebSocket edge

The harness starts a loopback FastAPI listener, installs an ephemeral
challenge-only WebSocket endpoint, creates a cloudflared quick tunnel, verifies
the exact challenge through the public `wss://` route, and tears down both
processes:

```bash
VOICEY_LIVE_TUNNEL_CONFIRM=I_ACKNOWLEDGE_PUBLIC_TUNNEL \
  uv run pytest -m live --no-cov \
  tests/live/test_tunnel_live.py::test_cloudflared_quick_tunnel_websocket_round_trip
```

On 2026-07-27, cloudflared 2026.3.0 connected and emitted a
`trycloudflare.com` URL on three attempts, but the generated hostname remained
unresolvable for the full 60-second probe deadline (`gaierror`). The harness
failed and cleaned up every child process, so this edge gate remains pending.

## P1 Twilio credential and PSTN gates

- **Twilio no-charge test-credential Calls API:** green on 2026-08-03. The test
  credentials accepted the outbound request contract without placing or
  charging for a real PSTN call.

  ```bash
  uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_test_credentials_accept_outbound_contract_without_charge
  ```

- **Twilio live account/owned-number readiness:** green on 2026-08-03. The live
  account authenticated and the configured from-number was owned by it; the
  test is read-only and placed no call.

  ```bash
  uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_live_account_and_owned_number_are_ready
  ```

- **Twilio route mutation + crash-safe restore:** ready-to-run, pending a
  reachable `VOICEY_LIVE_PUBLIC_BASE`, owned number, live credentials, and
  explicit `VOICEY_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION`.

  ```bash
  VOICEY_LIVE_ROUTE_CONFIRM=I_ACKNOWLEDGE_ROUTE_MUTATION \
    uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_live_route_point_and_crash_safe_restore
  ```

- **Twilio paid PSTN/physical-handset, outbound DTMF, dual-channel recording,
  and cold transfer:** ready-to-run, pending the running public Pipecat target,
  live from/to/transfer numbers, credentials, a person answering the handset,
  and explicit `VOICEY_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES`.

  ```bash
  VOICEY_LIVE_CONFIRM=I_ACKNOWLEDGE_PSTN_CHARGES \
    uv run pytest -m live --no-cov \
    tests/live/test_twilio_live.py::test_twilio_live_paid_pstn_dtmf_recording_and_cold_transfer
  ```

The inbound Pipecat runtime path and appointment recipe now exist. Run a
credentialed recipe project through `voicey dev --phone`, call its owned
number from a physical handset, book and then change an appointment, and inspect
the durable transcript:

```bash
VOICEY_TRANSFER_NUMBER=+14155550199 voicey dev --phone
voicey calls list
voicey calls show <call-id>
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
VOICEY_MANUAL_PROJECT="$(mktemp -d)/human-wizard"
uv run voicey init "$VOICEY_MANUAL_PROJECT"
```

Verify every choice starts unselected, scratch is last, channel multi-select
requires an explicit selection, each pasted key is validated, and the final
`Next:` command starts the generated project. Interrupt once before completion,
then run:

```bash
uv run voicey init "$VOICEY_MANUAL_PROJECT" --resume
```

The broken-machine doctor gate uses a disposable project rather than damaging
the host. From the repository root:

```bash
VOICEY_REPO_ROOT="$PWD"
VOICEY_BROKEN_PROJECT="$(mktemp -d)"
uv run python tests/manual/prepare_broken_doctor.py "$VOICEY_BROKEN_PROJECT"
(cd "$VOICEY_BROKEN_PROJECT" && "$VOICEY_REPO_ROOT/.venv/bin/voicey" doctor)
```

The run is expected to exit non-zero and visibly diagnose missing provider and
carrier credentials, missing webhook secret, `.env.example` drift, unreachable
signed receiver, and route/account checks it cannot safely perform. Then run
the safe subset and verify that secrets are not printed:

```bash
(cd "$VOICEY_BROKEN_PROJECT" && "$VOICEY_REPO_ROOT/.venv/bin/voicey" doctor --fix)
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
VOICEY_REPO_ROOT="$PWD"
VOICEY_PLAYGROUND_PARENT="$(mktemp -d)"
VOICEY_PLAYGROUND_PROJECT="$VOICEY_PLAYGROUND_PARENT/browser-call"
uv run voicey init "$VOICEY_PLAYGROUND_PROJECT" \
  --name browser-call \
  --recipe scratch \
  --description "Greet the caller and help them schedule an appointment." \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
(cd "$VOICEY_PLAYGROUND_PROJECT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" dev --port 7860)
```

Open `http://127.0.0.1:7861`, grant microphone access, speak at least two turns,
end the session, and verify transcript, latency, events, and the terminal-event
preview. Also confirm browser network URLs contain no bearer token. This gate
remains pending until that physical input and credentialed provider path run
green.

## P1 appointment recipe Evals

The complete native Pipecat text and audio manifests are packaged with every
appointment project. On 2026-08-03 a fresh generated Pipecat project ran the
full seven-case text suite through the production Pipecat Eval transport with
Deepgram Nova-3, Claude Sonnet 5, Cartesia Sonic 3.5, native Anthropic judging,
typed tools, and durable result assertions. Every case passed on its first
attempt. Ollama was not used. The text gate is green; audio and reference
latency remain pending below.

After injecting the reference-provider credentials into the process, create a
disposable recipe project from the repository root:

```bash
VOICEY_REPO_ROOT="$PWD"
VOICEY_EVAL_PARENT="$(mktemp -d)"
VOICEY_EVAL_PROJECT="$VOICEY_EVAL_PARENT/appointment-evals"
uv run voicey init "$VOICEY_EVAL_PROJECT" \
  --name appointment-evals \
  --recipe appointment-booking \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

Create the Anthropic `tests/voicey-test.jsonc` configuration shown in
`docs/testing.md`. To reproduce the completed text evidence, run:

```bash
(cd "$VOICEY_EVAL_PROJECT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --report json)
```

Then run the real STT→LLM→TTS audio path and retain recordings for manual
review:

```bash
(cd "$VOICEY_EVAL_PROJECT" && \
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --audio --report json)
```

Run the dedicated 20-turn reference-stack latency gate. It reads the production
observer's persisted end-to-end samples, requires 20 distinct measured turns,
and enforces both percentile budgets:

```bash
"$VOICEY_REPO_ROOT/.venv/bin/python" \
  "$VOICEY_REPO_ROOT/tests/verification/p1_latency_gate.py" \
  --project "$VOICEY_EVAL_PROJECT"
```

The suite commands use Pipecat's installed runner and return 1 on any failed
scenario. The first audio run may download Kokoro/Moonshine model data. The
latency wrapper returns 2 when credentials are missing and 1 for a suite,
sample-count, model, or percentile failure. The audio and latency gates remain
pending until their exact commands truly return 0; credentials alone do not
promote either gate.

## P1 Docker public deployment and paid smoke

The canonical image's local build, non-root/read-only start, storage preflight,
ready health contract, SIGTERM drain, zero exit, and fixed high/critical
vulnerability scan are automated. Those checks do not prove a public TLS edge
or a real carrier call.

Build the same unpublished engine wheel and generate artifacts in the target
agent project:

```bash
VOICEY_REPO_ROOT="$PWD"
uv build --wheel --out-dir dist
cd /path/to/agent-project
"$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy docker \
  --engine-wheel \
  "$VOICEY_REPO_ROOT/dist/voicey-0.0.0.dev0-py3-none-any.whl" \
  --skip-smoke
VOICEY_PUBLIC_BASE=https://voice.example.com \
  docker compose -f compose.voicey.yaml up -d --build
curl --fail https://voice.example.com/health
```

After configuring the public reverse proxy and injecting the live provider,
webhook, integrator, and Twilio variables through the protected runtime
environment, point the owned number and place the explicitly confirmed paid
smoke:

```bash
"$VOICEY_REPO_ROOT/.venv/bin/voicey" numbers point +14155550123 \
  --url https://voice.example.com \
  --yes
"$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy docker \
  --smoke https://voice.example.com \
  --to +15551234567 \
  --engine-wheel \
  "$VOICEY_REPO_ROOT/dist/voicey-0.0.0.dev0-py3-none-any.whl" \
  --yes
```

Verify answer latency, greeting, speech in both directions, and acknowledged
results-webhook delivery, then stop the old generation and observe a
`container_drained` log with exit code zero. If the smoke fails, immediately
run the `voicey numbers restore <rollback-token> --yes` command printed by
the cutover. This gate remains pending because no public target is deployed and
the local Docker daemon is stopped. Live Twilio credentials are available, but
credentials alone do not satisfy the paid smoke.

## P3 Vobiz certification on Pipecat and LiveKit

Offline certification covers Vobiz Voice API request shapes, VobizXML,
V3/V2 callback signatures and nonce replay, PCMU/8 kHz serialization, one-use
media admission, provider-authoritative terminalization, route/intent fencing,
recording ingestion, the documented LiveKit UDP topology, deterministic
resource reuse, drift rejection, reverse rollback, and ambiguous writes. Those
offline tests do not prove the current account control plane or a PSTN
conversation.

On 2026-08-03 the live account/owned-number readiness test passed, followed by
the Vobiz↔LiveKit no-call provision → exact reuse → reverse rollback test. The
test safely detached the existing Voice API application, assigned the temporary
SIP route, verified both providers, then restored the exact prior application.
Vobiz and LiveKit API/dashboard inspection showed zero temporary trunks,
dispatch rules, or number bindings after rollback. No PSTN call, recording, or
physical audio was claimed.

Export the Vobiz account, owned-number, public deployment, and paid-call
values:

```bash
export VOBIZ_AUTH_ID='MA_...'
export VOBIZ_AUTH_TOKEN='...'
export VOICEY_VOBIZ_LIVE_FROM='+9180...'
export VOICEY_VOBIZ_LIVE_TO='+91...'
export VOICEY_VOBIZ_TRANSFER_TO='+91...'
export VOICEY_LIVE_PUBLIC_BASE='https://voice.example.com'
export VOICEY_VOBIZ_LIVE_RECORDING_URL='https://provider-recording-url-from-a-verified-callback'
export VOICEY_LIVE_ROUTE_CONFIRM='I_ACKNOWLEDGE_ROUTE_MUTATION'
export VOICEY_LIVE_CONFIRM='I_ACKNOWLEDGE_PSTN_CHARGES'
```

Reproduce the account result, then run the remaining Pipecat/Voice API route
restore, paid AMD/DTMF/recording/cold-transfer, and artifact-ingestion gates:

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
export VOICEY_LIVEKIT_AGENT_NAME='appointment-booking'
export VOICEY_LIVEKIT_SIP_URI='sip:project-id.sip.livekit.cloud'
export VOICEY_VOBIZ_SIP_CREDENTIAL_ID='...'
export VOICEY_VOBIZ_SIP_USERNAME='...'
export VOICEY_VOBIZ_SIP_PASSWORD='...'
export LIVEKIT_SIP_OUTBOUND_TRUNK='ST_...'
export VOICEY_LIVEKIT_CERT_ROOM='voicey-vobiz-cert'
```

Reproduce the completed provision→idempotent reuse→reverse rollback result and
run the remaining paid outbound SIP call:

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
   stopping `voicey dev --phone`.
3. Re-provision the LiveKit route twice; confirm the second operation creates
   zero resources. Call inbound and outbound through Vobiz SIP, verify both
   speech directions and terminal mapping, then roll back and confirm the
   prior number binding is restored.
4. Inspect Vobiz and LiveKit: transport must be UDP/5060, inbound LiveKit
   addresses exactly `13.233.44.61/32`, and no test must claim TLS/SRTP.
5. Retain call ids, result event ids, recording ids, provisioning operation
   ids, timestamps, and the zero-exit test output without retaining secrets or
   raw caller PII.

The account and no-call LiveKit control-plane cases are green. This row stays
pending until the Pipecat route/paid/recording cases, LiveKit paid call, and the
physical checklist genuinely pass. A control-plane pass is not a substitute
for media or PSTN evidence.

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
export VOICEY_PLIVO_LIVE_FROM='+1415...'
export VOICEY_PLIVO_LIVE_TO='+1415...'
export VOICEY_PLIVO_TRANSFER_TO='+1415...'
export VOICEY_LIVE_PUBLIC_BASE='https://voice.example.com'
export VOICEY_PLIVO_LIVE_RECORDING_URL='https://provider-url-from-a-verified-callback'
export VOICEY_LIVE_ROUTE_CONFIRM='I_ACKNOWLEDGE_ROUTE_MUTATION'
export VOICEY_LIVE_CONFIRM='I_ACKNOWLEDGE_PSTN_CHARGES'
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
export VOICEY_LIVEKIT_AGENT_NAME='appointment-booking'
export VOICEY_LIVEKIT_SIP_URI='sip:project-id.sip.livekit.cloud'
export VOICEY_PLIVO_SIP_USERNAME='voiceyuser'
export VOICEY_PLIVO_SIP_PASSWORD='strong-special-value' # pragma: allowlist secret
export LIVEKIT_SIP_OUTBOUND_TRUNK='ST_...'
export VOICEY_LIVEKIT_CERT_ROOM='voicey-plivo-cert'
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
export VOICEY_LIVEKIT_AGENT_NAME='appointment-booking'
export VOICEY_SIP_LIVE_FROM='+1415...'
export VOICEY_SIP_LIVE_TO='+1415...'
export VOICEY_SIP_ADDRESS='trunk.provider.example:5061'
export VOICEY_SIP_USERNAME='voicey'
export VOICEY_SIP_PASSWORD='...'
export VOICEY_SIP_TRANSPORT='tls'
export VOICEY_SIP_MEDIA_ENCRYPTION='require'
export VOICEY_SIP_ALLOWED_ADDRESSES='203.0.113.0/24'
export LIVEKIT_SIP_OUTBOUND_TRUNK='ST_...'
export VOICEY_LIVEKIT_CERT_ROOM='voicey-generic-sip-cert'
export VOICEY_LIVE_ROUTE_CONFIRM='I_ACKNOWLEDGE_ROUTE_MUTATION'
export VOICEY_LIVE_CONFIRM='I_ACKNOWLEDGE_PSTN_CHARGES'
```

Run the guarded LiveKit provision/reuse/rollback and paid loopback:

```bash
uv run pytest -q --no-cov -m live tests/live/test_generic_sip_live.py
```

Operator checklist:

1. Point the external trunk's inbound destination at the LiveKit SIP endpoint
   and configure its reverse route to `VOICEY_SIP_ADDRESS`.
2. Confirm username/password, source CIDRs, signaling transport, and media
   policy match exactly on both systems. Never use TLS with disabled media
   encryption.
3. Call inbound and outbound between physical endpoints; verify two-way audio,
   interruption, DTMF, both hangup directions, one terminal event, and the
   expected caller ID.
4. Run provisioning twice and verify zero new resources on the second pass.
   Roll back and confirm every voicey-created LiveKit resource is removed.
5. Restore the external route manually and retain its audit evidence because
   voicey does not own that control plane.

This row remains pending until the command and every external-route check pass.
It does not turn generic SIP into a Certified carrier.

## P3 first-party recipe provider conversations

All three P3 recipe sources, deterministic integrations, native entrypoints,
and 17 shared scenarios compile locally on both runtimes. On 2026-08-03 six
fresh projects ran every text scenario through the production native runtime
path with Claude Sonnet 5 and native Anthropic judging: restaurant reservations
5+5, front desk 6+6, and lead intake 6+6. All 34 cases passed on their first
attempt. The runs covered typed tools and durable result assertions, LiveKit's
native restaurant waitlist handoff, voicemail privacy, deterministic warm-
transfer selection in the text tier, consented lead capture, corrections, and
failure boundaries. No Ollama request was used. The provider text gate is
green.

The remaining provider-audio/JUnit evidence uses the explicit Anthropic judge
configuration shown in `docs/testing.md`. From the repository root, create each
recipe/runtime pair in a disposable project:

```bash
VOICEY_REPO_ROOT="$PWD"
VOICEY_RECIPE_PARENT="$(mktemp -d)"
for VOICEY_RECIPE in restaurant-reservations front-desk lead-intake; do
  for VOICEY_RUNTIME in pipecat livekit; do
    VOICEY_PROJECT="$VOICEY_RECIPE_PARENT/$VOICEY_RECIPE-$VOICEY_RUNTIME"
    uv run voicey init "$VOICEY_PROJECT" \
      --name "$VOICEY_RECIPE-$VOICEY_RUNTIME" \
      --recipe "$VOICEY_RECIPE" \
      --channels web \
      --runtime "$VOICEY_RUNTIME" \
      --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
      --no-draft-prompts \
      --yes
    (
      cd "$VOICEY_PROJECT"
      "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --audio
      "$VOICEY_REPO_ROOT/.venv/bin/voicey" test --report junit
    )
  done
done
```

Each generated `tests/voicey-test.jsonc` must select the native Anthropic API
override; no remaining command requires Ollama. For `front-desk`, repeat the
live phone conversation after P3 warm-transfer provisioning and verify that the
human hears the private briefing before the caller joins. The audio/JUnit and
physical warm-transfer gates remain pending until their commands and human
observations pass; the already completed model-API text suites are not rerun or
misrepresented as physical media evidence.

## P3.6 Pipecat/Twilio warm-transfer live certification

The local protocol suite is green:

```bash
uv run pytest -q --no-cov \
  tests/certification/test_twilio_warm_transfer.py \
  tests/unit/test_pipecat_runtime.py \
  tests/unit/test_pipecat_host.py
```

The paid two-handset gate is ready-to-run but pending a funded Twilio account,
an owned number, a public Pipecat host, and two people/endpoints. In a generated
`front-desk` Pipecat project with its provider keys in `.env`, run:

```bash
export VOICEY_TRANSFER_NUMBER='+1415...human-destination'
voicey dev --phone --tunnel url \
  --url 'https://public-pipecat.example.com' \
  --no-open
```

Then execute this physical checklist without stopping the host:

1. Call the owned `phone.number` from handset A and ask for the configured
   department.
2. Decline consent once. Confirm `warm_transfer_to_human` is not called and the
   caller remains with the agent.
3. Give explicit consent. Confirm handset B rings while handset A continues to
   hear the agent/media stream.
4. Answer handset B. Confirm only B hears the concise private briefing and the
   instruction to press 1. Before pressing 1, confirm A and B cannot hear each
   other.
5. Let one attempt time out or press a non-1 digit. Confirm B is hung up, A
   remains with the agent, the agent offers to take a message, and the ledger
   row is `declined` or `failed`.
6. Repeat, press 1 on B, and confirm A joins only after acceptance. Verify both
   people have two-way audio, no private briefing is replayed to A, and the
   final call result has `ended_reason=transferred`.
7. Replay the signed accept callback and confirm no second human call or caller
   redirect is created. Send one callback with a changed CallSid and confirm
   HTTP 400 plus a `conflict` ledger row.
8. Kill the host during a third pre-accept attempt, restart the same command,
   and confirm the known orphan B leg is completed without redial. An
   `ambiguous` bridge must remain visible for operator review.
9. Inspect `.voicey/telephony.sqlite3`, application logs, and the terminal
   payload. Retain transfer/call/conference ids and timestamps, but confirm the
   raw private briefing appears in none of those artifacts.

This gate is not green until all nine observations are recorded from real
Twilio callbacks and physical endpoints. The current environment does not
contain the required funded Twilio/public-host evidence.

## P4.3 Railway results companion

The local gate executed Railway CLI 5.30.1, exercised create/resume/adopt/
rotation/reverse-rollback command contracts, checked that secret values appear
only on stdin, generated the two-replica non-root deployment, and passed the
real migration/object/fencing preflight against disposable PostgreSQL 17. This
machine is not authenticated to a billed Railway workspace, so no external
resource is represented as green.

Choose a disposable voicey phone-agent project and empty Railway project
identity. Authenticate, build the unpublished engine wheel, and run:

```bash
export VOICEY_REPO_ROOT="$PWD"
export VOICEY_AGENT_PROJECT='/absolute/path/to/disposable-agent-project'
export VOICEY_ENGINE_WHEEL="$PWD/dist/voicey-0.0.0.dev0-py3-none-any.whl"
export VOICEY_RAILWAY_PROJECT='voicey-results-cert'
export VOICEY_RAILWAY_WORKSPACE='exact-workspace-id-or-name'
export VOICEY_RAILWAY_ENVIRONMENT='production'
export VOICEY_RAILWAY_SERVICE='voicey-results-cert'
export VOICEY_RAILWAY_BUCKET='voicey-results-cert-objects'
export VOICEY_RAILWAY_SERVICE_REGION='us-east'
export VOICEY_RAILWAY_BUCKET_REGION='iad'
railway whoami
uv build --wheel --out-dir dist
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy railway \
    --project "$VOICEY_RAILWAY_PROJECT" \
    --workspace "$VOICEY_RAILWAY_WORKSPACE" \
    --environment "$VOICEY_RAILWAY_ENVIRONMENT" \
    --service "$VOICEY_RAILWAY_SERVICE" \
    --bucket "$VOICEY_RAILWAY_BUCKET" \
    --service-region "$VOICEY_RAILWAY_SERVICE_REGION" \
    --bucket-region "$VOICEY_RAILWAY_BUCKET_REGION" \
    --engine-wheel "$VOICEY_ENGINE_WHEEL" \
    --yes \
    --json
)
```

The JSON must show `resources.preflight_green=true`,
`resources.smoke_green=true`, two matching service replicas in the selected
region, and a smoke row with successful deployment, liveness, migration,
rolling-generation, and signed-readiness facts. Railway pre-deploy logs must
show checksummed migration validation, the private-bucket round trip, generation
1→2, stale-writer rejection, exactly one terminal event, and rollback-only
cleanup before server startup. Confirm the generated domain serves
`/healthz`, authenticated `/v1/ready` succeeds, the bucket is private, and the
owner-only ledger contains no value from the project's `.env`.

Rerun the identical command and verify the exact project, service, Postgres,
bucket, domain, and ids are reused. No second resource may appear. Then rotate
the current/previous relay and results pairs:

```bash
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy railway \
    --project "$VOICEY_RAILWAY_PROJECT" \
    --workspace "$VOICEY_RAILWAY_WORKSPACE" \
    --environment "$VOICEY_RAILWAY_ENVIRONMENT" \
    --service "$VOICEY_RAILWAY_SERVICE" \
    --bucket "$VOICEY_RAILWAY_BUCKET" \
    --service-region "$VOICEY_RAILWAY_SERVICE_REGION" \
    --bucket-region "$VOICEY_RAILWAY_BUCKET_REGION" \
    --rotate-credentials \
    --engine-wheel "$VOICEY_ENGINE_WHEEL" \
    --yes \
    --json
)
```

Deploy a paired Pipecat Cloud worker from the same project and place its paid
smoke through the Railway relay:

```bash
export VOICEY_RELAY_URL='https://exact-generated-domain.up.railway.app'
export VOICEY_PCC_AGENT='voicey-railway-cert'
export VOICEY_PCC_ORG='exact-pipecat-org'
export VOICEY_PCC_REGION='us-west'
export VOICEY_PCC_SECRET_SET='voicey-railway-cert-secrets' # pragma: allowlist secret
export VOICEY_PCC_IMAGE='registry.example.com/voicey/railway-cert:git-sha'
export VOICEY_CLOUD_SMOKE_TO='+14155550199'
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy pipecat-cloud \
    --agent "$VOICEY_PCC_AGENT" \
    --org "$VOICEY_PCC_ORG" \
    --region "$VOICEY_PCC_REGION" \
    --secret-set "$VOICEY_PCC_SECRET_SET" \
    --image "$VOICEY_PCC_IMAGE" \
    --min-agents 1 \
    --max-agents 4 \
    --profile agent-1x \
    --relay-url "$VOICEY_RELAY_URL" \
    --engine-wheel "$VOICEY_ENGINE_WHEEL" \
    --smoke-to "$VOICEY_CLOUD_SMOKE_TO" \
    --yes \
    --json
)
```

Prepare/build/push that immutable image using the P3 Pipecat Cloud
`--prepare-only` command before the paid invocation. Promotion requires the
platform-session begin/terminal pair, the paid-call terminal result, and
acknowledged delivery in Railway Postgres. A LiveKit-runtime project may
instead run the exact P3 LiveKit Cloud command with this Railway relay URL.

For rolling-drain evidence, start a second paid call and keep it active. While
it is active, rerun the Railway deploy command above from another terminal
with a newly built immutable wheel. Confirm the old replica closes readiness,
the already fenced call terminalizes once, a new call is admitted by the
replacement, and the stale generation cannot append or terminalize. Retain
redacted project/service/database/bucket/domain/deployment/replica/call/event/
delivery ids and timestamps.

After evidence capture, delete only this disposable, voicey-created set:

```bash
(
  cd "$VOICEY_AGENT_PROJECT"
  "$VOICEY_REPO_ROOT/.venv/bin/voicey" deploy railway \
    --project "$VOICEY_RAILWAY_PROJECT" \
    --workspace "$VOICEY_RAILWAY_WORKSPACE" \
    --environment "$VOICEY_RAILWAY_ENVIRONMENT" \
    --service "$VOICEY_RAILWAY_SERVICE" \
    --bucket "$VOICEY_RAILWAY_BUCKET" \
    --service-region "$VOICEY_RAILWAY_SERVICE_REGION" \
    --bucket-region "$VOICEY_RAILWAY_BUCKET_REGION" \
    --rollback-created \
    --yes \
    --json
)
```

Verify deletion order is domain, bucket, Postgres service, application
service, then project. Do not run rollback on an adopted or production
resource set.

## P4.1 full soak and live rolling drain

The credential-free chaos, drain, two-backend fencing, and bounded soak gate is
green. The required 24-hour wall-clock duration has not elapsed and is not
represented as green.

Run it on a stable Linux host:

```bash
uv sync --frozen --extra pipecat --extra livekit
uv run python tests/verification/p4_soak.py \
  --duration-s 86400 \
  --max-concurrent 8 \
  --call-hold-s 1 \
  --runtime both \
  --report .voicey/verification/p4-soak-report.json
```

The scheduled workflow `.github/workflows/soak.yml` targets a self-hosted Linux
runner labeled `voicey-soak`. GitHub-hosted jobs have a six-hour execution
limit, while self-hosted jobs may run for five days, so a hosted `ubuntu-latest`
job cannot honestly implement this gate. See the official
[GitHub Actions limits](https://docs.github.com/en/actions/reference/limits).

After the soak, confirm the report says `wall_clock_complete=true`,
`active_at_end=0`, has no failures, reaches peak active 16 (eight calls for each
runtime), and stays within the committed heap/RSS/FD bounds.

Live zero-downtime drain remains pending for every external target. For Docker,
run the P1 Docker smoke procedure above while keeping one call active across
replacement. For Fly, Pipecat Cloud, and LiveKit Cloud, run their P3 deployment
commands above, start a paid smoke call, deploy a new immutable version, and
confirm:

1. the old generation closes readiness before the new route is sent to it;
2. the admitted call finishes on the old generation without media loss;
3. a new call lands on the replacement generation;
4. the stale generation cannot append results or create a second terminal
   event; and
5. both terminal deliveries are acknowledged or visibly dead-lettered.

Run the P4.3 Railway paid-call/redeploy procedure immediately above for its
target row. No target row is green until the authenticated deployment and
active-call replacement actually run.
