# Appointment-booking recipe

`appointment-booking@1.0.0` is the first certified first-party conversation
design. P1 ships its Pipecat variant; the native LiveKit variant lands in P2
before the launch recipe is considered dual-runtime complete.

Create a project:

```bash
voicekit init ./appointments \
  --name appointments \
  --recipe appointment-booking \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

The command still collects and validates missing provider keys in-flow. The
result contains ordinary source files: `flow.py` is a native
`pipecat.flows.NodeConfig`, `tools.py` contains plain typed Python functions,
and `prompts/` contains the authored conversation policy. There is no recipe
runtime, hidden DSL, or network registry.

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

See [Pipecat Evals](../testing/pipecat-evals.md) for the text/audio suite,
local-Ollama default, cloud override shape, exit contract, and exact commands.

Next: run the text suite after every prompt or tool change and the audio suite
before deployment.
