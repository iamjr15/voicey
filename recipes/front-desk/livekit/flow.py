"""Native LiveKit front-desk workflow."""

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


class FrontDeskAgent(Agent):
    """Answer and triage, with native handoff to message collection."""

    def __init__(self, *, tools: list[NativeTool], chat_ctx: llm.ChatContext | None = None) -> None:
        self._shared_tools = list(tools)
        super().__init__(instructions=_ROLE, tools=self._shared_tools, chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=_GREETING)

    @function_tool
    async def start_message(self) -> Agent:
        """Hand the caller to the native message specialist."""
        return MessageAgent(tools=self._shared_tools, chat_ctx=self.chat_ctx)


class MessageAgent(Agent):
    """Collect and confirm one callback message."""

    def __init__(self, *, tools: list[NativeTool], chat_ctx: llm.ChatContext | None = None) -> None:
        self._shared_tools = list(tools)
        super().__init__(
            instructions=(
                f"{_ROLE}\n\nCollect name, E.164 callback number, department, and a concise "
                "message. Read them back and call take_message only after explicit confirmation."
            ),
            tools=self._shared_tools,
            chat_ctx=chat_ctx,
        )

    @function_tool
    async def return_to_front_desk(self) -> Agent:
        """Return to the main front desk when the caller changes intent."""
        return FrontDeskAgent(tools=self._shared_tools, chat_ctx=self.chat_ctx)


def entrypoint(tools: list[NativeTool]) -> Agent:
    """Return the native front-desk Agent."""
    return FrontDeskAgent(tools=tools)
