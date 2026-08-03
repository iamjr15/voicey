# Browser playground

`voicey dev` serves the selected Pipecat or LiveKit engine used by phone
calls and places a protected browser console beside it. The console is a
diagnostic surface, not a second conversation implementation: flows and tools
remain native runtime code.

## Start it

From an initialized project:

```bash
voicey dev
```

The default addresses are:

| Listener | Address | Contents |
|---|---|---|
| Public runtime | `http://127.0.0.1:7860` | Pipecat signaling or LiveKit room-token exchange |
| Local admin | `http://127.0.0.1:7861` | playground, session issuance, call/result/recording reads |
| LiveKit worker health | `http://127.0.0.1:7862` | LiveKit projects only; native worker health |

`--port N` moves the public listener to `N`, admin to `N + 1`, and LiveKit
worker health to `N + 2`. The highest valid value is `65534` for Pipecat and
`65533` for LiveKit. `--no-open` leaves the processes running without opening
a browser.

Phone development preserves the boundary:

```bash
voicey dev --phone --tunnel auto
```

Only the public listener is forwarded. The CLI probes the public WebSocket
edge before changing a carrier route and prints that the admin surface remains
local.

## What the console shows

- streaming user and assistant turns, with final durable transcript taking
  precedence when available;
- per-turn end-to-end badges and STT, LLM, TTS, and end-to-end latency samples;
- connection, state-transition, speaking, and interruption events;
- typed tool arguments, results, duration, and status;
- live `results.set()` data and interruption count;
- the exact immutable terminal-event JSON that webhook delivery signs;
- the active reload revision from the protected repository.

The runtime-specific media bundle loads only after **Start talking**. Provider
API keys never enter the page.

## Browser-session security

Before returning a browser token, the admin listener reserves runtime capacity
and creates the durable call row. It then mints a short-lived token carrying
issuer, audience, agent, session, durable call, nonce, issued-at, and expiry
claims. The page sends this **voicey token** as
`Authorization: Bearer …`; it never appears in a query string or fragment.

For Pipecat, the first authenticated offer consumes the token and binds the
pre-reserved call to the peer; PATCH signaling must match that binding. For
LiveKit, public `POST /api/livekit/token` consumes it and returns a distinct,
short-lived room credential. The pinned official client uses that credential
with LiveKit's native signaling protocol, which may include provider-native
query fields. A failed authenticated exchange consumes the voicey token and
terminalizes the reservation as `call.failed`. Expiry, replay, tampering,
cross-agent/audience use, and peer changes fail with `VY-WEB-001`.

Issuance has per-client, active-session, and global limits. Signaling has a
bounded rate. Origins must be the local admin origin or an explicit
`web.allowed_origins` value. Public host reconstruction accepts forwarded
headers only from configured proxy CIDRs and must equal the configured public
origin.

The public application has no admin session-issuance, call-record, result, or
recording route. Its LiveKit-only room-token exchange accepts only a previously
issued one-use voicey bearer. Local admin requests require the exact listener
Host and reject foreign Origin values. An integrator that exposes the admin
application outside loopback must supply an authentication hook; startup fails
closed otherwise.

Responses use no-store, nosniff, no-referrer, restrictive Permissions Policy,
and a self-hosted Content Security Policy. The Pipecat audio path uses the
installed `WavMediaManager`, so it does not load Daily's remote call-machine
bundle. Blob access is restricted to media and audio worklets.

## Safe reload

The watch controller reports `ready`, `reloading`, `restart_pending`, or
`error` in the UI:

- prompt and agent-configuration changes are validated and applied to the next
  session after the active-call boundary;
- flow and tool Python changes evict project modules and restart the Pipecat
  runner only after all active calls finish;
- runtime, agent name, phone identity, results identity, and enabled-channel
  changes require a full `voicey dev` restart and fail as `VY-WEB-005`.

No active call changes revision mid-session.

## Assets and packaging

The Vite/React source is in `playground-web/`. The application lazy-loads the
pinned Pipecat client/small-WebRTC/voice-ui-kit path or
`livekit-client==2.21.0` from the session's runtime discriminator. Build and
test it directly with:

```bash
cd playground-web
npm ci --ignore-scripts
npm run typecheck
npm test
npm run build
npm audit --audit-level=high
```

The hatch hook runs the locked npm install and build for wheels, then embeds
the output under `voicey/_frontend`. Runtime access uses
`importlib.resources.as_file`, so zipped installations are supported.
`VOICEY_SKIP_FRONTEND_BUILD=1` is accepted only when a complete prebuilt
entrypoint already exists; release builds must not use it to mask a missing
Node.js toolchain.

## Troubleshooting

- `VY-WEB-001`: return to the playground and request a fresh session.
- `VY-WEB-002`: correct the browser origin, public URL, or trusted proxy list.
- `VY-WEB-003`: end an active browser session or wait for the retry interval.
- `VY-WEB-004`: use the loopback admin URL or configure integrator auth.
- `VY-WEB-005`: run `voicey doctor`, rebuild assets, or restart after an
  identity-level configuration change.

After changing configuration, start another session and confirm the displayed
revision before testing the new behavior.
