"""Cross-runtime front-desk scenarios."""

from __future__ import annotations

from voicey.testing import (
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
        "judge": ("reports the approved office hours without inventing additional facts",),
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
            ScenarioTurn(
                user="Please take a message for billing.",
                expect=TurnExpectation(
                    judge=("asks for the caller name, callback number, and message",)
                ),
            ),
            ScenarioTurn(
                user="I'm {name}, callback {phone}. Please call about invoice 42.",
                expect=TurnExpectation(
                    judge=("reads the billing message back and asks before saving it",)
                ),
            ),
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
        "profiles": (_JORDAN,),
        "goals": ("consent to a warm transfer with a concise private summary",),
        "judge": ("clearly states the billing handoff status to the caller",),
        "turns": (
            ScenarioTurn(
                user="I need to speak with billing about a duplicate charge.",
                expect=TurnExpectation(
                    judge=(
                        "acknowledges the request for human billing help and asks for safe "
                        "handoff details",
                    )
                ),
            ),
            ScenarioTurn(
                user=("I'm {name}, callback {phone}. The issue is a duplicate charge."),
                expect=TurnExpectation(
                    judge=(
                        "asks for explicit transfer consent after collecting the available "
                        "handoff details",
                    )
                ),
            ),
            ScenarioTurn(
                user=(
                    "Those details are correct. I explicitly consent now: privately "
                    "brief billing and complete the transfer."
                ),
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="warm_transfer_to_human"),),
                    within_ms=30_000,
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
        "turns": (
            ScenarioTurn(
                user="Leave your message after the tone.",
                send_after=SendAfter(event="llm_started", delay_ms=100),
                expect=TurnExpectation(
                    judge=(
                        "leaves one short generic callback message without personal or "
                        "case-specific details",
                    )
                ),
            ),
        ),
    }
