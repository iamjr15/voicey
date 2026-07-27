"""Native Pipecat Flows fixture."""

from pipecat.flows import FlowManager, NodeConfig


def entry(_manager: FlowManager) -> NodeConfig:
    return NodeConfig(
        name="entry",
        task_messages=[{"role": "developer", "content": "Greet the caller."}],
        respond_immediately=True,
    )
