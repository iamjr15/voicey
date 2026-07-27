# Simulated-caller testing

`voicekit test` runs one owned scenario suite through the native evaluator for
the runtime selected in `voicekit.jsonc`. It does not replace native flow code:
Pipecat projects compile to Pipecat Evals 1.6.0 YAML, while LiveKit projects run
LiveKit Agents 1.6.7 `AgentSession.run()` and `RunResult.expect` assertions.

## Scenario source

Put scenarios in `tests/scenarios.py` or `tests/scenarios/*.py`. Discovery
imports only those locations, calls each zero-argument `@scenario` function
once, rejects duplicate names, and validates the returned value.

```python
from voicekit.testing import scenario


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

`expect.outcome`, nested `expect.data` values or predicates, `max_turns`, and
the wall-clock budget are hard assertions. `TurnExpectation` can assert native
tool calls, response content, handoffs, response-time budgets, and goal-based
judge criteria. Scenario-level judge decisions must include valid transcript
line citations; a model that says “pass” without a citation fails.

## Commands and exit contract

```bash
voicekit test
voicekit test --filter change_mind
voicekit test --audio
voicekit test --report junit
voicekit test --report json
```

- Exit `0`: every selected case passed on its first attempt.
- Exit `1`: invalid setup/runtime prerequisites or any failed case.
- An initial scenario failure is rerun three times. All four attempts remain in
  the report, the scenario remains failed, and stability is shown as the
  percentage of successful attempts.
- JUnit is written to `.voicekit/test-results.xml`.
- Generated native inputs, logs, and durable eval results remain under
  `.voicekit/test-runs/`, which the scaffold ignores.
- Every output ends with a next command. JSON includes `next_step`.

`--live` is deliberately fail-closed until the P3 PSTN loopback harness lands.
It never substitutes text or local audio while claiming a live result.

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

## Local default and cloud override

The default sim caller and cited judge are local Ollama `gemma2:9b`:

```bash
ollama pull gemma2:9b
ollama serve
voicekit test
```

Override either model in the secret-free
`tests/voicekit-test.jsonc`:

```json5
{
  judge: {
    service: "openai",
    model: "gpt-5-mini",
    base_url: "https://api.openai.com/v1",
    api_key_env: "OPENAI_API_KEY", // pragma: allowlist secret
  },
  sim_caller: {
    service: "ollama",
    model: "gemma2:9b",
    base_url: "http://localhost:11434/v1",
  },
}
```

The config stores only the environment-variable name. Add the value through
`voicekit keys add openai`; never put a secret in scenario or config source.

## CI

Normal CI validates the shared schema, profile expansion, installed native
parsers, text/audio compilation, LiveKit assertion plans, PCM bridge, exit
contract, JSON, JUnit, and every first-party recipe suite for both runtimes.
Credentialed reference-provider execution is a separate guarded gate because
it spends provider capacity; its exact commands and current evidence are in
`docs/GAPS.md`.

Next: run `voicekit test`, then fix every hard or cited failure before
`voicekit dev`.
