# Appointment-booking recipe

`appointment-booking@1.0.0` is the first dual-runtime first-party conversation
design. Select Pipecat for a native `NodeConfig`, or LiveKit for native
Agent-returning handoffs and prebuilt contact-capture tasks.

Create a project:

```bash
voicekit init ./appointments \
  --name appointments \
  --recipe appointment-booking \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

Use `--runtime pipecat` for the other native variant. The command collects and
validates missing provider and runtime keys in-flow. The result contains
ordinary source files: `flow.py` is either a native `pipecat.flows.NodeConfig`
entry or native LiveKit `Agent` classes; `tools.py` contains plain typed Python
functions; and `prompts/` contains the authored conversation policy. There is
no recipe runtime, hidden DSL, or network registry.

## Demo audio

<audio controls src="../assets/recipes/appointment-booking-demo.mp3">
  <a href="../assets/recipes/appointment-booking-demo.mp3">Download the appointment-booking demo</a>.
</audio>

This short illustrative exchange follows the checked-in
[demo transcript](../assets/recipes/demo-transcripts.json). It demonstrates
contact confirmation and final booking confirmation; it is generated system
speech, not evidence for provider quality or the latency gate. Regenerate the
four recipe assets with `uv run python scripts/generate_recipe_demo_audio.py`.

## Native LiveKit workflow

The intake Agent hands off by returning a booking, rescheduling, or cancellation
Agent from a native `@function_tool`. Shared calendar and transfer tools are
carried into every specialist. The booking specialist awaits the installed
LiveKit `GetNameTask` and `GetEmailTask`; rescheduling and cancellation use
`GetEmailTask`. These tasks run only from `on_enter`, as required by the pinned
LiveKit 1.6.7 task contract.

The contact tasks require explicit asking and spoken confirmation. Their output
is reintroduced as untrusted caller data, never prompt instructions. Calendar
mutation rules remain in the shared prompt and tools, so both runtimes preserve
the same confirmation and success contract.

## Production customization map

| File / setting | Owner action |
|---|---|
| `tools.py:TodoCalendarGateway` | Replace the deterministic stub with the calendar API and its authentication/idempotency policy. |
| `prompts/system.md` | Add business hours, timezone, appointment types, and confirmation policy. |
| `prompts/failure.md` | Align retry, callback, and escalation language with support operations. |
| `prompts/voicemail.md` | Add the approved callback number while preserving the no-PII rule. |
| `VOICEKIT_TRANSFER_NUMBER` | Inject an E.164 human destination at runtime; absence removes the native transfer function. |
| `agent.py:Results` | Configure the verified results receiver for the deployment. |

For a local transfer test, inject the non-secret destination for that process:

```bash
VOICEKIT_TRANSFER_NUMBER=+14155550199 voicekit dev --phone
```

Next: make a test call, request a human, and verify the Twilio cold-transfer
event and durable terminal result.

## Conversation contract

The prompt and tools enforce these invariants:

- no appointment success without a successful mutating tool result;
- explicit confirmation before booking, rescheduling, or canceling;
- the latest caller correction supersedes stale details;
- interruptions are accepted and the interrupted script is not repeated;
- voicemail is under 20 seconds and contains no appointment PII;
- calendar errors never become fabricated success;
- human escalation uses the runtime-injected `transfer_to_human` function.

The stub is stateless by design, so no developer can mistake it for a durable
calendar. Its stable `APT-…` reference is for local conversations and tests.

## Verification

See [Pipecat Evals](../testing/pipecat-evals.md) for the text/audio suites,
20-turn reference latency gate, local-Ollama default, cloud override shape,
exit contract, and exact commands.

The native LiveKit factory, three handoffs, tool preservation, pinned contact
tasks, and copied-project import path run in normal CI. The unified cross-runtime
scenario command lands in P2.4.

Next: run the Pipecat text suite after every prompt or tool change and the audio
suite before deployment.
