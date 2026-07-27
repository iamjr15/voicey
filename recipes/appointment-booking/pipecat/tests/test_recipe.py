"""Copied recipe smoke tests."""

from agent import agent
from flow import entry


def test_appointment_recipe_uses_native_pipecat_flow() -> None:
    node = entry(None)  # type: ignore[arg-type]
    assert agent.runtime == "pipecat"
    assert agent.flow == "flow:entry"
    assert node["name"] == "appointment-intake"
    assert "actually" in node["role_message"].casefold()
    assert agent.behavior.voicemail == "leave_message"
