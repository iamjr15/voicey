# Docker deployment

Docker is voicekit's canonical self-hosted target. It runs one Pipecat agent
generation with a local SQLite WAL database and protected local artifacts on a
Docker-managed host-local volume. The generated production process owns
SIGTERM, admission drain, result delivery, and both web listeners.

## Generate the artifacts

From the agent project:

```bash
voicekit deploy docker --skip-smoke
```

An unpublished development checkout must first build and pass its wheel
explicitly:

```bash
uv build --wheel --out-dir dist
cd /path/to/agent
voicekit deploy docker \
  --engine-wheel /path/to/voicekit/dist/voicekit-0.0.0.dev0-py3-none-any.whl \
  --skip-smoke
```

Generation is idempotent for byte-identical files and fails with `VK-DEP-001`
instead of overwriting a user-owned conflict. It emits:

- `Dockerfile.voicekit`: multi-stage Python 3.14 glibc image, non-root UID/GID
  10001, build-time NLTK data, and a process healthcheck;
- `compose.voicekit.yaml`: hardened service, local named volume, public port
  7860, internal-only admin port 7861, and bounded graceful stop;
- `.dockerignore`: excludes VCS state, build state, and every `.env*` file;
- `docker.env.example`: variable names and safe topology examples only;
- `.voicekit/deploy/project-requirements.txt`: project dependencies other than
  voicekit;
- for unpublished builds only, a mode-0600 local engine wheel under
  `.voicekit/deploy/`.

The wheel and requirements are build inputs, then removed from the runtime
image. Provider, carrier, webhook, and integrator secrets are never Docker
build arguments or image layers.

## Configure and start

The Compose service reads the agent project's gitignored `.env`, which the
guided voicekit setup writes. Supply deployment-only values in the process
environment or the same protected file:

```bash
export VOICEKIT_PUBLIC_BASE=https://voice.example.com
docker compose -f compose.voicekit.yaml up -d --build
docker compose -f compose.voicekit.yaml ps
curl --fail https://voice.example.com/health
```

`VOICEKIT_PUBLIC_BASE` must be the normalized public HTTPS base. Terminate TLS
at a reverse proxy or load balancer and forward only the public listener on
container port 7860. Never publish port 7861. The internal listener mints
browser session tokens and serves protected call data; it requires
`VOICEKIT_INTEGRATOR_SECRET` whenever web is enabled.

Set `VOICEKIT_TRUSTED_PROXY_IPS` to the exact reverse-proxy peer addresses used
for Twilio signature reconstruction. Set `VOICEKIT_TRUSTED_PROXY_CIDRS` to only
the proxy networks permitted to supply browser forwarded headers. Do not use
open-ended public CIDRs.

The default `VOICEKIT_STOP_GRACE_PERIOD=14460s` safely exceeds voicekit's
maximum allowed `limits.max_duration_s` by 60 seconds. If it is reduced, keep
it at least `limits.max_duration_s + 60s`.

## Persistence contract

The supported Docker matrix is deliberately narrow:

| Setting | Required value |
|---|---|
| deploy target | `docker` |
| repository | SQLite |
| SQLite placement | one host-local volume |
| steady replicas | one |
| journal/durability | WAL / `synchronous=FULL` |
| artifacts | protected local filesystem on the same volume |

Startup fails before admission if the data directory is a symlink, the
filesystem is known to be remote or distributed, schema/durability validation
fails, or an artifact byte round trip fails. NFS, CIFS/SMB, Ceph, Gluster and
similar volumes are unsupported. Cross-host or steady multi-replica SQLite is
unsupported; use the managed-Postgres targets introduced later in the build
plan for that topology.

Back up the stopped `voicekit-data` volume as one unit, including
`calls.sqlite3`, its WAL files, `telephony.sqlite3`, artifacts, and recordings.
Do not copy a live SQLite main file without its WAL or a SQLite-aware backup.

## Drain and same-host replacement

On SIGTERM the production supervisor:

1. marks `/health` unready and rejects new reservations;
2. waits for active and pending calls, bounded by `limits.max_duration_s`;
3. terminalizes any remaining sessions at the bound;
4. performs a final due result-delivery pass;
5. stops both listeners and exits zero.

For a same-host replacement, build the new image, keep the same local volume,
start the new generation without routing new traffic to it, wait for its
storage-ready health response, move ingress to it, then SIGTERM the old
generation. Overlap is for a controlled handover only: both generations use
lease generations and fencing, but the steady topology remains one replica.
The deployment preflight proves that a stale old-generation writer is rejected
and that both generations observe the same single terminal event.

## Telephony cutover and smoke

After public HTTPS ingress is healthy, explicitly point the owned number:

```bash
voicekit numbers point +14155550123 \
  --url https://voice.example.com \
  --yes
```

Then place one real paid smoke call:

```bash
voicekit deploy docker \
  --smoke https://voice.example.com \
  --to +15551234567 \
  --yes
```

The smoke command first verifies the public `/health` contract, including
runtime, storage readiness, and open admission, then places the call through
the configured Twilio adapter. `--to` may instead be provided through
`VOICEKIT_SMOKE_TO`. The command does not point the number automatically, and
the call can incur carrier charges. An unpublished checkout must also pass the
same `--engine-wheel` used during generation; the generated next step includes
its safe local copy automatically.

A web-only deployment cannot prove speech with an endpoint probe. Complete one
real browser conversation through an allowed origin and verify the resulting
durable call record and terminal event.

If a post-cutover check fails, restore the route with the rollback token printed
by `numbers point`:

```bash
voicekit numbers restore <rollback-token> --yes
```

## Operational checks

```bash
docker compose -f compose.voicekit.yaml logs --tail 200 agent
docker compose -f compose.voicekit.yaml exec agent \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:7860/health').read().decode())"
docker compose -f compose.voicekit.yaml stop
```

The release security workflow builds this exact image, starts it read-only and
non-root, exercises health plus SIGTERM drain, and fails on fixed high or
critical vulnerabilities. See the [error catalog](../errors.md) for
`VK-DEP-*` recovery.
