# Simulated-caller testing

`voicey test` runs one owned scenario suite through the native evaluator for
the runtime selected in `voicey.jsonc`. It does not replace native flow code:
Pipecat projects compile to Pipecat Evals 1.6.0 YAML, while LiveKit projects run
LiveKit Agents 1.6.7 `AgentSession.run()` and `RunResult.expect` assertions.

## Scenario source

Put scenarios in `tests/scenarios.py` or `tests/scenarios/*.py`. Discovery
imports only those locations, calls each zero-argument `@scenario` function
once, rejects duplicate names, and validates the returned value.

```python
from voicey.testing import scenario


@scenario
def changes_mind():
    return dict(
        caller=(
            "Busy parent, slightly distracted. Books for Tuesday 7pm, "
            "then switches to 8pm."
        ),
        goals=["end with a confirmed booking"],
        expect=dict(
            outcome="booked",
            data={"slot": lambda slot: slot.endswith("20:00")},
        ),
        judge=["agent confirmed the FINAL time, not the first one"],
        max_turns=24,
    )
```

This persona-only form asks the configured sim-caller model to create a
deterministic caller-turn plan before native compilation. For fully repeatable
CI cases, provide `turns=(ScenarioTurn(...), ...)`. `TestProfile` expands the
same behavior over mock identities, and `{field}` placeholders in caller turns
resolve from the profile identity. Profiles must contain fake data.
`ScenarioTurn.runtimes` defaults to both engines. Narrow it only when a pinned
native workflow requires an extra caller interaction; filtering occurs before
native compilation and reported turn counts. Scenario goals and hard business
outcomes cannot be runtime-gated.

`expect.outcome`, nested `expect.data` values or predicates, `max_turns`, and
the wall-clock budget are hard assertions. `TurnExpectation` can assert native
tool calls, response content, handoffs, response-time budgets, and goal-based
judge criteria. Scenario-level judge decisions must include valid transcript
line citations; a model that says “pass” without a citation fails.
For tool-using LiveKit turns, response-content and goal assertions target the
last assistant message recorded by the native run, after any tool output.
For Pipecat, a tool-only expectation compiles to the native function-call event
followed by an unjudged response event. The second event waits for the
post-tool reply before the evaluator sends the next caller turn, preventing a
native function result from being interrupted by test-driver pacing.
`send_after(event="llm_started")` uses LiveKit's native agent-state event and
forced interruption before the marked caller turn is submitted. A missing
event or interrupt failure is preserved as failed attempt evidence.
Reports and cited judges receive interleaved caller and assistant lines. Caller
inputs come from the compiled mock-profile turns; assistant output remains the
installed runtime's captured native events.

## Commands and exit contract

```bash
voicey test
voicey test --filter change_mind
voicey test --audio
voicey test --live
voicey test --report junit
voicey test --report json
```

- Exit `0`: every selected case passed on its first attempt.
- Exit `1`: invalid setup/runtime prerequisites or any failed case.
- An initial scenario failure is rerun three times. All four attempts remain in
  the report, the scenario remains failed, and stability is shown as the
  percentage of successful attempts.
- JUnit is written to `.voicey/test-results.xml`.
- Generated native inputs, logs, and durable eval results remain under
  `.voicey/test-runs/<run-id>/`, which the scaffold ignores. The immutable run
  id prevents a later command from reopening an earlier attempt's SQLite call
  ids.
- Every output ends with a next command. JSON includes `next_step`.

`--live` is a real, paid PSTN tier. It never substitutes text or local audio.
The command validates its exact acknowledgement, credentials, target, and
worst-case four-attempt call budget before running even a persona planner.

## Text and audio tiers

Pipecat text and audio use its installed EvalSuite. Audio uses local Kokoro for
caller speech, passes PCM through the production Eval transport, and uses local
Moonshine to transcribe actual bot audio.

LiveKit text uses `AgentSession.run()` with the production LLM, native Agent
workflow, and the shared typed-tool wrappers. LiveKit audio attaches a virtual
microphone and speaker to the installed `AgentSession`: Kokoro PCM is paced in
real time through the production STT→LLM→TTS services, agent PCM is captured,
and Moonshine transcribes it for assertions. Audio never falls back to
`input_modality="audio"` with text as the actual input.

Both tiers execute typed tools against their configured real integration or
the recipe's explicit deterministic stub. Tool calls update the same
`CallResultBuffer` contract used by production.

## Paid PSTN tier

The live tier deliberately treats the target agent as a black box. It records
the caller/agent transcript, carrier terminal status, runtime path, carrier
call id, and runtime call id. The judge sees caller-visible goals and spoken
outcomes only; hidden target tools or durable target state are not asserted.
JUnit writes the secret-free facts as `evidence.*` properties.

Pipecat projects run a pinned native `PipelineWorker` caller at the carrier's
8 kHz PCMU boundary, expose only signed Twilio callback/media routes through
the selected tunnel, and dial with the durable Twilio intent ledger. LiveKit
projects create an isolated room, run a pinned native caller `AgentSession`,
and add the target number with an outbound SIP participant. Both paths use the
reference Deepgram, Anthropic, and Cartesia models.

Configure only environment-variable references in
`tests/voicey-test.jsonc`:

```json5
{
  live: {
    tunnel: "ngrok", // Pipecat only; auto|ngrok|cloudflared|url
    port: 18765,
    answer_timeout_s: 45,
    public_url_env: "VOICEY_LIVE_PUBLIC_URL",
    target_number_env: "VOICEY_LIVE_TARGET_NUMBER",
    twilio_from_number_env: "VOICEY_LIVE_TWILIO_FROM",
    livekit_outbound_trunk_env: "VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID",
    paid_ack_env: "VOICEY_LIVE_PSTN_ACK",
    max_calls_env: "VOICEY_LIVE_PSTN_MAX_CALLS",
  },
}
```

For one selected case, the guarded invocation is:

```bash
export VOICEY_LIVE_PSTN_ACK='I_ACKNOWLEDGE_PAID_PSTN'
export VOICEY_LIVE_PSTN_MAX_CALLS=4
export VOICEY_LIVE_TARGET_NUMBER='+14155550123'
voicey test --live --filter live_greeting_smoke --report junit
```

The call limit must be at least four times the number of selected
profile-expanded cases because an initial failure is always retained and may
be rerun three times. It cannot exceed 1000. Pipecat additionally requires
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`VOICEY_LIVE_TWILIO_FROM`, and either a usable tunnel or an HTTPS origin in
`VOICEY_LIVE_PUBLIC_URL`. LiveKit requires `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and
`VOICEY_LIVEKIT_OUTBOUND_TRUNK_ID`. Both require the three reference-provider
keys and the configured judge key. Target and Twilio caller numbers must be
distinct E.164 values.

The repository's nightly workflow remains skipped unless
`VOICEY_LIVE_PSTN_ENABLED=true`. Its protected `paid-pstn` environment must
also set `VOICEY_LIVE_PSTN_ACK` to the exact acknowledgement and provide all
secrets. A skipped job is not green evidence.

## Local default and cloud override

The default sim caller and cited judge are tool-capable local Ollama `qwen3:8b`.
Requests disable the model's optional thinking mode for deterministic test
latency. Native LiveKit judgments have a 60-second hard timeout. The native
conversation alone is cancelled at the scenario's declared duration budget;
goal judging runs after the session closes and cannot consume that call budget:

```bash
ollama pull qwen3:8b
ollama serve
voicey test
```

Override either model in the secret-free
`tests/voicey-test.jsonc`:

```json5
{
  judge: {
    service: "anthropic",
    model: "claude-sonnet-5",
    base_url: "https://api.anthropic.com",
    api_key_env: "ANTHROPIC_API_KEY", // pragma: allowlist secret
  },
  sim_caller: {
    service: "anthropic",
    model: "claude-sonnet-5",
    base_url: "https://api.anthropic.com",
    api_key_env: "ANTHROPIC_API_KEY", // pragma: allowlist secret
  },
}
```

`service: "anthropic"` uses Anthropic's native Messages API and the installed
native Pipecat/LiveKit provider plugins. `service: "openai"` remains the
OpenAI-compatible cloud option. The config stores only the environment-variable
name. Add the value through `voicey keys add anthropic` (or `openai`); never put
a secret in scenario or config source. Claude Sonnet 5 does not accept the
legacy `temperature` field, so the Anthropic adapter intentionally omits it.
Sim-caller plans and cited transcript verdicts use Anthropic's strict JSON
schema output format; Pipecat's native cloud judge disables adaptive thinking.

On 2026-08-03 the full seven-case appointment text suite ran green on the first
attempt on both runtimes using only model APIs: Deepgram Nova-3, Claude Sonnet
5, Cartesia Sonic 3.5, and native Anthropic judges. Those runs prove the text
provider path only. They do not promote the audio, latency, PSTN, microphone,
or physical-handset gates listed in `docs/GAPS.md`.

## CI

Normal CI validates the shared schema, profile expansion, installed native
parsers, text/audio compilation, LiveKit assertion plans, PCM bridge, exit
contract, JSON, JUnit, and every first-party recipe suite for both runtimes.
Credentialed reference-provider execution is a separate guarded gate because
it spends provider capacity. The two live callers have a separately guarded
nightly workflow and bounded one-case fixtures. Exact commands and current
evidence are in `docs/GAPS.md`.

Next: run `voicey test`, then fix every hard or cited failure before
`voicey dev`.
