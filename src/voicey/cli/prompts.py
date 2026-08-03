"""Questionary adapter with explicit, neutral, non-defaulted choices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

import questionary
from prompt_toolkit.styles import Style

from voicey.errors import VoiceyError

ValueT = TypeVar("ValueT")


@dataclass(frozen=True, slots=True)
class PromptChoice:
    """One factual choice with an optional capability-disabled explanation."""

    title: str
    value: str
    description: str
    disabled_reason: str | None = None


class PromptIO(Protocol):
    """Injectable prompt surface used by the resumable wizard."""

    @property
    def interactive(self) -> bool: ...

    def select(self, message: str, choices: tuple[PromptChoice, ...]) -> str: ...

    def multiselect(
        self,
        message: str,
        choices: tuple[PromptChoice, ...],
        *,
        minimum: int = 1,
    ) -> tuple[str, ...]: ...

    def text(self, message: str) -> str: ...

    def secret(self, message: str) -> str: ...

    def notice(self, message: str) -> None: ...


class QuestionaryPromptIO:
    """Production prompt adapter; no semantic answer has a default."""

    def __init__(self, *, interactive: bool = True) -> None:
        self._interactive = interactive
        self._style = Style.from_dict(
            {
                "qmark": "fg:#5f87ff bold",
                "question": "bold",
                "pointer": "fg:#5f87ff bold",
                "highlighted": "fg:#5f87ff",
                "selected": "fg:#5faf5f",
            }
        )

    @property
    def interactive(self) -> bool:
        return self._interactive

    def select(self, message: str, choices: tuple[PromptChoice, ...]) -> str:
        self._require_interactive(message)
        answer = questionary.select(
            message,
            choices=[
                questionary.Choice(
                    title=choice.title,
                    value=choice.value,
                    description=choice.description,
                    disabled=choice.disabled_reason,
                    checked=False,
                )
                for choice in choices
            ],
            default=None,
            style=self._style,
            show_description=True,
        ).ask()
        return _answer(answer, message)

    def multiselect(
        self,
        message: str,
        choices: tuple[PromptChoice, ...],
        *,
        minimum: int = 1,
    ) -> tuple[str, ...]:
        self._require_interactive(message)

        def validate(values: list[str]) -> bool | str:
            return True if len(values) >= minimum else f"Select at least {minimum}."

        answer = questionary.checkbox(
            message,
            choices=[
                questionary.Choice(
                    title=choice.title,
                    value=choice.value,
                    description=choice.description,
                    disabled=choice.disabled_reason,
                    checked=False,
                )
                for choice in choices
            ],
            validate=validate,
            style=self._style,
            show_description=True,
        ).ask()
        return tuple(cast("list[str]", _answer(answer, message)))

    def text(self, message: str) -> str:
        self._require_interactive(message)
        answer = questionary.text(message, default="", style=self._style).ask()
        return str(_answer(answer, message)).strip()

    def secret(self, message: str) -> str:
        self._require_interactive(message)
        answer = questionary.password(message, style=self._style).ask()
        return str(_answer(answer, message)).strip()

    def notice(self, message: str) -> None:
        questionary.print(message)

    def _require_interactive(self, question: str) -> None:
        if not self._interactive:
            raise VoiceyError(
                "VY-CLI-001",
                detail=f"explicit flag required for: {question}",
            )


def _answer(value: ValueT | None, question: str) -> ValueT:
    if value is None:
        raise VoiceyError(
            "VY-CLI-002",
            detail=f"setup was interrupted while answering: {question}",
        )
    return value
