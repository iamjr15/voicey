"""One bounded, paid black-box smoke scenario for the nightly Pipecat path."""

from voicey.testing import ScenarioTurn, scenario


@scenario
def live_greeting_smoke():
    return {
        "caller": "A concise caller verifying that the target agent can answer a phone call.",
        "goals": ["receive a relevant spoken answer to a basic availability question"],
        "judge": ["the target agent answers the caller's availability question"],
        "turns": (
            ScenarioTurn(user="Hello, are you available to help me today?"),
            ScenarioTurn(user="Thank you, goodbye."),
        ),
        "max_turns": 4,
        "max_duration_ms": 90_000,
    }
