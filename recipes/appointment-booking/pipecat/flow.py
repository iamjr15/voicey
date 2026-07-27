"""Native Pipecat Flows entrypoint for appointment booking."""

from __future__ import annotations

from pathlib import Path

from pipecat.flows import FlowManager, NodeConfig

_PROMPTS = Path(__file__).parent / "prompts"


def entry(_flow_manager: FlowManager) -> NodeConfig:
    """Create the initial native node; global typed tools come from voicekit."""
    role = "\n\n".join(
        (_PROMPTS / name).read_text(encoding="utf-8")
        for name in ("system.md", "failure.md", "voicemail.md")
    )
    return NodeConfig(
        name="appointment-intake",
        role_message=role,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Greet the caller, ask whether they want to book, reschedule, "
                    "cancel, or speak with a person, then follow the role policy. "
                    "Use the registered calendar functions for every calendar fact."
                ),
            }
        ],
        respond_immediately=True,
    )
