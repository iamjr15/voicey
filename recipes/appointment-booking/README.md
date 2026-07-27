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
2. Inject an E.164 human destination as `VOICEKIT_TRANSFER_NUMBER` in the
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
voicekit doctor
voicekit dev
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

The suite uses Pipecat's installed harness directly. Local Ollama is the default
judge; install Ollama and pull the documented model once:

```bash
ollama pull gemma2:9b
uv run pipecat eval suite evals/text-suite.yaml
uv run pipecat eval suite evals/audio-suite.yaml -a
```

The suite command spawns `eval_bot.py -t eval`, allocates one fresh bot per
scenario, and exits nonzero when any expectation fails. Audio scenarios use
local Kokoro for the simulated caller and Moonshine to transcribe the agent's
real synthesized audio.

The P1 reference-stack latency gate uses a dedicated 20-turn manifest and the
production observer's persisted end-to-end samples:

```bash
uv run python /path/to/voicekit/tests/verification/p1_latency_gate.py \
  --project "$PWD"
```

It fails unless p50 is at most 800 ms and p95 is at most 1500 ms. Provider
credentials must be injected into the process; the script never reads or logs
secret values from scenario YAML.

For a cloud judge, copy `evals/judge-cloud.example.yaml` to a private config
location, choose it in a private scenario overlay, and inject that provider key
with `voicekit keys add openai`. Do not commit the key.

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
