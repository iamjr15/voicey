from __future__ import annotations

from typing import Any, ClassVar

import pytest

from voicey.cli.prompts import PromptChoice, QuestionaryPromptIO
from voicey.errors import VoiceyError


class StubQuestion:
    value: ClassVar[object]
    threaded_runs: ClassVar[list[bool]] = []

    @property
    def application(self) -> StubQuestion:
        return self

    def ask(self) -> object:
        return self.value

    def run(self, *, in_thread: bool = False) -> object:
        self.threaded_runs.append(in_thread)
        return self.value


class QuestionaryStub:
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    @classmethod
    def select(cls, message: str, **kwargs: Any) -> StubQuestion:
        cls.calls.append((f"select:{message}", dict(kwargs)))
        return StubQuestion()

    @classmethod
    def checkbox(cls, message: str, **kwargs: Any) -> StubQuestion:
        cls.calls.append((f"checkbox:{message}", dict(kwargs)))
        return StubQuestion()

    @classmethod
    def text(cls, message: str, **kwargs: Any) -> StubQuestion:
        cls.calls.append((f"text:{message}", dict(kwargs)))
        return StubQuestion()

    @classmethod
    def password(cls, message: str, **kwargs: Any) -> StubQuestion:
        cls.calls.append((f"password:{message}", dict(kwargs)))
        return StubQuestion()


@pytest.fixture(autouse=True)
def stub_questionary(monkeypatch: pytest.MonkeyPatch) -> None:
    QuestionaryStub.calls.clear()
    StubQuestion.threaded_runs.clear()
    monkeypatch.setattr("voicey.cli.prompts.questionary.select", QuestionaryStub.select)
    monkeypatch.setattr("voicey.cli.prompts.questionary.checkbox", QuestionaryStub.checkbox)
    monkeypatch.setattr("voicey.cli.prompts.questionary.text", QuestionaryStub.text)
    monkeypatch.setattr("voicey.cli.prompts.questionary.password", QuestionaryStub.password)


def test_questionary_adapter_never_supplies_semantic_defaults() -> None:
    prompt = QuestionaryPromptIO()
    choices = (
        PromptChoice("First", "first", "Fact one."),
        PromptChoice("Later", "later", "Fact two.", disabled_reason="not ready"),
    )

    StubQuestion.value = "first"
    assert prompt.select("Choose", choices) == "first"
    select_kwargs = QuestionaryStub.calls[-1][1]
    assert select_kwargs["default"] is None
    assert all(not choice.checked for choice in select_kwargs["choices"])
    assert select_kwargs["choices"][1].disabled == "not ready"

    StubQuestion.value = ["first"]
    assert prompt.multiselect("Choose many", choices) == ("first",)
    checkbox_kwargs = QuestionaryStub.calls[-1][1]
    validator = checkbox_kwargs["validate"]
    assert validator([]) == "Select at least 1."
    assert validator(["first"]) is True
    assert all(not choice.checked for choice in checkbox_kwargs["choices"])


def test_questionary_text_secret_notice_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[str] = []
    monkeypatch.setattr("voicey.cli.prompts.questionary.print", notices.append)
    prompt = QuestionaryPromptIO()

    StubQuestion.value = "  words  "
    assert prompt.text("Text") == "words"
    assert QuestionaryStub.calls[-1][1]["default"] == ""
    StubQuestion.value = "  secret  "
    assert prompt.secret("Secret") == "secret"
    prompt.notice("Shown")
    assert notices == ["Shown"]

    StubQuestion.value = None
    with pytest.raises(VoiceyError) as cancelled:
        prompt.select("Cancelled", ())
    assert cancelled.value.code == "VY-CLI-002"


@pytest.mark.asyncio
async def test_questionary_prompt_uses_a_thread_inside_the_async_wizard() -> None:
    prompt = QuestionaryPromptIO()
    StubQuestion.value = "  secret  "

    assert prompt.secret("Secret") == "secret"
    assert StubQuestion.threaded_runs == [True]


def test_noninteractive_prompt_fails_before_questionary_call() -> None:
    prompt = QuestionaryPromptIO(interactive=False)

    with pytest.raises(VoiceyError) as caught:
        prompt.select("No default", ())

    assert caught.value.code == "VY-CLI-001"
    assert not prompt.interactive
    assert QuestionaryStub.calls == []
