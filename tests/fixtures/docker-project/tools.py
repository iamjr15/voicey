"""Typed fixture tool."""

from voicey import tool


@tool
def deployment_identity() -> str:
    """Return a stable deployment fixture identity."""
    return "docker-fixture"
