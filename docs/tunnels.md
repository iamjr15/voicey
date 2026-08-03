# Development tunnels

Voicey tunnels only the public runtime listener used by carrier callbacks,
carrier media, and web signaling. The admin listener introduced with the P1.9
playground is separate and is never a tunnel target.

## Resolution order

`TunnelManager` implements the locked automatic order:

1. when `NGROK_AUTHTOKEN` is present, use the pinned `ngrok==1.4.0` Python SDK;
2. otherwise, start an installed `cloudflared` quick tunnel;
3. use a caller-supplied HTTPS origin only when `url` is selected explicitly.

An explicit provider never silently falls through to another provider. Missing
ngrok credentials, a missing SDK, or a missing cloudflared executable is a
cataloged error with the exact fix.

Install the optional ngrok SDK with:

```bash
uv sync --extra tunnel
```

Cloudflared remains an external executable. Automatic quick tunnels require no
Cloudflare account. They are development endpoints with no SLA and must pass
the WebSocket probe before voicey points a real carrier number.

## Runtime integration

```python
from fastapi import FastAPI

from voicey.tunnel import TunnelManager, TunnelProbe

app = FastAPI()
probe = TunnelProbe()
probe.install(app)

# Start the local public listener on 127.0.0.1:7860 first.
handle = await TunnelManager().open(7860)
try:
    await probe.verify(handle.public_url)
    print(handle.public_url)
finally:
    await handle.close()
```

The probe route accepts one random, process-local challenge and echoes it only
when it matches exactly. It is not a general-purpose public echo server. A
successful check proves DNS, TLS, HTTP Upgrade, bidirectional WebSocket frames,
and the local public listener in one round trip.

`TunnelProbe.verify()` retries transient DNS and connection readiness within
one bounded deadline. A wrong challenge fails immediately. Failed verification
returns `VY-TUN-004`; callers must not mutate carrier routing afterward.

## Cloudflared process safety

Cloudflared starts with argument-vector execution, never a shell:

```text
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:7860
```

`cloudflared_protocol="http2"` adds `--protocol http2` for networks where the
default transport is blocked. Voicey accepts only a generated
`https://*.trycloudflare.com` origin from the process logs. It drains both log
pipes for the tunnel's full lifetime, bounds and redacts startup diagnostics,
and shuts down with:

```text
terminate → wait up to 10 seconds → kill → wait
```

The child process is directly supervised, so Ctrl-C and normal FastAPI lifespan
shutdown do not leave a shell or tunnel grandchild behind.

## Ngrok lifecycle

The token is passed directly to `ngrok.forward()` and never appears in a
subprocess argument, URL, diagnostic, or call record. The public origin comes
from `listener.url()` and is validated as an HTTPS origin. Shutdown awaits
`listener.close()` exactly once. Invalid listener output is closed before the
error is returned.

## Manual URL

An existing tunnel or reverse proxy can be adopted without giving voicey its
lifecycle:

```python
handle = await TunnelManager().open(
    7860,
    preference="url",
    public_url="https://voice.example.com",
)
```

The value must be an HTTPS origin with no credentials, non-default port, path,
query, or fragment. Voicey still requires the authenticated WebSocket probe;
closing a manual handle does not stop the operator-owned proxy.

## Verification

Local provider selection, current ngrok calls, exact cloudflared arguments,
URL parsing, diagnostic redaction, pipe draining, terminate-to-kill escalation,
FastAPI challenge enforcement, and a real local WebSocket round trip:

```bash
uv run pytest --no-cov tests/unit/test_tunnel.py
```

Public cloudflared edge verification:

```bash
VOICEY_LIVE_TUNNEL_CONFIRM=I_ACKNOWLEDGE_PUBLIC_TUNNEL \
  uv run pytest -m live --no-cov \
  tests/live/test_tunnel_live.py::test_cloudflared_quick_tunnel_websocket_round_trip
```

The public command is currently pending in this workspace: cloudflared
establishes a connector and emits a URL, but the generated hostname did not
resolve within 60 seconds on 2026-07-27. This is recorded in
[`GAPS.md`](GAPS.md), not represented as a green edge test.

`voicey dev --phone` now owns this lifecycle: it probes the tunnel before
changing a route and restores both route and tunnel during teardown.
