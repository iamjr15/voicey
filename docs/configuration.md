# Agent configuration

`agent.py` is the canonical, typed definition of a voice agent. It selects a
runtime and providers, but it does not describe conversation states:
`flow="flow:entry"` points directly to native Pipecat Flows or a native LiveKit
agent workflow.

## Complete example

```python
from voicekit import Agent, Behavior, Limits, Models, Phone, Results, Voice, Web

agent = Agent(
    name="clinic-front-desk",
    runtime="pipecat",
    models=Models(
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
        fallbacks={"tts": "elevenlabs/flash-2.5"},
    ),
    voice=Voice(language="en", speed=1.0),
    persona="Warm, brisk, professional. Company: Sunrise Dental.",
    flow="flow:entry",
    tools="tools",
    phone=Phone(
        provider="twilio",
        number="+14155550123",
        inbound=True,
        outbound=True,
        record=True,
    ),
    web=Web(
        enabled=True,
        allowed_origins=["https://sunrisedental.example"],
    ),
    results=Results(
        webhook="https://api.sunrisedental.example/voice-results",
        secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        previous_secret_env=None,
        redact=["phone_number"],
        purge_after_days=30,
    ),
    limits=Limits(
        max_duration_s=600,
        max_concurrent=20,
        silence_hangup_s=30,
    ),
    behavior=Behavior(
        allow_interruptions=True,
        voicemail="hangup",
        dtmf=True,
        transfer_number=None,
    ),
)
```

`tools` accepts either an importable module or an explicit list of importable,
module-level callables. Conversation functions remain ordinary typed Python
functions; secret values are never configuration fields.

## Validation

Pydantic performs structural validation when `Agent` is imported. The engine
then calls `validate_agent_config(agent, environ=...)` at `dev` and `deploy` to
check the selected provider catalog:

- every model and carrier exists and supports the selected runtime;
- the STT, LLM, and TTS support the primary and fallback languages;
- every required provider and webhook-secret environment variable exists;
- current and previous result secrets use Standard Webhooks `whsec_` encoding.

Validation collects independent failures in one pass. Every issue has a stable
`VK-CFG-*` code, an exact field path, and a direct fix. Carrier account
ownership is deliberately a live validation stage and is implemented by the
carrier adapter and `doctor`, rather than guessed from local configuration.

## Provider catalog

`voicekit.config.DEFAULT_PROVIDER_CATALOG` is the shared source for validation,
wizard choices, and key checks. Each entry records:

- stable `provider/model` id and STT, LLM, TTS, or carrier kind;
- runtime and language support;
- factual price and latency classes;
- required environment-variable names and authenticated validation endpoint;
- whether the carrier has a native outbound idempotency key.

The catalog contains no credentials. Wizard choice ordering is deterministic
and does not imply or preselect a recommendation.

## Configuration hash

`agent.config_hash` is:

```text
sha256(json.dumps(agent.model_dump(mode="json"),
                  sort_keys=True,
                  separators=(",", ":")))
```

The value is stable across dictionary insertion order and changes when any
serialized setting changes. Runtime bootstraps stamp it into call records and
result events so operators can identify exactly what was live for a call.

## `voicekit.jsonc`

The project manifest is an engine-owned, resumable record of wizard choices. It
contains the project, runtime, recipe version, channels, providers, deploy
target, and completed steps. For phone projects it also stores exactly one
carrier and its selected E.164 number so interrupted routing and calling
commands can resume. It never contains secret values.

`ManifestStore` accepts JSON5 comments and trailing commas. Saves use a
same-directory temporary file, flush file contents, atomically replace the
manifest, and fsync the parent directory on POSIX. The resulting manifest is
world-readable configuration (`0644`); protected credential and record files
use the stricter permissions documented in [Security](../SECURITY.md).

```python
from pathlib import Path

from voicekit.config import ManifestStore

manifest = ManifestStore(Path("voicekit.jsonc")).load()
```

Next step after changing configuration: run `voicekit doctor` before starting
or deploying the agent.
