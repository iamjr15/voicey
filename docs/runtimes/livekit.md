# LiveKit runtime

Voicekit’s LiveKit runtime is a thin production host around native
`livekit-agents==1.6.7` workflows. Conversation code remains a LiveKit
`Agent`, `@function_tool` methods, handoffs, and native tasks; voicekit does not
define a flow language.

## Install

```bash
uv sync --extra livekit
```

The resolved runtime is `livekit-agents==1.6.7` with
`livekit-api==1.2.0`. Configure `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and
`LIVEKIT_API_SECRET`, along with credentials for the selected STT, LLM, and TTS
providers.

An agent points `flow` at either a native `Agent` object or a zero/one-argument
factory. A one-argument factory receives voicekit’s native LiveKit tool list:

```python
from livekit.agents import Agent


def entrypoint(tools):
    return Agent(
        instructions="Help the caller complete the task accurately.",
        tools=tools,
    )
```

Factories with any other signature and objects that are not native
`livekit.agents.Agent` instances fail with `VK-RUN-003`.

## Runtime contract

`LiveKitHost` registers an `AgentServer` `rtc_session` handler using the
configured agent name. It reserves capacity before a browser dispatch token is
issued or a SIP job is accepted, then creates the durable call and fenced lease
before starting `AgentSession`.

The runtime maps canonical config to current LiveKit mechanisms:

- STT, LLM, and TTS fallbacks use native `FallbackAdapter` classes.
- Turn detection uses the current inference `TurnDetector("v1-mini")`; the
  deprecated plugin package is not installed.
- Interruption and endpointing use the consolidated `TurnHandlingOptions`.
- Silence, duration, end phrases, language fallback, DTMF, recording, and
  transfer policies are enforced in the session adapter.
- Shared `@tool` functions become native `@function_tool` objects. A mutating
  tool calls `RunContext.disallow_interruptions()` after it starts; filler
  speech uses `RunContext.with_filler()`.

Transcript items, tool events, metrics, state changes, and errors are persisted
incrementally while the call is active. `JobContext.make_session_report()` is
recorded only as a final supplement. A heartbeat renews the call lease, and the
shared fenced lifecycle creates exactly one transactional terminal event and
delivery row.

For Twilio SIP recording, LiveKit exposes the carrier CA Call SID as
`participant.attributes["sip.twilio.callSid"]`. Elastic SIP automatic recording
has no status-callback field, so the host performs bounded post-call
reconciliation against Twilio Core Recordings, requires a completed `Trunking`
source, downloads through the existing authenticated media path, and emits
`call.recording.ready`. Missing/late correlation remains visibly pending.

## Browser tokens

`LiveKitTokenIssuer` mints a short-lived, least-privilege room token with an
explicit `RoomAgentDispatch`. It grants join, publish, subscribe, and data
publish for one room and does not grant room administration or metadata
mutation. The call capacity reservation must exist before returning the token.

The shared visual playground uses this token path in P2.2.

## SIP behavior

Phone jobs are recognized from the native SIP participant kind. The runtime
maps `sip_dtmf_received` events into the durable timeline and exposes the native
beta DTMF-send tool when `behavior.dtmf` is enabled.

Cold transfer uses `JobContext.transfer_sip_participant()` and SIP REFER. Warm
transfer uses LiveKit’s prebuilt `WarmTransferTask`; both use the configured
`behavior.transfer_number`. Native close/error events map to the shared terminal
reason vocabulary.

Twilio provisioning, TLS/auth boundaries, recording, rollback, and live
certification commands are documented in
[`docs/carriers/twilio.md`](../carriers/twilio.md).

## Crash recovery

The integration test starts a real child process, begins a LiveKit-stamped
fenced call, persists a native conversation event, and sends it `SIGKILL`.
Two recovery sweepers then prove one terminal event and one delivery while
retaining the pre-crash transcript:

```bash
uv run pytest --no-cov \
  tests/integration/test_livekit_sigkill.py
```

## Local verification

```bash
uv run pytest --no-cov \
  tests/unit/test_livekit_runtime.py \
  tests/integration/test_livekit_sigkill.py \
  tests/certification/test_twilio_livekit_sip.py
```

Credentialed Twilio–LiveKit commands are listed in
[`docs/GAPS.md`](../GAPS.md). They remain pending until their exact guarded
commands run against funded accounts and real PSTN endpoints.

Next step: configure a LiveKit project and run the P2.2 browser path through
`voicekit dev`.
