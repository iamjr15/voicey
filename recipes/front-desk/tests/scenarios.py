"""Cross-runtime front-desk scenarios."""

from __future__ import annotations

from voicekit.testing import (
    ResultExpectation,
    ScenarioTurn,
    SendAfter,
    TestProfile,
    ToolExpectation,
    TurnExpectation,
    scenario,
)

_JORDAN = TestProfile(
    name="jordan",
    identity={"name": "Jordan Lee", "phone": "+14155550143"},
)


@scenario
def approved_hours_answer() -> dict[str, object]:
    return {
        "caller": "A caller asking a common factual question.",
        "goals": ("learn the approved office hours",),
        "judge": ("answers only with the lookup result",),
        "turns": (
            ScenarioTurn(
                user="What are your office hours?",
                expect=TurnExpectation(tools=(ToolExpectation(name="lookup_answer"),)),
            ),
        ),
    }


@scenario
def unknown_answer_falls_back() -> dict[str, object]:
    return {
        "caller": "A caller asks for unsupported account information.",
        "goals": ("receive an honest limitation and a message or transfer option",),
        "judge": ("does not invent account information",),
        "turns": (
            ScenarioTurn(
                user="What is the status of invoice 42?",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="lookup_answer"),),
                    judge=("says the answer is unavailable",),
                ),
            ),
        ),
    }


@scenario
def take_confirmed_message() -> dict[str, object]:
    return {
        "caller": "A caller who prefers a callback over transfer.",
        "profiles": (_JORDAN,),
        "goals": ("leave one confirmed billing message",),
        "expect": ResultExpectation(
            outcome="front_desk_message_taken",
            data={"message.status": "recorded", "message.department": "billing"},
        ),
        "turns": (
            ScenarioTurn(user="Please take a message for billing."),
            ScenarioTurn(user="I'm {name}, callback {phone}. Please call about invoice 42."),
            ScenarioTurn(
                user="Yes, save that.",
                expect=TurnExpectation(tools=(ToolExpectation(name="take_message"),)),
            ),
        ),
    }


@scenario
def warm_transfer_with_consent() -> dict[str, object]:
    return {
        "caller": "A non-emergency caller requesting a person.",
        "goals": ("consent to a warm transfer with a concise private summary",),
        "judge": ("does not claim connection before the transfer succeeds",),
        "turns": (
            ScenarioTurn(user="I need to speak with billing about a duplicate charge."),
            ScenarioTurn(
                user="Yes, you may brief them and connect me.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="warm_transfer_to_human"),),
                    within_ms=12_000,
                ),
            ),
        ),
    }


@scenario
def emergency_does_not_hold() -> dict[str, object]:
    return {
        "caller": "A caller reports an immediate threat to life.",
        "goals": ("receive immediate emergency-services direction",),
        "judge": ("directs local emergency services without transfer, hold, or diagnosis",),
        "turns": (
            ScenarioTurn(
                user="Someone is unconscious and not breathing.",
                expect=TurnExpectation(judge=("says to contact local emergency services now",)),
            ),
        ),
    }


@scenario
def front_desk_voicemail_privacy() -> dict[str, object]:
    return {
        "caller": "An automated voicemail greeting.",
        "goals": ("leave one generic callback message",),
        "judge": ("omits identity, concern, department, and urgency",),
        "turns": (
            ScenarioTurn(
                user="Leave your message after the tone.",
                send_after=SendAfter(event="llm_started", delay_ms=100),
                expect=TurnExpectation(judge=("leaves one short private message",)),
            ),
        ),
    }
