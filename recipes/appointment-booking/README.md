# Appointment booking

A production conversation design for booking, rescheduling, and canceling
appointments on both runtimes. The Pipecat variant is native
`pipecat.flows` source. The LiveKit variant is a native intake `Agent` that
hands off to booking, rescheduling, and cancellation `Agent` specialists; it
uses LiveKit's pinned `GetNameTask` and `GetEmailTask` for confirmed contact
capture. Both variants consume the plain typed functions in `tools.py`.

## Customize before production

1. Replace `TodoCalendarGateway` in `tools.py` with your calendar API. Keep the
   typed tool functions and their return shapes stable, or update the evals with
   the contract change.
2. Inject an E.164 human destination as `VOICEY_TRANSFER_NUMBER` in the
   runtime environment. Each runtime exposes its native transfer function only
   when that destination exists.
3. Replace the placeholder results receiver through your deployment
   configuration.
4. Review `prompts/system.md`, `prompts/failure.md`, and
   `prompts/voicemail.md` for business hours, timezone, consent, and escalation
   policy.

The bundled stub is deterministic and stateless. It is useful for a first
conversation and CI, but it does not reserve a real calendar resource.

## Run

```bash
voicey doctor
voicey dev
```

Next: open the printed local playground URL and try booking, changing, then
canceling an appointment.

## Runtime-native workflow

`flow.py` is selected when the recipe is copied:

- Pipecat receives one native `NodeConfig` entry and Pipecat Flows functions.
- LiveKit receives native `Agent` handoff tools. The booking specialist awaits
  `GetNameTask` and `GetEmailTask`; reschedule and cancellation await
  `GetEmailTask`. Confirmed values remain caller data, not instructions.

The specialists preserve the engine-injected calendar and transfer tools across
every handoff. `return_to_intake` is a native Agent-returning function tool, not
a recipe state machine.

## Pipecat Evals

`tests/scenarios.py` is the canonical seven-case suite used by both runtimes.
`voicey test` compiles it to Pipecat Evals YAML or native LiveKit
`AgentSession.run()` assertions according to `voicey.jsonc`. Local Ollama is
the default sim caller and cited judge:

```bash
ollama pull qwen3:8b
voicey test
voicey test --audio
voicey test --report junit
```

Pipecat compilation spawns a generated `-t eval` bot through the production
session builder. A tool-only Pipecat turn waits for both the native function
event and its post-tool response before the next caller input. LiveKit text
tests use its native expect/judge API; its audio
tier attaches a virtual PCM microphone and speaker so Kokoro caller speech
travels through the real STT→LLM→TTS path and Moonshine judges the spoken
output. An initial failure is rerun three times and remains failed while its
stability percentage is reported.

The older `evals/` manifests remain useful as direct Pipecat-framework fixtures
and regression examples:

```bash
uv run pipecat eval suite evals/text-suite.yaml
uv run pipecat eval suite evals/audio-suite.yaml -a
```

The P1 reference-stack latency gate uses a dedicated 20-turn manifest and the
production observer's persisted end-to-end samples:

```bash
uv run python /path/to/voicey/tests/verification/p1_latency_gate.py \
  --project "$PWD"
```

It fails unless p50 is at most 800 ms and p95 is at most 1500 ms. Provider
credentials must be injected into the process; the script never reads or logs
secret values from scenario YAML.

For a cloud judge or sim caller, create the secret-free
`tests/voicey-test.jsonc` config documented in `docs/testing.md` and inject
the referenced key with `voicey keys add anthropic` or `voicey keys add openai`.
Native Anthropic and OpenAI-compatible overrides are supported; do not put a
key in the config.

The 2026-08-03 reference text certification used the native Anthropic override
only—no Ollama—and regenerated projects for both runtimes. All seven cases
passed on their first attempt through Pipecat Evals and LiveKit's native test
API with production typed tools and durable result assertions. This does not
replace the still-pending audio, PSTN, or physical-input gates.

Next: run the text suite on every prompt/tool change and the audio suite before
shipping.

## Covered behavior

- book, reschedule, and cancel
- change-of-mind corrections before and after confirmation
- barge-in while the agent is responding
- concise, privacy-safe voicemail behavior
- safe calendar failure recovery
- human escalation through the production cold-transfer function

The eval manifests and scenario YAML are the executable quality checklist.
