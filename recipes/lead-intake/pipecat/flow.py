"""Native Pipecat Flows entrypoint for lead intake."""

from __future__ import annotations

from pathlib import Path

from pipecat.flows import FlowManager, NodeConfig

_SOURCE_DIR = Path(__file__).parent
_PROMPTS = (
    _SOURCE_DIR / "prompts"
    if (_SOURCE_DIR / "prompts").is_dir()
    else _SOURCE_DIR.parent / "prompts"
)


def entry(_flow_manager: FlowManager) -> NodeConfig:
    """Create the lead coordinator as a native Pipecat node."""
    role = "\n\n".join(
        (_PROMPTS / name).read_text(encoding="utf-8")
        for name in ("system.md", "failure.md", "voicemail.md")
    )
    greeting = (_PROMPTS / "greeting.md").read_text(encoding="utf-8").strip()
    return NodeConfig(
        name="lead-intake-coordinator",
        role_message=role,
        task_messages=[{"role": "developer", "content": greeting}],
        respond_immediately=True,
    )
