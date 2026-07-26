import pytest

from voicekit import tool
from voicekit.errors import VoicekitError
from voicekit.tools import get_tool_metadata


def test_tool_keeps_plain_function_and_records_metadata() -> None:
    @tool(say_while_running="One moment.")
    def lookup_slot(day: str) -> str:
        """Look up one appointment slot."""
        return f"{day}:10:00"

    assert lookup_slot("Monday") == "Monday:10:00"
    assert get_tool_metadata(lookup_slot).name == "lookup_slot"
    assert get_tool_metadata(lookup_slot).description == "Look up one appointment slot."
    assert get_tool_metadata(lookup_slot).say_while_running == "One moment."


def test_bare_tool_decorator() -> None:
    @tool
    async def ping() -> str:
        """Return a health response."""
        return "pong"

    assert get_tool_metadata(ping).name == "ping"


def test_undecorated_callable_is_rejected() -> None:
    def plain() -> None:
        return None

    with pytest.raises(VoicekitError, match="VK-TOL-001"):
        get_tool_metadata(plain)
