"""Copied LiveKit recipe smoke tests."""

import asyncio

from agent import agent
from flow import entrypoint
from livekit.agents import Agent, function_tool, llm


async def example_shared_tool() -> str:
    """Return deterministic evidence for recipe smoke tests."""
    return "ok"


def _function(agent_workflow: Agent, name: str) -> llm.FunctionTool:
    return next(
        tool
        for tool in agent_workflow.tools
        if isinstance(tool, llm.FunctionTool) and tool.info.name == name
    )


def test_appointment_recipe_uses_native_livekit_handoffs() -> None:
    shared = function_tool(example_shared_tool)
    intake = entrypoint([shared])
    booking = asyncio.run(_function(intake, "start_booking")())

    assert agent.runtime == "livekit"
    assert agent.flow == "flow:entrypoint"
    assert isinstance(intake, Agent)
    assert isinstance(booking, Agent)
    assert intake.id == "appointment_intake_agent"
    assert booking.id == "booking_agent"
    assert shared in intake.tools
    assert shared in booking.tools
    assert _function(booking, "return_to_intake")
    assert agent.behavior.voicemail == "leave_message"
