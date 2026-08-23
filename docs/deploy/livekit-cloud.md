# LiveKit Cloud deployment

LiveKit Cloud runs the native agent worker and managed SIP path. Durable call
state and result delivery remain on the user-owned results companion. The
worker performs signed relay readiness before registering for jobs and requires
an acknowledged durable call reservation before admitting each job.

The implementation is verified against `lk==2.16.2`,
`livekit-agents==1.6.7`, and `livekit-api==1.2.0`. It uses the installed
`AgentServer`, named agent dispatch, and SIP participant APIs.

## Prerequisites

1. Deploy and verify a [Fly results companion](fly-companion.md), or an
   equivalent validated relay.
2. Authenticate the LiveKit CLI with `lk cloud auth` and confirm the exact
   project appears in `lk project list --json`.
3. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, model
   credentials, and `VOICEY_RELAY_CREDENTIAL` in the agent project's
   owner-only `.env`.
4. Choose the exact project, region, and registered agent name. Voicey has no
   recommended default.
5. For a phone smoke, provision the carrier/LiveKit trunks first and set
   `VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID`.

For an unpublished checkout, build the engine wheel with
`uv build --out-dir dist`.

## Deploy and verify

Run from a LiveKit-runtime agent project:

```bash
voicey deploy livekit-cloud \
  --agent my-agent \
  --project my-livekit-project \
  --region us-west \
  --relay-url https://my-agent-results.fly.dev \
  --engine-wheel /absolute/path/to/voicey-0.0.0.dev0-py3-none-any.whl \
  --yes
```

Published releases omit `--engine-wheel`. Voicey generates a secret-free,
multi-stage glibc/non-root context, validates signed relay readiness, verifies
the exact authenticated project, and refuses unledgered LiveKit agent state.
The installed `lk==2.16.2` returns capitalized project fields (`Name`,
`ProjectId`, `URL`, `APIKey`, and `APISecret`); project identity parsing accepts
that observed shape while retaining the older lowercase compatibility shape.
The generated context also includes a standard root `requirements.txt` marker
with the certified `livekit-agents==1.6.7` pin because this CLI resolves the
agent language and requires an explicit LiveKit dependency before it invokes
the supplied Dockerfile.
It sends worker-only secrets through a temporary `0600` file that is removed
as soon as the CLI returns.
LiveKit project credentials injected into the local command remain available
to the room-smoke client but are not copied into that worker secret file; the
Cloud agent runtime receives its project credentials from LiveKit itself.

On first deploy it calls `lk agent create`; subsequent deployments use
`lk agent deploy` and checkpoint the previous current version. Ready status is
required before smoke. For `lk==2.16.2`, Voicey parses the exact `Status`
column in the Unicode agent-status table; only a ready terminal value such as
`Running` passes, while `CrashLoop`, `Deploying`, and unknown values fail
closed. Because the current CLI returns from `agent deploy` while that status
can still be `Building`, Voicey polls it for up to ten minutes. If a run ends
after deployment but before readiness, rerunning the exact same artifact
resumes the ledgered readiness and smoke steps without deploying a duplicate
version. The web smoke creates a real room with named agent
dispatch, waits for durable `runtime.admitted`, deletes the room, and waits for
the terminal relay event. A successful room smoke does not replace a real
browser conversation as media evidence.

A project whose manifest includes `phone` must provide a paid destination
unless the operator explicitly uses `--skip-smoke`:

```bash
voicey deploy livekit-cloud \
  --agent my-agent \
  --project my-livekit-project \
  --region us-west \
  --relay-url https://my-agent-results.fly.dev \
  --smoke-to +15551234567 \
  --engine-wheel /absolute/path/to/voicey-0.0.0.dev0-py3-none-any.whl \
  --yes
```

That path creates an isolated named-dispatch room, calls the destination with
the exact outbound trunk and `wait_until_answered=True`, then requires durable
admission and terminal persistence before deleting the room. It is a real paid
SIP call. Voicey does not create or silently select a trunk in this command.

## Adoption, resume, and rollback

The owner-only nonsecret ledger is
`.voicey/deploy/livekit-cloud-resources.json`. It stores the exact project,
region, agent id, created/adopted ownership, previous version, relay
fingerprint, artifact digest, and smoke status. It contains no secret values.

To adopt an exact existing agent, supply both its id and explicit permission:

```bash
voicey deploy livekit-cloud \
  --agent my-agent \
  --project my-livekit-project \
  --region us-west \
  --relay-url https://my-agent-results.fly.dev \
  --agent-id agent_123456 \
  --adopt \
  --skip-smoke \
  --yes
```

An existing `livekit.toml` without matching ledger/adoption evidence stops the
run. Account, project, region, relay credential, or agent-id drift also fails
closed.

Rollback is explicit:

```bash
voicey deploy livekit-cloud \
  --agent my-agent \
  --project my-livekit-project \
  --region us-west \
  --relay-url https://my-agent-results.fly.dev \
  --rollback \
  --yes
```

A first version created by voicey is deleted. A later deployment rolls back
to the ledgered previous version. An adopted agent without a ledgered previous
version is left untouched. Do not combine rollback with adoption, wheel, or
smoke flags.
