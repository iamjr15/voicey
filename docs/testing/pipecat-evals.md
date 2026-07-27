# Pipecat Evals

P1 uses Pipecat 1.6.0's native eval transport and CLI directly. The
`voicekit[pipecat]` extra installs `pipecat-ai[evals]`; the copied recipe's
`eval_bot.py` calls the same production session builder used by phone and web
calls. Provider creation, native Flows initialization, typed tools,
observations, admission, and fenced terminal persistence therefore stay in the
test path.

The only substituted side effect is carrier transfer: eval mode records a
transfer request and ends the call without contacting Twilio.

## Text suite

From an appointment recipe project:

```bash
ollama pull gemma2:9b
uv run pipecat eval suite evals/text-suite.yaml
```

Seven fresh-bot scenarios cover booking, change of mind, cancellation,
barge-in, calendar failure, voicemail, and the production
`transfer_to_human` function. Text mode bypasses STT/TTS for fast behavioral
iteration but still uses the real agent LLM and native tools.

## Audio suite

```bash
uv run pipecat eval suite evals/audio-suite.yaml -a
```

Three audio scenarios synthesize the caller locally with Kokoro, stream the
audio through the real Deepgram STT and VAD path, synthesize the agent with the
real configured TTS, then transcribe that output locally with Moonshine.
The first run downloads local speech models. Recordings and logs are written
under `eval-runs/`, which is project-local diagnostic output and must not be
committed when it contains real conversations.

## Judge configuration

`evals/judge-local.yaml` and `evals/judge-audio-local.yaml` select local Ollama
with `gemma2:9b`; this is the default and requires no judge API key. A
`judge-cloud.example.yaml` documents the Pipecat-supported cloud shape. Keep a
private scenario overlay that includes the cloud file and collect its provider
credential with:

```bash
voicekit keys add openai
```

Never put a credential in YAML or commit it.

## Exit and CI contract

`pipecat eval suite` returns 0 only when every scenario passes and 1 when a
scenario, manifest, spawn, or assertion fails. CI validates the installed
command's 0/1 contract plus every scenario/manifest against the installed 1.6.0
parser. Credentialed recipe suites are release gates; they are not replaced by
mock success.

Run one scenario while iterating:

```bash
uv run eval_bot.py -t eval
uv run pipecat eval run evals/text/book.yaml --verbose --stop-bot
```

Next: inspect the generated eval log on failure, fix the native prompt/tool
source, and rerun the same command.
