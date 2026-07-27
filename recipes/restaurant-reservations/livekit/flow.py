"""Native LiveKit workflow for restaurant reservations."""

from __future__ import annotations

from pathlib import Path

from livekit.agents import Agent, function_tool, llm

_SOURCE_DIR = Path(__file__).parent
_PROMPTS = (
    _SOURCE_DIR / "prompts"
    if (_SOURCE_DIR / "prompts").is_dir()
    else _SOURCE_DIR.parent / "prompts"
)
_ROLE = "\n\n".join(
    (_PROMPTS / name).read_text(encoding="utf-8")
    for name in ("system.md", "failure.md", "voicemail.md")
)
_GREETING = (_PROMPTS / "greeting.md").read_text(encoding="utf-8").strip()
NativeTool = llm.Tool | llm.Toolset


class ReservationIntakeAgent(Agent):
    """Native intake that hands unavailable requests to a waitlist specialist."""

    def __init__(self, *, tools: list[NativeTool], chat_ctx: llm.ChatContext | None = None) -> None:
        self._shared_tools = list(tools)
        super().__init__(instructions=_ROLE, tools=self._shared_tools, chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=_GREETING)

    @function_tool
    async def discuss_waitlist(self) -> Agent:
        """Hand an unavailable request to the native waitlist specialist."""
        return WaitlistAgent(tools=self._shared_tools, chat_ctx=self.chat_ctx)


class WaitlistAgent(Agent):
    """Obtain explicit waitlist consent and allow a native return handoff."""

    def __init__(self, *, tools: list[NativeTool], chat_ctx: llm.ChatContext | None = None) -> None:
        self._shared_tools = list(tools)
        super().__init__(
            instructions=(
                f"{_ROLE}\n\nThe requested table is unavailable. Explain waitlist uncertainty, "
                "confirm date, preferred time, timezone, party size, name, and phone, then "
                "call join_waitlist only after explicit consent."
            ),
            tools=self._shared_tools,
            chat_ctx=chat_ctx,
        )

    @function_tool
    async def return_to_reservations(self) -> Agent:
        """Return to table search when the caller changes their request."""
        return ReservationIntakeAgent(tools=self._shared_tools, chat_ctx=self.chat_ctx)


def entrypoint(tools: list[NativeTool]) -> Agent:
    """Return the native reservation intake Agent."""
    return ReservationIntakeAgent(tools=tools)
