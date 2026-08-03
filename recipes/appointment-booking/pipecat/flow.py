"""Native Pipecat Flows entrypoint for appointment booking."""

from __future__ import annotations

from pathlib import Path

from pipecat.flows import FlowManager, NodeConfig

_SOURCE_DIR = Path(__file__).parent
_PROMPTS = (
    _SOURCE_DIR / "prompts"
    if (_SOURCE_DIR / "prompts").is_dir()
    else _SOURCE_DIR.parent / "prompts"
)
_GREETING_INSTRUCTION = (_PROMPTS / "greeting.md").read_text(encoding="utf-8").strip()


def entry(_flow_manager: FlowManager) -> NodeConfig:
    """Create the initial native node; global typed tools come from voicey."""
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
                "content": _GREETING_INSTRUCTION,
            }
        ],
        respond_immediately=True,
    )
