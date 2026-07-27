"""Compiler and native assertion helpers for LiveKit Agents 1.6.7."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from voicekit.testing.models import ScenarioDefinition, ScenarioTurn, TestProfile


@dataclass(frozen=True, slots=True)
class LiveKitTurn:
    """One profile-rendered input and its native assertion plan."""

    user: str | None
    source: ScenarioTurn


@dataclass(frozen=True, slots=True)
class LiveKitCase:
    """A shared scenario compiled to LiveKit ``session.run`` operations."""

    name: str
    scenario: ScenarioDefinition
    profile: TestProfile
    turns: tuple[LiveKitTurn, ...]


def compile_livekit(
    scenarios: tuple[ScenarioDefinition, ...],
    *,
    planned_turns: dict[tuple[str, str], tuple[ScenarioTurn, ...]] | None = None,
) -> tuple[LiveKitCase, ...]:
    """Expand test profiles into deterministic native LiveKit run plans."""
    compiled: list[LiveKitCase] = []
    for definition in scenarios:
        for profile in definition.profiles:
            turns = definition.turns or (planned_turns or {}).get(
                (definition.name, profile.name), ()
            )
            compiled.append(
                LiveKitCase(
                    name=f"{definition.name}[{profile.name}]",
                    scenario=definition,
                    profile=profile,
                    turns=tuple(
                        LiveKitTurn(
                            user=_render(turn.user, profile) if turn.user is not None else None,
                            source=turn,
                        )
                        for turn in turns
                    ),
                )
            )
    return tuple(compiled)


async def assert_native_turn(
    result: Any,
    turn: LiveKitTurn,
    *,
    judge_llm: Any,
) -> None:
    """Apply only installed LiveKit ``RunResult.expect`` assertions."""
    expectation = turn.source.expect
    if expectation is None:
        return
    for tool in expectation.tools:
        if "livekit" not in tool.runtimes:
            continue
        result.expect.contains_function_call(
            name=tool.name,
            **({"arguments": tool.arguments} if tool.arguments else {}),
        )
    if expectation.handoff:
        result.expect.contains_agent_handoff()
    assistant = None
    if expectation.text_contains or expectation.judge:
        assistant = result.expect.contains_message(role="assistant")
    if expectation.text_contains:
        assert assistant is not None
        content = assistant.event().item.text_content or ""
        if expectation.text_contains.casefold() not in content.casefold():
            raise AssertionError(
                f"assistant message does not contain {expectation.text_contains!r}"
            )
    if expectation.judge:
        assert assistant is not None
        for criterion in expectation.judge:
            await assistant.judge(judge_llm, intent=criterion)


def _render(value: str, profile: TestProfile) -> str:
    return value.format_map(profile.identity)
